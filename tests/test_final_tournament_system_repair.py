import pytest
import pytest_asyncio
import asyncio
from datetime import datetime, timezone
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI, Depends, Header, HTTPException

from app.core.database import get_db, set_database
from app.main import app
from app.models.base import generate_uuid
from app.models.tournament import Tournament
from app.models.match import Match
from app.models.user import User
from app.services.bracket_engine import generate_tournament_bracket
from app.services.battle_royale_engine import get_battle_royale_specs
from app.api.deps import get_current_user, get_current_user_optional, require_organizer, require_super_admin


@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_final_repair_test"]
    set_database(db)
    return db


@pytest.fixture
def make_client(mock_db):
    user_map = {}
    app.dependency_overrides[get_db] = lambda: mock_db

    async def _dynamic_current_user(authorization: str = Header(None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Unauthorized")
        token = authorization.replace("Bearer ", "").strip()
        if token in user_map:
            return user_map[token]
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def _dynamic_optional_user(authorization: str = Header(None)):
        if not authorization or not authorization.startswith("Bearer "):
            return None
        token = authorization.replace("Bearer ", "").strip()
        return user_map.get(token)

    async def _dynamic_require_organizer(current_user: User = Depends(_dynamic_current_user)):
        if current_user.role in ["ORGANIZER", "SUPER_ADMIN", "ADMIN"]:
            return current_user
        raise HTTPException(status_code=403, detail="Forbidden: Organizer required")

    async def _dynamic_require_super_admin(current_user: User = Depends(_dynamic_current_user)):
        if current_user.role in ["SUPER_ADMIN", "ADMIN"]:
            return current_user
        raise HTTPException(status_code=403, detail="Forbidden: Admin required")

    app.dependency_overrides[get_current_user] = _dynamic_current_user
    app.dependency_overrides[get_current_user_optional] = _dynamic_optional_user
    app.dependency_overrides[require_organizer] = _dynamic_require_organizer
    app.dependency_overrides[require_super_admin] = _dynamic_require_super_admin

    def _create(user: User = None):
        headers = {}
        if user:
            user_map[user.id] = user
            headers["Authorization"] = f"Bearer {user.id}"
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test", headers=headers)

    return _create


@pytest.mark.asyncio
async def test_01_solo_registration_increments_capacity_by_1_and_team_id_is_none(mock_db, make_client):
    """
    Scenario 1: Solo registration increments capacity by 1, and team_id is None in the registration document.
    """
    user = User(
        id="user_solo_1",
        email="solo@test.com",
        username="SoloGamer",
        role="USER",
        inr_balance="1000.0",
    )
    await mock_db.users.insert_one(user.to_mongo())

    tourn_id = "tourn_solo_1"
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "slug": "chess-solo-masters",
        "title": "Chess Solo Masters",
        "status": "REGISTRATION_OPEN",
        "registration_type": "SOLO",
        "is_solo": True,
        "match_structure": "KNOCKOUT",
        "game_format": "Solo",
        "entry_fee_inr": 0,
        "max_teams": 32,
        "min_team_size": 1,
        "max_team_size": 1,
    })

    client = make_client(user)
    resp = await client.post(f"/api/v1/tournaments/{tourn_id}/register", json={})
    assert resp.status_code == 200, resp.text

    reg = await mock_db.tournament_registrations.find_one({"tournament_id": tourn_id})
    assert reg is not None
    assert reg.get("player_id") == "user_solo_1"
    assert reg.get("team_id") is None

    # Capacity count is exactly 1
    count = await mock_db.tournament_registrations.count_documents({"tournament_id": tourn_id})
    assert count == 1


@pytest.mark.asyncio
async def test_02_team_registration_5v5_increments_capacity_by_1(mock_db, make_client):
    """
    Scenario 2: Team registration (5v5) increments capacity by 1, representing 1 registered unit.
    """
    captain = User(
        id="user_cap_1",
        email="cap@test.com",
        username="TeamCap",
        role="USER",
        inr_balance="1000.0",
    )
    await mock_db.users.insert_one(captain.to_mongo())

    team_id = "team_squad_5v5"
    await mock_db.teams.insert_one({
        "_id": team_id,
        "id": team_id,
        "name": "Alpha Squad",
        "tag": "ALP",
        "captain_id": captain.id,
        "game_id": "val_game",
        "members": [
            {"user_id": captain.id, "role": "CAPTAIN"},
            {"user_id": "p2", "role": "MEMBER"},
            {"user_id": "p3", "role": "MEMBER"},
            {"user_id": "p4", "role": "MEMBER"},
            {"user_id": "p5", "role": "MEMBER"},
        ],
    })

    tourn_id = "tourn_team_5v5"
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "slug": "val-champions",
        "title": "Valorant Champions 5v5",
        "status": "REGISTRATION_OPEN",
        "registration_type": "TEAM",
        "is_solo": False,
        "match_structure": "KNOCKOUT",
        "game_format": "5v5",
        "game_id": "val_game",
        "entry_fee_inr": 0,
        "max_teams": 16,
        "min_team_size": 5,
        "max_team_size": 5,
    })

    client = make_client(captain)
    resp = await client.post(f"/api/v1/tournaments/{tourn_id}/register", json={"team_id": team_id})
    assert resp.status_code == 200, resp.text

    count = await mock_db.tournament_registrations.count_documents({"tournament_id": tourn_id})
    assert count == 1  # Exactly 1 team registered, not 5


