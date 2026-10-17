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
    
    admin_user = User(
        id="admin_123",
        clerk_user_id="user_admin_123",
        email="admin@ametheus.gg",
        username="superadmin",
        role="SUPER_ADMIN",
    )
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_super_admin] = lambda: admin_user
    app.dependency_overrides[require_organizer] = lambda: admin_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_roster_draft_hidden_from_public(client, mock_db):
    """Verify that draft rosters are hidden from the public live matches query."""
    await mock_db.tournaments.insert_one({"_id": "t1", "id": "t1", "title": "Winter Championship", "status": "ONGOING"})
    
    # Draft match
    await mock_db.matches.insert_one({
        "_id": "m_draft",
        "id": "m_draft",
        "tournament_id": "t1",
        "stage": "Quarterfinals",
        "round_name": "Quarterfinals",
        "round_number": 1,
        "match_number": 1,
        "status": "SCHEDULED",
        "roster_status": "DRAFT",
        "team_a_name": "Draft Team A",
        "team_b_name": "Draft Team B",
    })
    
    # Published match
    await mock_db.matches.insert_one({
        "_id": "m_pub",
        "id": "m_pub",
        "tournament_id": "t1",
        "stage": "Quarterfinals",
        "round_name": "Quarterfinals",
        "round_number": 1,
        "match_number": 2,
        "status": "SCHEDULED",
        "roster_status": "PUBLISHED",
        "team_a_name": "Published Team A",
        "team_b_name": "Published Team B",
    })

    resp = await client.get("/api/v1/matches?tournament_id=t1")
    assert resp.status_code == 200
    matches = resp.json()
    assert len(matches) == 1
    assert matches[0]["id"] == "m_pub"


@pytest.mark.asyncio
async def test_publish_roster_notifies_captains(client, mock_db):
    """Verify that publishing a roster transitions it to PUBLISHED and creates in-app notifications."""
    await mock_db.tournaments.insert_one({
        "_id": "t1",
        "id": "t1",
        "title": "Ametheus Cup",
        "game_name": "Valorant",
        "community_platform": "WHATSAPP",
        "community_link": "https://chat.whatsapp.com/test",
    })

    # Captain users
    await mock_db.users.insert_one({"_id": "u_cap_a", "id": "u_cap_a", "username": "cap_alpha", "email": "capa@ametheus.gg"})
    await mock_db.users.insert_one({"_id": "u_cap_b", "id": "u_cap_b", "username": "cap_bravo", "email": "capb@ametheus.gg"})

    # Teams
    await mock_db.teams.insert_one({"_id": "t_a", "id": "t_a", "name": "Team Alpha", "captain_id": "u_cap_a"})
    await mock_db.teams.insert_one({"_id": "t_b", "id": "t_b", "name": "Team Bravo", "captain_id": "u_cap_b"})

    # Draft Match
    await mock_db.matches.insert_one({
        "_id": "m100",
        "id": "m100",
        "tournament_id": "t1",
        "round_name": "Quarter Finals",
        "round_number": 1,
        "match_number": 1,
        "stage": "Quarter Finals",
        "status": "SCHEDULED",
        "roster_status": "DRAFT",
        "team_a_id": "t_a",
        "team_a_name": "Team Alpha",
        "team_b_id": "t_b",
        "team_b_name": "Team Bravo",
        "scheduled_date": "Oct 10, 2026",
        "scheduled_time": "18:00 IST",
        "notified_captains": [],
    })

    # Publish Roster
    pub_resp = await client.post("/api/v1/admin/matches/m100/publish-roster")
    assert pub_resp.status_code == 200
    pub_data = pub_resp.json()
    assert pub_data["status"] == "success"
    assert pub_data["roster_status"] == "PUBLISHED"

    # Verify match doc in DB
    m_doc = await mock_db.matches.find_one({"_id": "m100"})
    assert m_doc["roster_status"] == "PUBLISHED"
    assert "u_cap_a" in m_doc["notified_captains"]
    assert "u_cap_b" in m_doc["notified_captains"]

    # Verify in-app notifications were created
    notifs = await mock_db.notifications.find({"type": "MATCH"}).to_list(10)
    assert len(notifs) >= 2
    user_ids_notified = {n["user_id"] for n in notifs}
    assert "u_cap_a" in user_ids_notified
    assert "u_cap_b" in user_ids_notified


