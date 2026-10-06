import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.database import get_db
from app.models.user import User
from app.api.deps import get_current_user, require_super_admin
from app.services.bracket_engine import advance_bracket_match
from app.models.match import Match

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
async def test_tournament_start_live_and_complete_lifecycle(client, mock_db):
    """Test organizer and admin transitions from REGISTRATION_CLOSED -> LIVE -> COMPLETED."""
    # 1. Insert a tournament with REGISTRATION_CLOSED status
    tour_doc = {
        "_id": "tour_life_1",
        "id": "tour_life_1",
        "slug": "tour-life-1",
        "title": "Championship 2026",
        "organizer_id": "admin_123",
        "game_id": "valorant",
        "game_slug": "valorant",
        "status": "REGISTRATION_CLOSED",
        "is_active": True,
        "max_teams": 8,
        "registered_teams": 4,
    }
    await mock_db.tournaments.insert_one(tour_doc)

    # 2. Insert two matches associated with this tournament:
    # m1 has both teams populated -> should transition to LIVE
    # m2 has pending/TBD teams -> should remain SCHEDULED until teams are populated
    await mock_db.matches.insert_one({
        "_id": "m1",
        "id": "m1",
        "tournament_id": "tour_life_1",
        "status": "SCHEDULED",
        "round_name": "Round 1",
        "team_a_id": "team_alpha",
        "team_b_id": "team_beta",
    })
    await mock_db.matches.insert_one({
        "_id": "m2",
        "id": "m2",
        "tournament_id": "tour_life_1",
        "status": "SCHEDULED",
        "round_name": "Finals",
        "team_a_id": None,
        "team_b_id": None,
    })

    # 3. Call POST /api/v1/organizer/tournaments/tour_life_1/start-live
    resp = await client.post("/api/v1/organizer/tournaments/tour_life_1/start-live")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "LIVE"

    # Verify DB tournament is LIVE
    updated_t = await mock_db.tournaments.find_one({"_id": "tour_life_1"})
    assert updated_t["status"] == "LIVE"

    # Verify populated match is LIVE and pending match remains SCHEDULED
    m1_doc = await mock_db.matches.find_one({"_id": "m1"})
    assert m1_doc["status"] == "LIVE"

    m2_doc = await mock_db.matches.find_one({"_id": "m2"})
    assert m2_doc["status"] == "SCHEDULED"

    # 4. Call POST /api/v1/organizer/tournaments/tour_life_1/complete
    resp_complete = await client.post("/api/v1/organizer/tournaments/tour_life_1/complete")
    assert resp_complete.status_code == 200
    assert resp_complete.json()["status"] == "COMPLETED"

    completed_t = await mock_db.tournaments.find_one({"_id": "tour_life_1"})
    assert completed_t["status"] == "COMPLETED"

@pytest.mark.asyncio
async def test_admin_start_live_and_complete_endpoints(client, mock_db):
    """Test admin direct start-live and complete endpoints."""
    await mock_db.tournaments.insert_one({
        "_id": "tour_admin_1",
        "id": "tour_admin_1",
        "slug": "tour-admin-1",
        "title": "Admin Cup",
        "organizer_id": "org_999",
        "status": "REGISTRATION_CLOSED",
        "is_active": True,
    })

    # Start live as admin
    resp = await client.post("/api/v1/admin/tournaments/tour_admin_1/start-live")
    assert resp.status_code == 200
    t = await mock_db.tournaments.find_one({"_id": "tour_admin_1"})
    assert t["status"] == "LIVE"

    # Complete as admin
    resp2 = await client.post("/api/v1/admin/tournaments/tour_admin_1/complete")
    assert resp2.status_code == 200
    t2 = await mock_db.tournaments.find_one({"_id": "tour_admin_1"})
    assert t2["status"] == "COMPLETED"

