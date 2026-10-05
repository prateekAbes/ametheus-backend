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
    
    test_user = User(
        id="mongo_user_123",
        clerk_user_id="user_2clerk_456",
        email="player@ametheus.gg",
        username="clerk_player",
        role="ORGANIZER",
    )
    app.dependency_overrides[get_current_user] = lambda: test_user
    app.dependency_overrides[require_super_admin] = lambda: test_user
    app.dependency_overrides[require_organizer] = lambda: test_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_tournament_upi_alias_and_clerk_captain_auth(client, mock_db):
    """Verify /{id}/register/upi works and accepts captain when captain_id matches clerk_user_id."""
    # 1. Insert game
    await mock_db.games.insert_one({
        "_id": "game_val",
        "id": "game_val",
        "slug": "valorant",
        "name": "Valorant",
        "is_team_game": True,
        "max_team_size": 5,
        "status": "ACTIVE",
    })

    # 2. Insert tournament
    await mock_db.tournaments.insert_one({
        "_id": "tourn_upi_1",
        "id": "tourn_upi_1",
        "slug": "tourn-upi-1",
        "title": "Valorant Ascent Cup",
        "game_id": "game_val",
        "status": "REGISTRATION_OPEN",
        "entry_fee_inr": 200,
        "max_teams": 16,
    })

    # 3. Insert active rotating admin UPI account
    await mock_db.upi_accounts.insert_one({
        "_id": "upi_acc_1",
        "id": "upi_acc_1",
        "upi_id": "ametheus@okaxis",
        "payee_name": "Ametheus Esports",
        "status": "ACTIVE",
        "payment_count": 0,
    })

    # 4. Insert team where captain_id is set to user's clerk_user_id
    await mock_db.teams.insert_one({
        "_id": "team_upi_1",
        "id": "team_upi_1",
        "name": "Vanguard Squad",
        "tag": "VNG",
        "game_id": "game_val",
        "captain_id": "user_2clerk_456",  # Clerk user id format
        "members": [{"user_id": "user_2clerk_456", "role": "CAPTAIN", "game_handle": "vanguard_cap"}],
    })

    # 5. Call /{id}/register/upi
    payload = {
        "team_id": "team_upi_1",
    }
    resp = await client.post("/api/v1/tournaments/tourn_upi_1/register/upi", json=payload)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["upi_id"] == "ametheus@okaxis"
    assert data["amount_inr"] == 200
    assert "payment_id" in data

@pytest.mark.asyncio
async def test_leave_team_member_and_captain_guard(client, mock_db):
    """Verify regular member can leave team, and captain cannot leave without disbanding."""
    # 1. Team with captain (user_2clerk_456)
    await mock_db.teams.insert_one({
        "_id": "team_leave_test",
        "id": "team_leave_test",
        "name": "Leave Test Team",
        "captain_id": "user_2clerk_456",
        "members": [
            {"user_id": "user_2clerk_456", "role": "CAPTAIN"},
            {"user_id": "other_member", "role": "MEMBER"},
        ],
        "substitutes": [],
    })

    # As captain, trying to leave should return 400
    resp = await client.post("/api/v1/teams/team_leave_test/leave")
    assert resp.status_code == 400
    assert "The captain cannot leave the team" in resp.json()["detail"]

    # Now make the current user a normal member
    await mock_db.teams.update_one(
        {"_id": "team_leave_test"},
        {"$set": {"captain_id": "other_captain", "members": [{"user_id": "user_2clerk_456", "role": "MEMBER"}]}}
    )
    resp2 = await client.post("/api/v1/teams/team_leave_test/leave")
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "success"

    # Verify roster is updated
    team_doc = await mock_db.teams.find_one({"_id": "team_leave_test"})
    assert len(team_doc["members"]) == 0

@pytest.mark.asyncio
async def test_notifications_multi_id_resolution(client, mock_db):
    """Verify notifications query matches both clerk_user_id and mongo user id."""
    await mock_db.notifications.insert_one({
        "_id": "notif_1",
        "user_id": "user_2clerk_456",
        "title": "Welcome Contender",
        "message": "Welcome to Ametheus",
        "is_read": False,
        "created_at": "2026-10-06T00:00:00Z",
    })
    await mock_db.notifications.insert_one({
        "_id": "notif_2",
        "user_id": "mongo_user_123",
        "title": "Match Scheduled",
        "message": "Match ready",
        "is_read": False,
        "created_at": "2026-10-06T00:01:00Z",
    })

    resp = await client.get("/api/v1/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert data["unread_count"] == 2
    assert len(data["items"]) == 2

@pytest.mark.asyncio
async def test_organizer_match_request_lifecycle(client, mock_db):
    """Test match request lifecycle with NEW, UNDER_REVIEW, APPROVED."""
    # 1. Create tournament
    await mock_db.tournaments.insert_one({
        "_id": "tourn_req_1",
        "id": "tourn_req_1",
        "title": "Regional Battle",
    })

    # 2. Organizer submits match request
    req_payload = {
        "tournament_id": "tourn_req_1",
        "title": "Semi Finals A",
        "team_a_name": "Team One",
        "team_b_name": "Team Two",
        "urgency": "HIGH",
    }
    resp = await client.post("/api/v1/organizer/match-requests", json=req_payload)
    assert resp.status_code == 201
    req_id = resp.json()["id"]
    assert resp.json()["status"] == "NEW"

    # 3. Admin updates status to UNDER_REVIEW
    patch_resp = await client.patch(f"/api/v1/admin/match-requests/{req_id}", json={
        "status": "UNDER_REVIEW",
        "admin_notes": "Checking broadcast stream schedule",
    })
    assert patch_resp.status_code == 200
    assert patch_resp.json()["status"] == "UNDER_REVIEW"

    # 4. Filter by UNDER_REVIEW
    filter_resp = await client.get("/api/v1/admin/match-requests?status_filter=UNDER_REVIEW")
    assert filter_resp.status_code == 200
    items = filter_resp.json()
    assert len(items) == 1
    assert items[0]["id"] == req_id