@pytest.mark.asyncio
async def test_03_25_team_battle_royale_produces_one_match_with_25_participants(mock_db, make_client):
    """
    Scenario 3: 25-team Battle Royale produces 1 match with 25 participants.
    """
    organizer = User(id="org_1", email="org@test.com", username="Organizer", role="ORGANIZER")
    await mock_db.users.insert_one(organizer.to_mongo())

    tourn_id = "tourn_br_25"
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "slug": "bgmi-mega-war",
        "title": "BGMI Mega War",
        "status": "REGISTRATION_CLOSED",
        "registration_type": "TEAM",
        "is_solo": False,
        "match_structure": "BATTLE_ROYALE",
        "format": "Battle Royale",
        "tournament_type": "Battle Royale",
        "game_id": "bgmi",
        "room_capacity": 25,
        "max_teams": 30,
        "organizer_id": organizer.id,
    })

    # Insert 25 teams and registrations
    team_ids = []
    for i in range(25):
        tid = f"team_br_{i+1}"
        team_ids.append(tid)
        await mock_db.teams.insert_one({
            "_id": tid,
            "id": tid,
            "name": f"Squad {i+1}",
            "tag": f"SQ{i+1}",
        })
        await mock_db.tournament_registrations.insert_one({
            "_id": f"reg_{i+1}",
            "tournament_id": tourn_id,
            "team_id": tid,
            "payment_status": "PAID",
        })

    client = make_client(organizer)
    resp = await client.post(f"/api/v1/organizer/tournaments/{tourn_id}/generate-brackets")
    assert resp.status_code == 200, resp.text

    matches = await mock_db.matches.find({"tournament_id": tourn_id}).to_list(length=10)
    assert len(matches) == 1
    m = matches[0]
    assert len(m.get("team_ids", [])) == 25
    assert len(m.get("teams", [])) == 25


@pytest.mark.asyncio
async def test_04_30_team_battle_royale_room_cap_25_produces_two_multi_participant_matches(mock_db, make_client):
    """
    Scenario 4: 30-team Battle Royale with room capacity 25 produces 2 multi-participant matches.
    """
    organizer = User(id="org_2", email="org2@test.com", username="Organizer2", role="ORGANIZER")
    await mock_db.users.insert_one(organizer.to_mongo())

    tourn_id = "tourn_br_30"
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "slug": "free-fire-cup",
        "title": "Free Fire Cup",
        "status": "REGISTRATION_CLOSED",
        "registration_type": "TEAM",
        "is_solo": False,
        "match_structure": "BATTLE_ROYALE",
        "format": "Battle Royale",
        "tournament_type": "Battle Royale",
        "game_id": "free_fire",
        "room_capacity": 25,
        "max_teams": 50,
        "organizer_id": organizer.id,
    })

    # Insert 30 registrations
    for i in range(30):
        tid = f"team_ff_{i+1}"
        await mock_db.teams.insert_one({
            "_id": tid,
            "id": tid,
            "name": f"FF Squad {i+1}",
            "tag": f"FF{i+1}",
        })
        await mock_db.tournament_registrations.insert_one({
            "_id": f"reg_ff_{i+1}",
            "tournament_id": tourn_id,
            "team_id": tid,
            "payment_status": "PAID",
        })

    client = make_client(organizer)
    resp = await client.post(f"/api/v1/organizer/tournaments/{tourn_id}/generate-brackets")
    assert resp.status_code == 200, resp.text

    matches = await mock_db.matches.find({"tournament_id": tourn_id}).to_list(length=10)
    assert len(matches) == 2
    # Verify both matches are multi-participant and partition 30 teams (e.g. 15 and 15)
    total_assigned = sum(len(m.get("team_ids", [])) for m in matches)
    assert total_assigned == 30
    for m in matches:
        assert len(m.get("team_ids", [])) >= 10


