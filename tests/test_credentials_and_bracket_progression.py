import pytest
import pytest_asyncio
from datetime import datetime, timezone
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport

from app.core.database import get_db, set_database
from app.main import app
from app.models.user import User
from app.api.deps import get_current_user, get_current_user_optional, require_organizer, require_super_admin


@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_progression_test"]
    set_database(db)
    return db


@pytest.fixture
def make_client(mock_db):
    from fastapi import Header, Depends, HTTPException
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
            token = f"token_{user.id}"
            user_map[token] = user
            headers["Authorization"] = f"Bearer {token}"
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test", headers=headers)

    return _create


@pytest.mark.asyncio
async def test_credentials_auto_released_on_live_and_visible_to_participants(mock_db, make_client):
    admin_user = User(id="admin_1", username="admin_boss", email="admin@test.com", role="SUPER_ADMIN")
    player_user = User(id="player_a", username="player_a", email="playera@test.com", role="USER")

    tourn_oid = ObjectId()
    tourn_id = str(tourn_oid)
    match_oid = ObjectId()
    match_id = str(match_oid)

    await mock_db.tournaments.insert_one({
        "_id": tourn_oid,
        "id": tourn_id,
        "title": "Valorant Showdown",
        "slug": "valorant-showdown",
        "status": "LIVE",
        "created_by": "admin_1",
    })

    await mock_db.matches.insert_one({
        "_id": match_oid,
        "id": match_id,
        "tournament_id": tourn_id,
        "round_number": 1,
        "match_number": 1,
        "status": "SCHEDULED",
        "team_a_id": "player_a",
        "team_a_name": "Alpha Contender",
        "team_b_id": "player_b",
        "team_b_name": "Beta Contender",
        "team_ids": ["player_a", "player_b"],
        "room_id": "AMT-ROOM-VAL-01",
        "room_password": "PASS-SECRET-99",
        "credentials_released": False,
        "join_info_enabled": False,
    })

    # Participant client views match before Go Live
    p_client = make_client(player_user)
    res_before = await p_client.get(f"/api/v1/matches/{match_id}")
    assert res_before.status_code == 200
    data_before = res_before.json()
    assert data_before["status"] == "SCHEDULED"
    assert data_before.get("room_id") is None
    assert data_before.get("room_password") is None

    # Organizer goes live with match
    a_client = make_client(admin_user)
    live_res = await a_client.patch(
        f"/api/v1/admin/matches/{match_id}/status",
        json={"status": "LIVE"}
    )
    assert live_res.status_code == 200

    # Verify database persistence
    db_match = await mock_db.matches.find_one({"_id": match_oid})
    assert db_match["status"] == "LIVE"
    assert db_match["credentials_released"] is True
    assert db_match["room_id"] == "AMT-ROOM-VAL-01"
    assert db_match["room_password"] == "PASS-SECRET-99"

    # Authorized participant refreshes and now receives credentials immediately
    res_after = await p_client.get(f"/api/v1/matches/{match_id}")
    assert res_after.status_code == 200
    data_after = res_after.json()
    assert data_after["status"] == "LIVE"
    assert data_after["room_id"] == "AMT-ROOM-VAL-01"
    assert data_after["room_password"] == "PASS-SECRET-99"


