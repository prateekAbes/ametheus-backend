import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.database import get_db
from app.models.user import User
from app.api.deps import get_current_user, require_super_admin, require_organizer

@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
    return db

@pytest_asyncio.fixture
async def client(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db
    
    organizer_user = User(
        id="org_123",
        clerk_user_id="user_org_123",
        email="organizer@ametheus.gg",
        username="organizer_prime",
        role="ORGANIZER",
    )
    app.dependency_overrides[get_current_user] = lambda: organizer_user
    app.dependency_overrides[require_super_admin] = lambda: organizer_user
    app.dependency_overrides[require_organizer] = lambda: organizer_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_tournament_creation_success(client, mock_db):
    """Test successful tournament creation with starts_at, roster_lock_time, and community links."""
    await mock_db.games.insert_one({
        "_id": "valorant",
        "id": "valorant",
        "slug": "valorant",
        "name": "Valorant",
        "accent_color": "#FF4655",
    })

    payload = {
        "game_id": "valorant",
        "title": "Ametheus Premier Cup 2026",
        "format": "Single Elimination · BO3",
        "prize_pool_inr": 50000,
        "entry_fee_inr": 250,
        "max_teams": 16,
        "min_team_size": 5,
        "max_team_size": 5,
        "starts_at": "2026-11-01T14:00:00.000Z",
        "roster_lock_time": "2026-11-01T12:00:00.000Z",
        "whatsapp_group_url": "https://chat.whatsapp.com/sampleInvite123",
        "community_link": "https://discord.gg/ametheus",
        "description": "Official competitive championship on AMETHEUS.",
    }

    res = await client.post("/api/v1/organizer/tournaments", json=payload)
    assert res.status_code == 201, f"Failed: {res.text}"
    data = res.json()

    assert data["title"] == "Ametheus Premier Cup 2026"
    assert data["starts_at"] == "2026-11-01T14:00:00.000Z"
    assert data["roster_lock_time"] == "2026-11-01T12:00:00.000Z"
    assert data["whatsapp_group_url"] == "https://chat.whatsapp.com/sampleInvite123"
    assert data["community_link"] == "https://discord.gg/ametheus"
    assert data["prize_pool_inr"] == 50000
    assert data["entry_fee_inr"] == 250
    assert data["max_teams"] == 16

    # Verify persisted in database
    saved = await mock_db.tournaments.find_one({"title": "Ametheus Premier Cup 2026"})
    assert saved is not None
    assert saved["starts_at"] == "2026-11-01T14:00:00.000Z"
    assert saved["roster_lock_time"] == "2026-11-01T12:00:00.000Z"
    assert saved["whatsapp_group_url"] == "https://chat.whatsapp.com/sampleInvite123"
    assert saved["community_link"] == "https://discord.gg/ametheus"


@pytest.mark.asyncio
async def test_tournament_creation_missing_required_dates(client, mock_db):
    """Test that missing starts_at or roster_lock_time fails with HTTP 422."""
    await mock_db.games.insert_one({
        "_id": "valorant",
        "id": "valorant",
        "slug": "valorant",
        "name": "Valorant",
    })

    # Missing roster_lock_time
    payload = {
        "game_id": "valorant",
        "title": "Championship Without Lock Time",
        "starts_at": "2026-11-01T14:00:00.000Z",
        "description": "Description",
    }
    res = await client.post("/api/v1/organizer/tournaments", json=payload)
    assert res.status_code == 422
    err_detail = str(res.json())
    assert "roster_lock_time" in err_detail

    # Missing starts_at
    payload2 = {
        "game_id": "valorant",
        "title": "Championship Without Starts At",
        "roster_lock_time": "2026-11-01T12:00:00.000Z",
        "description": "Description",
    }
    res2 = await client.post("/api/v1/organizer/tournaments", json=payload2)
    assert res2.status_code == 422
    err_detail2 = str(res2.json())
    assert "starts_at" in err_detail2


@pytest.mark.asyncio
async def test_tournament_creation_roster_lock_after_starts_at_rejected(client, mock_db):
    """Test that roster_lock_time strictly after starts_at is rejected with HTTP 422."""
    await mock_db.games.insert_one({
        "_id": "valorant",
        "id": "valorant",
        "slug": "valorant",
        "name": "Valorant",
    })

    payload = {
        "game_id": "valorant",
        "title": "Invalid Schedule Championship",
        "starts_at": "2026-11-01T10:00:00.000Z",
        "roster_lock_time": "2026-11-01T12:00:00.000Z",  # 2 hours AFTER start
        "description": "Description",
    }
    res = await client.post("/api/v1/organizer/tournaments", json=payload)
    assert res.status_code == 422
    assert "Roster lock time must be at or before tournament start time" in res.json()["detail"]


@pytest.mark.asyncio
async def test_tournament_creation_invalid_iso_format(client, mock_db):
    """Test that non-ISO formatted date strings are rejected with HTTP 422."""
    await mock_db.games.insert_one({
        "_id": "valorant",
        "id": "valorant",
        "slug": "valorant",
        "name": "Valorant",
    })

    payload = {
        "game_id": "valorant",
        "title": "Bad ISO Format Tournament",
        "starts_at": "invalid-datetime-string",
        "roster_lock_time": "2026-11-01T12:00:00.000Z",
        "description": "Description",
    }
    res = await client.post("/api/v1/organizer/tournaments", json=payload)
    assert res.status_code == 422
    assert "Invalid ISO 8601 date format" in res.json()["detail"]


@pytest.mark.asyncio
async def test_tournament_creation_empty_title_rejected(client, mock_db):
    """Test that empty or whitespace title is rejected with HTTP 422."""
    await mock_db.games.insert_one({
        "_id": "valorant",
        "id": "valorant",
        "slug": "valorant",
        "name": "Valorant",
    })

    payload = {
        "game_id": "valorant",
        "title": "   ",
        "starts_at": "2026-11-01T14:00:00.000Z",
        "roster_lock_time": "2026-11-01T12:00:00.000Z",
        "description": "Description",
    }
    res = await client.post("/api/v1/organizer/tournaments", json=payload)
    assert res.status_code == 422
    assert "Tournament title cannot be empty" in res.json()["detail"]


@pytest.mark.asyncio
async def test_tournament_creation_invalid_team_size_bounds(client, mock_db):
    """Test that invalid team size bounds are rejected with HTTP 422."""
    await mock_db.games.insert_one({
        "_id": "valorant",
        "id": "valorant",
        "slug": "valorant",
        "name": "Valorant",
    })

    # max < min
    payload = {
        "game_id": "valorant",
        "title": "Invalid Squad Bounds",
        "min_team_size": 5,
        "max_team_size": 2,
        "starts_at": "2026-11-01T14:00:00.000Z",
        "roster_lock_time": "2026-11-01T12:00:00.000Z",
        "description": "Description",
    }
    res = await client.post("/api/v1/organizer/tournaments", json=payload)
    assert res.status_code == 422
    assert "Maximum team size cannot be less than minimum team size" in res.json()["detail"]


@pytest.mark.asyncio
async def test_tournament_update_schedule_and_links(client, mock_db):
    """Test updating schedule dates and community links via organizer endpoint."""
    await mock_db.games.insert_one({
        "_id": "valorant",
        "id": "valorant",
        "slug": "valorant",
        "name": "Valorant",
    })

    # First create
    create_res = await client.post("/api/v1/organizer/tournaments", json={
        "game_id": "valorant",
        "title": "Original Tournament Title",
        "starts_at": "2026-11-01T14:00:00.000Z",
        "roster_lock_time": "2026-11-01T12:00:00.000Z",
        "description": "Original Description",
    })
    assert create_res.status_code == 201
    tid = create_res.json()["id"]

    # Attempt update with invalid schedule order (lock after start)
    bad_update = await client.put(f"/api/v1/organizer/tournaments/{tid}", json={
        "starts_at": "2026-11-05T10:00:00.000Z",
        "roster_lock_time": "2026-11-05T15:00:00.000Z",
    })
    assert bad_update.status_code == 422
    assert "Roster lock time must be at or before tournament start time" in bad_update.json()["detail"]

    # Valid update
    good_update = await client.put(f"/api/v1/organizer/tournaments/{tid}", json={
        "title": "Updated Tournament Title",
        "starts_at": "2026-11-10T18:00:00.000Z",
        "roster_lock_time": "2026-11-10T16:00:00.000Z",
        "whatsapp_group_url": "https://chat.whatsapp.com/newGroup456",
        "community_link": "https://discord.gg/newDiscordChannel",
    })
    assert good_update.status_code == 200
    updated_data = good_update.json()
    assert updated_data["title"] == "Updated Tournament Title"
    assert updated_data["starts_at"] == "2026-11-10T18:00:00.000Z"
    assert updated_data["roster_lock_time"] == "2026-11-10T16:00:00.000Z"
    assert updated_data["whatsapp_group_url"] == "https://chat.whatsapp.com/newGroup456"
    assert updated_data["community_link"] == "https://discord.gg/newDiscordChannel"


@pytest.mark.asyncio
async def test_list_tournaments_search_filter(client, mock_db):
    """Test searching tournaments via query parameter."""
    await mock_db.games.insert_one({
        "_id": "bgmi",
        "id": "bgmi",
        "slug": "bgmi",
        "name": "Battlegrounds Mobile India",
    })
    await mock_db.tournaments.insert_one({
        "_id": "bgmi-championship-2026",
        "id": "bgmi-championship-2026",
        "slug": "bgmi-championship-2026",
        "title": "BGMI Pro Championship",
        "game_id": "bgmi",
        "game_slug": "bgmi",
        "status": "REGISTRATION_OPEN",
        "is_dummy": False,
        "format": "Squad Battle Royale",
        "description": "High stakes mobile battle",
    })
    await mock_db.tournaments.insert_one({
        "_id": "valorant-cup",
        "id": "valorant-cup",
        "slug": "valorant-cup",
        "title": "Valorant Showdown",
        "game_id": "valorant",
        "game_slug": "valorant",
        "status": "REGISTRATION_OPEN",
        "is_dummy": False,
        "format": "5v5 Single Elimination",
        "description": "Tactical shooter contest",
    })

    # Search for "championship"
    res1 = await client.get("/api/v1/tournaments?search=championship")
    assert res1.status_code == 200
    items1 = res1.json()
    assert len(items1) == 1
    assert items1[0]["slug"] == "bgmi-championship-2026"

    # Search for "tactical" (in description)
    res2 = await client.get("/api/v1/tournaments?search=tactical")
    assert res2.status_code == 200
    items2 = res2.json()
    assert len(items2) == 1
    assert items2[0]["slug"] == "valorant-cup"

    # Search non-existent
    res3 = await client.get("/api/v1/tournaments?search=nonexistenttermxyz")
    assert res3.status_code == 200
    assert len(res3.json()) == 0


@pytest.mark.asyncio
async def test_tournament_creation_persistence_and_public_visibility(client, mock_db):
    """Test tournament creation persists in DB with _id, id, registration_deadline and is visible in listings."""
    await mock_db.games.insert_one({
        "_id": "cs2",
        "id": "cs2",
        "slug": "cs2",
        "name": "Counter-Strike 2",
        "accent_color": "#DE9B35",
    })

    payload = {
        "game_id": "cs2",
        "title": "Ametheus Master Series 2026",
        "format": "5v5 Single Elimination",
        "prize_pool_inr": 75000,
        "entry_fee_inr": 500,
        "max_teams": 32,
        "min_team_size": 5,
        "max_team_size": 5,
        "starts_at": "2026-11-15T15:00:00.000Z",
        "roster_lock_time": "2026-11-15T14:00:00.000Z",
        "whatsapp_group_url": "https://chat.whatsapp.com/sampleCS2Invite",
        "community_link": "https://discord.gg/cs2ametheus",
        "description": "Premier CS2 showdown on AMETHEUS.",
    }

    res = await client.post("/api/v1/organizer/tournaments", json=payload)
    assert res.status_code == 201, f"Creation failed: {res.text}"
    created = res.json()

    assert created["status"] == "REGISTRATION_OPEN"
    assert created["title"] == "Ametheus Master Series 2026"
    tid = created["id"]
    assert tid

    # 1. Verify in database: both _id and id are present
    db_tourn = await mock_db.tournaments.find_one({"$or": [{"_id": tid}, {"id": tid}]})
    assert db_tourn is not None
    assert str(db_tourn["_id"]) == tid
    assert str(db_tourn["id"]) == tid
    assert db_tourn["status"] == "REGISTRATION_OPEN"
    assert db_tourn.get("registration_deadline") is not None

    # 2. Verify in public listing
    pub_res = await client.get("/api/v1/tournaments")
    assert pub_res.status_code == 200
    pub_list = pub_res.json()
    assert any(t["id"] == tid for t in pub_list)

    # 3. Verify in status=ALL listing
    all_res = await client.get("/api/v1/tournaments?status=ALL")
    assert all_res.status_code == 200
    assert any(t["id"] == tid for t in all_res.json())

    # 4. Verify in organizer my-tournaments listing
    org_res = await client.get("/api/v1/organizer/my-tournaments")
    assert org_res.status_code == 200
    assert any(t["id"] == tid for t in org_res.json())


@pytest.mark.asyncio
async def test_tournament_creation_draft_isolation_and_publishing(client, mock_db):
    """Test that DRAFT tournament is hidden from public but visible to admin, and becomes public when published."""
    await mock_db.games.insert_one({
        "_id": "bgmi",
        "id": "bgmi",
        "slug": "bgmi",
        "name": "Battlegrounds Mobile India",
        "accent_color": "#E56A10",
    })

    payload = {
        "game_id": "bgmi",
        "title": "Secret Draft Tournament 2026",
        "status": "DRAFT",
        "format": "Squad Battle Royale",
        "prize_pool_inr": 100000,
        "entry_fee_inr": 0,
        "max_teams": 64,
        "min_team_size": 4,
        "max_team_size": 5,
        "starts_at": "2026-12-20T10:00:00.000Z",
        "roster_lock_time": "2026-12-20T09:00:00.000Z",
        "description": "Unpublished tournament draft.",
    }

    res = await client.post("/api/v1/organizer/tournaments", json=payload)
    assert res.status_code == 201
    created = res.json()
    tid = created["id"]
    assert created["status"] == "DRAFT"

    # 1. Verify in database: status is DRAFT
    db_tourn = await mock_db.tournaments.find_one({"$or": [{"_id": tid}, {"id": tid}]})
    assert db_tourn is not None
    assert db_tourn["status"] == "DRAFT"

    # 2. Public tournament listing must NOT include the draft tournament
    pub_res = await client.get("/api/v1/tournaments")
    assert pub_res.status_code == 200
    assert not any(t["id"] == tid for t in pub_res.json())

    # 3. Admin / Organizer query with include_drafts=true or status=DRAFT must include it
    draft_res = await client.get("/api/v1/tournaments?status=DRAFT")
    assert draft_res.status_code == 200
    assert any(t["id"] == tid for t in draft_res.json())

    org_res = await client.get("/api/v1/organizer/my-tournaments")
    assert org_res.status_code == 200
    assert any(t["id"] == tid for t in org_res.json())

    # 4. Publish tournament
    pub_action_res = await client.post(f"/api/v1/organizer/tournaments/{tid}/publish")
    assert pub_action_res.status_code == 200

    # 5. Now it MUST be in public listing as REGISTRATION_OPEN
    pub_after_res = await client.get("/api/v1/tournaments")
    assert pub_after_res.status_code == 200
    found_after = next((t for t in pub_after_res.json() if t["id"] == tid), None)
    assert found_after is not None
    assert found_after["status"] == "REGISTRATION_OPEN"