@pytest.mark.asyncio
async def test_05_knockout_bracket_generates_pairwise_matches(mock_db, make_client):
    """
    Scenario 5: Knockout bracket generates pairwise matches.
    """
    organizer = User(id="org_3", email="org3@test.com", username="Organizer3", role="ORGANIZER")
    await mock_db.users.insert_one(organizer.to_mongo())

    tourn_id = "tourn_ko_8"
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "slug": "val-knockout-8",
        "title": "Valorant Knockout 8",
        "status": "REGISTRATION_CLOSED",
        "registration_type": "TEAM",
        "is_solo": False,
        "match_structure": "KNOCKOUT",
        "format": "Single Elimination",
        "tournament_type": "Single Elimination",
        "organizer_id": organizer.id,
    })

    for i in range(8):
        tid = f"team_ko_{i+1}"
        await mock_db.teams.insert_one({
            "_id": tid,
            "id": tid,
            "name": f"KO Team {i+1}",
            "tag": f"K{i+1}",
        })
        await mock_db.tournament_registrations.insert_one({
            "_id": f"reg_ko_{i+1}",
            "tournament_id": tourn_id,
            "team_id": tid,
            "payment_status": "PAID",
        })

    client = make_client(organizer)
    resp = await client.post(f"/api/v1/organizer/tournaments/{tourn_id}/generate-brackets")
    assert resp.status_code == 200, resp.text

    matches = await mock_db.matches.find({"tournament_id": tourn_id}).to_list(length=10)
    # Round 1 of 8 teams must have 4 pairwise matches
    round_1_matches = [m for m in matches if m.get("round_number") == 1]
    assert len(round_1_matches) == 4
    for m in round_1_matches:
        assert m.get("team_a_id") is not None
        assert m.get("team_b_id") is not None
        assert m.get("team_a_name") is not None
        assert m.get("team_b_name") is not None


@pytest.mark.asyncio
async def test_06_competitor_hub_releases_credentials_to_authorized_participant(mock_db, make_client):
    """
    Scenario 6: Competitor Hub releases credentials to authorized participant when credentials_released is true.
    """
    user = User(id="user_auth_1", email="auth@test.com", username="AuthPlayer", role="USER")
    await mock_db.users.insert_one(user.to_mongo())

    tourn_id = "tourn_creds_test"
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "slug": "creds-tourn",
        "title": "Creds Tournament",
        "status": "LIVE",
    })

    team_id = "team_auth_1"
    await mock_db.teams.insert_one({
        "_id": team_id,
        "id": team_id,
        "name": "Auth Squad",
        "captain_id": user.id,
        "members": [{"user_id": user.id}],
    })

    await mock_db.tournament_registrations.insert_one({
        "_id": "reg_auth_1",
        "tournament_id": tourn_id,
        "team_id": team_id,
        "payment_status": "PAID",
    })

    mid = "match_creds_1"
    await mock_db.matches.insert_one({
        "_id": mid,
        "id": mid,
        "tournament_id": tourn_id,
        "team_ids": [team_id, "other_team"],
        "team_a_id": team_id,
        "team_b_id": "other_team",
        "credentials_released": True,
        "room_id": "ROOM_AUTH_999",
        "room_password": "SECRET_PASS_999",
        "custom_instructions": "Join room within 10 minutes",
        "status": "LIVE",
    })

    client = make_client(user)
    resp = await client.get(f"/api/v1/tournaments/{tourn_id}/matches")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 1
    m = data[0]
    assert m["credentials_released"] is True
    assert m["room_id"] == "ROOM_AUTH_999"
    assert m["room_password"] == "SECRET_PASS_999"
    assert m["custom_instructions"] == "Join room within 10 minutes"


@pytest.mark.asyncio
async def test_07_unreleased_credentials_remain_hidden(mock_db, make_client):
    """
    Scenario 7: Unreleased credentials remain hidden (None) even for registered participant.
    """
    user = User(id="user_auth_2", email="auth2@test.com", username="AuthPlayer2", role="USER")
    await mock_db.users.insert_one(user.to_mongo())

    tourn_id = "tourn_creds_unrel"
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "slug": "creds-unrel",
        "title": "Creds Unreleased Tournament",
        "status": "REGISTRATION_CLOSED",
    })

    team_id = "team_auth_2"
    await mock_db.teams.insert_one({
        "_id": team_id,
        "id": team_id,
        "name": "Auth Squad 2",
        "captain_id": user.id,
        "members": [{"user_id": user.id}],
    })

    await mock_db.tournament_registrations.insert_one({
        "_id": "reg_auth_2",
        "tournament_id": tourn_id,
        "team_id": team_id,
        "payment_status": "PAID",
    })

    mid = "match_creds_2"
    await mock_db.matches.insert_one({
        "_id": mid,
        "id": mid,
        "tournament_id": tourn_id,
        "team_ids": [team_id, "other_team"],
        "team_a_id": team_id,
        "team_b_id": "other_team",
        "credentials_released": False,
        "room_id": "SECRET_ROOM",
        "room_password": "HIDDEN_PASSWORD",
        "status": "SCHEDULED",
    })

    client = make_client(user)
    resp = await client.get(f"/api/v1/tournaments/{tourn_id}/matches")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 1
    m = data[0]
    assert m["credentials_released"] is False
    assert m["room_id"] is None
    assert m["room_password"] is None