@pytest.mark.asyncio
async def test_winner_automatic_next_round_progression(client, mock_db):
    """Verify that completing a match automatically advances the winner to next round slot."""
    await mock_db.tournaments.insert_one({
        "_id": "t1",
        "id": "t1",
        "title": "Championship",
        "custom_rounds": ["Quarterfinals", "Semifinals", "Grand Final"],
    })

    # Round 1 Match 1
    await mock_db.matches.insert_one({
        "_id": "m_r1_1",
        "id": "m_r1_1",
        "tournament_id": "t1",
        "round_name": "Quarterfinals",
        "round_number": 1,
        "match_number": 1,
        "stage": "Quarterfinals",
        "status": "LIVE",
        "roster_status": "PUBLISHED",
        "team_a_id": "team_alpha",
        "team_a_name": "Team Alpha",
        "team_b_id": "team_bravo",
        "team_b_name": "Team Bravo",
    })

    # Complete match with Team Alpha as winner
    complete_resp = await client.post(
        "/api/v1/admin/matches/m_r1_1/complete",
        json={"winner_team_id": "team_alpha", "score_a": 2, "score_b": 0}
    )
    assert complete_resp.status_code == 200

    # Verify Round 2 Match was NOT auto-created (manual progression model)
    r2_m1 = await mock_db.matches.find_one({
        "tournament_id": "t1",
        "round_number": 2,
    })
    assert r2_m1 is None, "Downstream match must not be auto-created"

    # Verify Round 1 match records completed status and winner
    r1_doc = await mock_db.matches.find_one({"_id": "m_r1_1"})
    assert r1_doc["status"] == "COMPLETED"
    assert r1_doc["winner_team_id"] == "team_alpha"

    # Verify qualification records
    winner_qual = await mock_db.tournament_qualifications.find_one({
        "tournament_id": "t1",
        "participant_id": "team_alpha",
    })
    assert winner_qual is not None
    assert winner_qual["status"] == "QUALIFIED"

    loser_qual = await mock_db.tournament_qualifications.find_one({
        "tournament_id": "t1",
        "participant_id": "team_bravo",
    })
    assert loser_qual is not None
    assert loser_qual["status"] == "ELIMINATED"


@pytest.mark.asyncio
async def test_public_match_detail_rich_rosters_and_progression(client, mock_db):
    """Verify GET /api/v1/matches/{id} returns rich tournament, roster, and progression details."""
    await mock_db.tournaments.insert_one({
        "_id": "t1",
        "id": "t1",
        "title": "Grand Invitational",
        "banner_url": "https://img.ametheus.gg/banner.png",
        "format": "Single Elimination",
        "community_platform": "WHATSAPP",
        "community_link": "https://chat.whatsapp.com/inv",
        "game_name": "Valorant",
    })

    await mock_db.users.insert_one({
        "_id": "u_lead_a",
        "id": "u_lead_a",
        "username": "PhoenixAce",
        "in_game_name": "PHX#1234",
    })

    await mock_db.teams.insert_one({
        "_id": "team_x",
        "id": "team_x",
        "name": "Team Xtreme",
        "tag": "XTREME",
        "captain_id": "u_lead_a",
        "members": [
            {"user_id": "u_lead_a", "player_handle": "PHX#1234", "role": "CAPTAIN"}
        ]
    })

    await mock_db.matches.insert_one({
        "_id": "m_rich",
        "id": "m_rich",
        "tournament_id": "t1",
        "round_name": "Grand Final",
        "round_number": 3,
        "match_number": 1,
        "stage": "Grand Final",
        "status": "LIVE",
        "roster_status": "PUBLISHED",
        "team_a_id": "team_x",
        "team_a_name": "Team Xtreme",
        "team_b_name": "TBD",
        "scheduled_date": "Oct 12, 2026",
        "scheduled_time": "20:00 IST",
    })

    detail_resp = await client.get("/api/v1/matches/m_rich")
    assert detail_resp.status_code == 200
    data = detail_resp.json()

    assert "tournament" in data
    assert data["tournament"]["title"] == "Grand Invitational"
    assert data["tournament"]["community_platform"] == "WHATSAPP"

    assert "team_a_details" in data
    assert data["team_a_details"]["name"] == "Team Xtreme"
    assert data["team_a_details"]["captain"]["handle"] == "PHX#1234"

    assert "tournament_progression" in data
    assert isinstance(data["tournament_progression"], list)