@pytest.mark.asyncio
async def test_winner_selection_earlier_rounds_with_objectid(mock_db, make_client):
    admin_user = User(id="admin_1", username="admin_boss", email="admin@test.com", role="SUPER_ADMIN")

    tourn_oid = ObjectId()
    tourn_id = str(tourn_oid)
    match_oid = ObjectId()
    match_id = str(match_oid)

    await mock_db.tournaments.insert_one({
        "_id": tourn_oid,
        "id": tourn_id,
        "title": "Apex Legends Cup",
        "total_rounds": 3,
        "status": "LIVE",
    })

    await mock_db.matches.insert_one({
        "_id": match_oid,
        "id": match_id,
        "tournament_id": tourn_id,
        "round_number": 1,
        "match_number": 1,
        "round_name": "Round 1",
        "status": "LIVE",
        "team_a_id": "team_alpha",
        "team_a_name": "Team Alpha",
        "team_b_id": "team_beta",
        "team_b_name": "Team Beta",
        "team_ids": ["team_alpha", "team_beta"],
    })

    a_client = make_client(admin_user)

    # Attempt to complete with invalid non-participant
    inv_res = await a_client.post(
        f"/api/v1/admin/matches/{match_id}/complete",
        json={"winner_team_id": "team_charlie", "score_a": 2, "score_b": 0}
    )
    assert inv_res.status_code == 400

    # Complete with valid participant
    comp_res = await a_client.post(
        f"/api/v1/admin/matches/{match_id}/complete",
        json={"winner_team_id": "team_alpha", "score_a": 2, "score_b": 0}
    )
    assert comp_res.status_code == 200

    # Verify match is completed
    m_doc = await mock_db.matches.find_one({"_id": match_oid})
    assert m_doc["status"] == "COMPLETED"
    assert m_doc["winner_team_id"] == "team_alpha"

    # Verify qualification is recorded
    qual_doc = await mock_db.tournament_qualifications.find_one({
        "tournament_id": tourn_id,
        "participant_id": "team_alpha",
    })
    assert qual_doc is not None
    assert qual_doc["status"] == "QUALIFIED"

    # Verify loser is recorded as ELIMINATED
    elim_doc = await mock_db.tournament_qualifications.find_one({
        "tournament_id": tourn_id,
        "participant_id": "team_beta",
    })
    assert elim_doc is not None
    assert elim_doc["status"] == "ELIMINATED"

    # Strict Rule: Preliminary/intermediate rounds must NEVER complete the whole tournament
    t_doc = await mock_db.tournaments.find_one({"_id": tourn_oid})
    assert t_doc["status"] == "LIVE"


@pytest.mark.asyncio
async def test_qualified_contenders_excludes_eliminated_and_warns_on_pending(mock_db, make_client):
    admin_user = User(id="admin_1", username="admin_boss", email="admin@test.com", role="SUPER_ADMIN")

    tourn_oid = ObjectId()
    tourn_id = str(tourn_oid)

    await mock_db.tournaments.insert_one({
        "_id": tourn_oid,
        "id": tourn_id,
        "title": "Championship 2026",
        "total_rounds": 2,
        "status": "LIVE",
    })

    # R1 Match 1 completed: Alpha beat Beta
    m1_oid = ObjectId()
    await mock_db.matches.insert_one({
        "_id": m1_oid,
        "id": str(m1_oid),
        "tournament_id": tourn_id,
        "round_number": 1,
        "match_number": 1,
        "status": "COMPLETED",
        "team_a_id": "team_alpha",
        "team_a_name": "Team Alpha",
        "team_b_id": "team_beta",
        "team_b_name": "Team Beta",
        "winner_team_id": "team_alpha",
    })

    # R1 Match 2 still in progress / pending
    m2_oid = ObjectId()
    await mock_db.matches.insert_one({
        "_id": m2_oid,
        "id": str(m2_oid),
        "tournament_id": tourn_id,
        "round_number": 1,
        "match_number": 2,
        "status": "LIVE",
        "team_a_id": "team_gamma",
        "team_a_name": "Team Gamma",
        "team_b_id": "team_delta",
        "team_b_name": "Team Delta",
    })

    # Record qualifications
    await mock_db.tournament_qualifications.insert_one({
        "tournament_id": tourn_id,
        "team_id": "team_alpha",
        "team_name": "Team Alpha",
        "status": "QUALIFIED",
        "round_number": 1,
    })
    await mock_db.tournament_qualifications.insert_one({
        "tournament_id": tourn_id,
        "team_id": "team_beta",
        "team_name": "Team Beta",
        "status": "ELIMINATED",
        "round_number": 1,
    })

    a_client = make_client(admin_user)
    res = await a_client.get(f"/api/v1/admin/tournaments/{tourn_id}/qualified-contenders?round_number=2")
    assert res.status_code == 200
    data = res.json()

    # Must warn about pending match in round 1
    assert data.get("has_pending_matches") is True
    assert len(data.get("pending_matches", [])) == 1
    assert data.get("is_previous_round_complete") is False

    # Contenders list must only carry qualified winners
    c_ids = [c["team_id"] for c in data.get("contenders", [])]
    assert "team_alpha" in c_ids
    assert "team_beta" not in c_ids

    # Eliminated list includes team_beta
    assert "team_beta" in data.get("eliminated_contenders", [])