@pytest.mark.asyncio
async def test_08_non_registered_user_sees_unreleased_credentials_hidden(mock_db, make_client):
    """
    Scenario 8: Non-registered user sees credentials hidden even if credentials_released is True.
    """
    stranger = User(id="user_stranger", email="stranger@test.com", username="Stranger", role="USER")
    await mock_db.users.insert_one(stranger.to_mongo())

    tourn_id = "tourn_creds_stranger"
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "slug": "creds-stranger",
        "title": "Creds Stranger Tournament",
        "status": "LIVE",
    })

    mid = "match_creds_3"
    await mock_db.matches.insert_one({
        "_id": mid,
        "id": mid,
        "tournament_id": tourn_id,
        "team_ids": ["team_a", "team_b"],
        "team_a_id": "team_a",
        "team_b_id": "team_b",
        "credentials_released": True,
        "room_id": "TOP_SECRET_ROOM",
        "room_password": "TOP_SECRET_PASS",
        "status": "LIVE",
    })

    client = make_client(stranger)
    resp = await client.get(f"/api/v1/tournaments/{tourn_id}/matches")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 1
    m = data[0]
    # Stranger is not in match, room_id and room_password MUST be None
    assert m["room_id"] is None
    assert m["room_password"] is None


@pytest.mark.asyncio
async def test_09_check_in_marks_status_without_starting_match_prematurely(mock_db, make_client):
    """
    Scenario 9: Check-in marks status without starting the match prematurely.
    """
    captain = User(id="user_cap_checkin", email="checkin@test.com", username="CheckInCap", role="USER")
    await mock_db.users.insert_one(captain.to_mongo())

    tourn_id = "tourn_checkin_test"
    mid = "match_checkin_1"
    team_id = "team_checkin_1"

    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "title": "Checkin Tournament",
        "status": "IN_PROGRESS",
    })
    await mock_db.teams.insert_one({
        "_id": team_id,
        "id": team_id,
        "name": "Checkin Squad",
        "captain_id": captain.id,
        "members": [{"user_id": captain.id}],
    })
    await mock_db.matches.insert_one({
        "_id": mid,
        "id": mid,
        "tournament_id": tourn_id,
        "team_ids": [team_id, "other_team"],
        "team_a_id": team_id,
        "team_b_id": "other_team",
        "status": "CHECK_IN_OPEN",
        "check_in_status": {},
    })

    client = make_client(captain)
    resp = await client.post(f"/api/v1/matches/{mid}/check-in", json={"team_id": team_id})
    assert resp.status_code == 200, resp.text

    m_after = await mock_db.matches.find_one({"_id": mid})
    assert m_after is not None
    # Team check in must be recorded
    check_ins = m_after.get("check_in_status", {})
    assert check_ins.get(team_id) is True or check_ins.get(team_id) == "CHECKED_IN" or any(ci.get("team_id") == team_id for ci in m_after.get("check_ins", []))
    # Match must NOT have transitioned to LIVE or COMPLETED automatically
    assert m_after["status"] in ["CHECK_IN_OPEN", "SCHEDULED"]


@pytest.mark.asyncio
async def test_10_battle_royale_qualifier_selection_advances_multiple_winners(mock_db, make_client):
    """
    Scenario 10: Battle Royale qualifier selection advances multiple winners and marks eliminated teams.
    """
    organizer = User(id="org_br_qual", email="orgqual@test.com", username="OrgQual", role="ORGANIZER")
    await mock_db.users.insert_one(organizer.to_mongo())

    tourn_id = "tourn_br_qual"
    mid = "match_br_qual_1"

    all_teams = [f"team_{i+1}" for i in range(25)]
    qualifiers = all_teams[:10]  # Top 10 qualify

    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "id": tourn_id,
        "title": "BR Qualifiers Tourn",
        "status": "LIVE",
        "organizer_id": organizer.id,
    })

    await mock_db.matches.insert_one({
        "_id": mid,
        "id": mid,
        "tournament_id": tourn_id,
        "team_ids": all_teams,
        "status": "LIVE",
        "round_number": 1,
        "is_final_round": False,
    })

    client = make_client(organizer)
    resp = await client.post(
        f"/api/v1/matches/{mid}/qualifiers",
        json={"qualifier_team_ids": qualifiers},
    )
    assert resp.status_code == 200, resp.text
    res_data = resp.json()
    assert res_data["qualifier_team_ids"] == qualifiers
    assert res_data["qualifiers_count"] == 10

    m_doc = await mock_db.matches.find_one({"_id": mid})
    assert m_doc["qualifier_team_ids"] == qualifiers
    assert m_doc["qualifiers_count"] == 10