@pytest.mark.asyncio
async def test_team_email_broadcast_and_history(client, mock_db):
    """Test admin broadcasting official email to members of selected teams."""
    # Insert users
    await mock_db.users.insert_one({"_id": "u_cap", "id": "u_cap", "email": "captain@test.com", "display_name": "CaptAlpha"})
    await mock_db.users.insert_one({"_id": "u_mem1", "id": "u_mem1", "email": "member1@test.com", "display_name": "RosterOne"})

    # Insert team
    await mock_db.teams.insert_one({
        "_id": "team_alpha",
        "id": "team_alpha",
        "name": "Team Alpha",
        "tag": "ALP",
        "captain_id": "u_cap",
        "members": [{"user_id": "u_mem1", "role": "MEMBER"}],
    })

    # Dispatch official email to SELECTED_TEAMS
    payload = {
        "recipient_type": "SELECTED_TEAMS",
        "team_ids": ["team_alpha"],
        "subject": "Mandatory Roster Check-In",
        "message": "All roster members must be present in lobby 15 minutes before match.",
    }
    resp = await client.post("/api/v1/admin/email/official/send", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["recipient_count"] == 2
    assert "2 recipient(s)" in data["message"]

    # Verify history
    h_resp = await client.get("/api/v1/admin/email/official/history")
    assert h_resp.status_code == 200
    h_data = h_resp.json()
    assert h_data["total"] >= 1
    assert h_data["items"][0]["recipient_type"] == "SELECTED_TEAMS"
    assert h_data["items"][0]["recipient_count"] == 2

@pytest.mark.asyncio
async def test_bracket_advancement_and_completion(mock_db):
    """Verify that match winner propagates to downstream match via source match slots."""
    # Set up tournament
    await mock_db.tournaments.insert_one({
        "_id": "tour_adv_1",
        "id": "tour_adv_1",
        "status": "LIVE",
    })

    # Completed semi-final match
    match1 = Match(
        id="semi_1",
        tournament_id="tour_adv_1",
        round_name="Semi-Finals",
        status="COMPLETED",
        team_a_id="team_w",
        team_a_name="Winners",
        score_a=2,
        team_b_id="team_l",
        team_b_name="Losers",
        score_b=0,
        winner_team_id="team_w",
        next_match_id="final_1",
    )
    d1 = match1.model_dump(by_alias=True)
    d1["_id"] = match1.id
    await mock_db.matches.insert_one(d1)

    # Downstream final match with slot_a_source_match_id pointing to semi_1
    final_match = Match(
        id="final_1",
        tournament_id="tour_adv_1",
        round_name="Grand Final",
        status="SCHEDULED",
        slot_a_source_match_id="semi_1",
        slot_b_source_match_id="semi_2",
    )
    d2 = final_match.model_dump(by_alias=True)
    d2["_id"] = final_match.id
    await mock_db.matches.insert_one(d2)

    # Advance bracket match
    updated_final = await advance_bracket_match("tour_adv_1", match1, "team_w", mock_db)
    assert updated_final is not None
    assert updated_final.team_a_id == "team_w"
    assert updated_final.team_a_name == "Winners"

    # Verify DB persistence
    final_in_db = await mock_db.matches.find_one({"_id": "final_1"})
    assert final_in_db["team_a_id"] == "team_w"
    assert final_in_db["team_a_name"] == "Winners"


@pytest.mark.asyncio
async def test_all_organizers_email_broadcast(client, mock_db):
    """Test admin broadcasting official email to all registered organizers specifically."""
    # Insert organizers and normal player
    await mock_db.users.insert_one({"_id": "org_1", "id": "org_1", "email": "org1@test.com", "role": "ORGANIZER", "display_name": "OrgOne"})
    await mock_db.users.insert_one({"_id": "org_2", "id": "org_2", "email": "org2@test.com", "role": "ORGANIZER", "display_name": "OrgTwo"})
    await mock_db.users.insert_one({"_id": "player_1", "id": "player_1", "email": "player@test.com", "role": "PLAYER", "display_name": "PlayerOne"})

    # 1. Dispatch using ALL_ORGANIZERS
    payload1 = {
        "recipient_type": "ALL_ORGANIZERS",
        "subject": "Organizer Briefing",
        "message": "Tournament operations protocol update.",
    }
    resp1 = await client.post("/api/v1/admin/email/official/send", json=payload1)
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["recipient_count"] == 2
    assert "2 recipient(s)" in data1["message"]

    # 2. Dispatch using ORGANIZERS alias
    payload2 = {
        "recipient_type": "ORGANIZERS",
        "subject": "Organizer Briefing 2",
        "message": "Protocol update follow-up.",
    }
    resp2 = await client.post("/api/v1/admin/email/official/send", json=payload2)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["recipient_count"] == 2

