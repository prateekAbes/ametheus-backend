import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from bson import ObjectId
from app.core.database import get_db, set_database
from app.main import app
from app.models.user import User
from app.api.deps import get_current_user, get_current_user_optional, require_organizer, require_super_admin


@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_phase1_verification"]
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
        from httpx import AsyncClient, ASGITransport
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers)

    return _create


@pytest.mark.asyncio
async def test_scenario_a_and_b_tournament_formats_and_edit_preservation(mock_db, make_client):
    """
    Scenario A & B:
    - Verify tournament creation with each competition format.
    - Verify Edit Tournament preserves format in database when updating other fields.
    """
    admin = User(id="admin_phase1", username="admin_phase1", email="admin@phase1.gg", role="SUPER_ADMIN")
    client = make_client(admin)

    # Setup game
    game_oid = ObjectId()
    await mock_db.games.insert_one({
        "_id": game_oid,
        "id": "game_val",
        "name": "Valorant",
        "slug": "valorant",
        "is_active": True,
    })

    formats = [
        "Knockout",
        "Battle Royale",
        "Round Robin",
    ]

    for fmt in formats:
        # Create tournament with format
        create_payload = {
            "title": f"Championship {fmt}",
            "description": f"Official Championship for {fmt}",
            "game_id": "game_val",
            "format": fmt,
            "tournament_type": fmt,
            "participation_mode": "SQUAD",
            "max_teams": 16,
            "start_time": "2026-11-01T10:00:00Z",
            "registration_end": "2026-10-31T23:59:59Z",
        }
        res = await client.post("/api/v1/organizer/tournaments", json=create_payload)
        assert res.status_code == 201, res.text
        created_data = res.json()
        tid = created_data["id"]

        # Verify DB persisted format and participation mode
        db_doc = await mock_db.tournaments.find_one({"$or": [{"_id": tid}, {"id": tid}]})
        assert db_doc is not None
        assert db_doc["format"] == fmt
        assert db_doc["participation_mode"] == "SQUAD"

        # Now update tournament via organizer PUT without format field (Edit Tournament form behavior)
        update_payload = {
            "title": f"Updated Championship {fmt}",
            "rules": ["Updated tournament rules"],
            "prize_pool_inr": 50000,
        }
        update_res = await client.put(f"/api/v1/organizer/tournaments/{tid}", json=update_payload)
        assert update_res.status_code == 200, update_res.text

        # Verify format was NOT overwritten or cleared
        updated_db_doc = await mock_db.tournaments.find_one({"$or": [{"_id": tid}, {"id": tid}]})
        assert updated_db_doc["title"] == f"Updated Championship {fmt}"
        assert updated_db_doc["format"] == fmt, f"Format was corrupted for {fmt}!"
        assert updated_db_doc["participation_mode"] == "SQUAD"


@pytest.mark.asyncio
async def test_scenario_c_and_d_matches_endpoints_removed(mock_db, make_client):
    """
    Scenario C & D:
    - Verify /api/v1/matches endpoint is removed (returns 404).
    - Verify /api/v1/admin/matches/live-dashboard endpoint is removed (returns 404).
    """
    admin = User(id="admin_phase1", username="admin_phase1", email="admin@phase1.gg", role="SUPER_ADMIN")
    client = make_client(admin)

    # 1. /api/v1/matches -> 404
    resp_matches = await client.get("/api/v1/matches")
    assert resp_matches.status_code == 404, f"Expected 404, got {resp_matches.status_code}"

    # 2. /api/v1/matches/{id} -> 404
    resp_match_detail = await client.get("/api/v1/matches/some_match_id")
    assert resp_match_detail.status_code == 404, f"Expected 404, got {resp_match_detail.status_code}"

    # 3. /api/v1/admin/matches/live-dashboard endpoint is removed (GET returns 404 or 405)
    resp_live_dash = await client.get("/api/v1/admin/matches/live-dashboard")
    assert resp_live_dash.status_code in [404, 405], f"Expected 404 or 405, got {resp_live_dash.status_code}"


@pytest.mark.asyncio
async def test_scenario_e_tournament_bracket_and_core_flow_intact(mock_db, make_client):
    """
    Scenario E:
    - Tournament detail matches bracket endpoint /tournaments/{slug}/matches remains functional.
    - Tournaments listing remains functional.
    """
    player = User(id="player_phase1", username="player_phase1", email="player@phase1.gg", role="PLAYER")
    client = make_client(player)

    tourn_oid = ObjectId()
    tid = str(tourn_oid)
    await mock_db.tournaments.insert_one({
        "_id": tourn_oid,
        "id": tid,
        "title": "Valorant Open Bracket",
        "slug": "val-open-bracket",
        "status": "LIVE",
        "format": "Single Elimination",
        "participation_mode": "SOLO",
    })

    match_oid = ObjectId()
    await mock_db.matches.insert_one({
        "_id": match_oid,
        "id": str(match_oid),
        "tournament_id": tid,
        "round_number": 1,
        "round_name": "Round 1",
        "match_number": 1,
        "status": "LIVE",
        "team_a_name": "Player 1",
        "team_b_name": "Player 2",
        "roster_status": "PUBLISHED",
    })

    # Tournaments list endpoint
    list_res = await client.get("/api/v1/tournaments")
    assert list_res.status_code == 200
    assert len(list_res.json()) >= 1

    # Tournament detail matches bracket endpoint
    bracket_res = await client.get("/api/v1/tournaments/val-open-bracket/matches")
    assert bracket_res.status_code == 200
    matches = bracket_res.json()
    assert len(matches) == 1
    assert matches[0]["id"] == str(match_oid)
    assert matches[0]["round_name"] == "Round 1"
