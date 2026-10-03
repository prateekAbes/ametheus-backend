import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.database import get_db
from app.models.user import User
from app.api.deps import get_current_user, require_super_admin

@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
    return db

@pytest_asyncio.fixture
async def client(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db

    admin_user = User(
        id="admin_123",
        clerk_user_id="user_admin_123",
        email="admin@ametheus.gg",
        username="superadmin",
        role="SUPER_ADMIN",
    )
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_super_admin] = lambda: admin_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_genres_crud_and_game_aggregation(client, mock_db):
    """Test dynamic genres: create unique genre, attach games, retrieve aggregated list."""
    # Create genre
    payload = {
        "name": "Battle Royale",
        "slug": "battle-royale",
        "description": "Drop in and survive",
        "rotation_interval_seconds": 5,
        "is_active": True,
    }
    res = await client.post("/api/v1/admin/genres", json=payload)
    assert res.status_code == 201
    genre = res.json()
    assert genre["slug"] == "battle-royale"
    assert genre["name"] == "Battle Royale"
    assert genre["rotation_interval_seconds"] == 5
    genre_id = genre["id"]

    # Insert games in database matching this category
    await mock_db.games.insert_one({
        "_id": "g1",
        "title": "BGMI",
        "name": "BGMI",
        "slug": "bgmi",
        "category": "battle-royale",
        "logo_url": "https://img.ametheus.gg/bgmi.png",
        "status": "ACTIVE",
    })
    await mock_db.games.insert_one({
        "_id": "g2",
        "title": "Free Fire",
        "name": "Free Fire",
        "slug": "free-fire",
        "category": "BATTLE_ROYALE",
        "logo_url": "https://img.ametheus.gg/ff.png",
        "status": "ACTIVE",
    })

    # Public GET /genres
    get_res = await client.get("/api/v1/genres")
    assert get_res.status_code == 200
    genres_list = get_res.json()
    assert len(genres_list) >= 1
    br_genre = next(g for g in genres_list if g["slug"] == "battle-royale")
    assert br_genre["game_count"] == 2
    assert len(br_genre["games"]) == 2

    # PATCH genre
    patch_res = await client.patch(f"/api/v1/admin/genres/{genre_id}", json={"rotation_interval_seconds": 3})
    assert patch_res.status_code == 200
    assert patch_res.json()["rotation_interval_seconds"] == 3

    # DELETE genre
    del_res = await client.delete(f"/api/v1/admin/genres/{genre_id}")
    assert del_res.status_code == 200

@pytest.mark.asyncio
async def test_reviews_submission_and_moderation(client, mock_db):
    """Test reviews workflow: public submit, text sanitization, rating distribution, admin moderation."""
    # Submit review
    review_data = {
        "rating": 5,
        "review_text": "<b>Incredible platform!</b> Matches are smooth and instant UPI payouts work flawlessly.",
        "category": "TOURNAMENTS",
        "user_name": "ProPlayer99",
    }
    submit_res = await client.post("/api/v1/reviews", json=review_data)
    assert submit_res.status_code == 201
    created = submit_res.json()
    assert created["rating"] == 5
    # HTML stripped by sanitization
    assert "<b>" not in created["review_text"]
    review_id = created["id"]

    # Public summary
    summary_res = await client.get("/api/v1/reviews")
    assert summary_res.status_code == 200
    summary = summary_res.json()
    assert summary["total_reviews"] >= 1
    assert summary["average_rating"] >= 4.0
    assert "5" in summary["rating_distribution"]

    # Admin moderate to FEATURED
    mod_res = await client.patch(
        f"/api/v1/admin/reviews/{review_id}/status",
        json={"status": "FEATURED", "admin_notes": "Verified verified pro feedback"}
    )
    assert mod_res.status_code == 200
    assert mod_res.json()["status"] == "FEATURED"
    assert mod_res.json()["is_featured"] is True

    # Admin delete review
    del_res = await client.delete(f"/api/v1/admin/reviews/{review_id}")
    assert del_res.status_code == 200

@pytest.mark.asyncio
async def test_registration_guard_and_reopen(client, mock_db):
    """Test Match Engine registration guard: reject open tournament, allow closed tournament, reopen tournament."""
    # Insert an OPEN tournament
    await mock_db.tournaments.insert_one({
        "_id": "t_open",
        "slug": "tour-open",
        "title": "Open Championship",
        "status": "REGISTRATION_OPEN",
        "registration_status": "OPEN",
    })

    # Trying to create a match targeting the OPEN tournament MUST FAIL with 400
    match_payload = {
        "tournament_id": "t_open",
        "round": 1,
        "match_order": 1,
        "round_name": "Round 1",
        "team_a_id": "team1",
        "team_b_id": "team2",
    }
    fail_res = await client.post("/api/v1/admin/matches", json=match_payload)
    assert fail_res.status_code == 400
    assert "CLOSED" in fail_res.json()["detail"]

    # Close registration on the tournament
    await mock_db.tournaments.update_one(
        {"_id": "t_open"},
        {"$set": {"status": "REGISTRATION_CLOSED", "registration_status": "CLOSED"}}
    )

    # Now match creation succeeds
    success_res = await client.post("/api/v1/admin/matches", json=match_payload)
    assert success_res.status_code == 200

    # Reopen registration via admin endpoint
    reopen_res = await client.post("/api/v1/admin/tournaments/t_open/reopen-registration")
    assert reopen_res.status_code == 200
    updated_tour = await mock_db.tournaments.find_one({"_id": "t_open"})
    assert updated_tour["registration_status"] == "OPEN"
    assert updated_tour["status"] == "REGISTRATION_OPEN"
