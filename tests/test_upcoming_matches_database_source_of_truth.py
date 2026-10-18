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
from app.services.live_match_service import sync_live_match_for_tournament, reconcile_all_live_matches, ensure_live_match_indexes
from app.api.deps import get_current_user, get_current_user_optional, require_organizer, require_super_admin


@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_upcoming_test"]
    set_database(db)
    await ensure_live_match_indexes(db)
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
async def test_tournament_zero_matches_returns_empty_list_no_dummy_cards(mock_db, make_client):
    """
    Requirements 1 & 2:
    Tournaments with zero matches in db.matches must NOT fabricate dummy upcoming match cards.
    GET /api/v1/matches must return an empty list (clean empty state).
    """
    await mock_db.tournaments.delete_many({})
    await mock_db.matches.delete_many({})
    await mock_db.live_matches.delete_many({})

    # Create 4 tournaments (like Valorant Qualifiers, Free Fire Qualifiers, COD Mobile Qualifiers, BGMI Qualifiers)
    t1 = generate_uuid()
    t2 = generate_uuid()
    t3 = generate_uuid()
    t4 = generate_uuid()

    for tid, title, slug, gslug in [
        (t1, "Valorant Qualifiers", "val-qualifiers", "valorant"),
        (t2, "Free Fire Qualifiers", "ff-qualifiers", "free-fire"),
        (t3, "COD Mobile Qualifiers", "codm-qualifiers", "codm"),
        (t4, "BGMI Qualifiers", "bgmi-qualifiers", "bgmi"),
    ]:
        await mock_db.tournaments.insert_one({
            "_id": tid,
            "id": tid,
            "title": title,
            "slug": slug,
            "game_slug": gslug,
            "game_name": title.split()[0],
            "status": "UPCOMING",
            "format": "Single Elimination",
            "series_format": "BO1",
        })

    # Run full reconciliation
    reconciled = await reconcile_all_live_matches(mock_db)
    assert reconciled == 0

    # Verify db.live_matches has 0 documents
    lm_count = await mock_db.live_matches.count_documents({})
    assert lm_count == 0

    client = make_client()
    # Query all matches
    resp_all = await client.get("/api/v1/matches")
    assert resp_all.status_code == 200
    assert resp_all.json() == []

    # Query upcoming matches
    resp_up = await client.get("/api/v1/matches?status=UPCOMING")
    assert resp_up.status_code == 200
    assert resp_up.json() == []


@pytest.mark.asyncio
async def test_genuine_persisted_match_returns_actual_database_id(mock_db, make_client):
    """
    Requirement 2 & 3:
    When a genuine match is persisted in db.matches:
    - It is retrieved by GET /api/v1/matches
    - Its card ID is the actual database record ID (not a synthetic live_ ID)
    - GET /api/v1/matches/{id} retrieves the actual record
    """
    await mock_db.tournaments.delete_many({})
    await mock_db.matches.delete_many({})
    await mock_db.live_matches.delete_many({})

    tid = generate_uuid()
    await mock_db.tournaments.insert_one({
        "_id": tid,
        "id": tid,
        "title": "Valorant Champions Cup",
        "slug": "val-champions-cup",
        "game_slug": "valorant",
        "game_name": "Valorant",
        "status": "LIVE",
        "format": "Knockout",
        "series_format": "BO3",
    })

    real_mid = generate_uuid()
    await mock_db.matches.insert_one({
        "_id": real_mid,
        "id": real_mid,
        "tournament_id": tid,
        "match_number": 1,
        "round_number": 1,
        "round_name": "Quarterfinals",
        "stage": "Match 01",
        "format": "BO3",
        "series_format": "BO3",
        "team_a_name": "Team Sol",
        "team_b_name": "Team Luna",
        "status": "SCHEDULED",
        "roster_status": "CONFIRMED",
        "scheduled_time": "18:00 IST",
    })

    synced = await sync_live_match_for_tournament(tid, mock_db)
    assert synced is not None
    assert synced["current_encounter_id"] == real_mid

    client = make_client()
    resp = await client.get("/api/v1/matches")
    assert resp.status_code == 200
    cards = resp.json()
    assert len(cards) == 1
    # Card ID MUST be the actual database record ID
    assert cards[0]["id"] == real_mid
    assert cards[0]["current_encounter_id"] == real_mid
    assert cards[0]["tournament_id"] == tid
    assert cards[0]["team_a"]["name"] == "Team Sol"
    assert cards[0]["team_b"]["name"] == "Team Luna"

    # Detail API must resolve the real match
    detail_resp = await client.get(f"/api/v1/matches/{real_mid}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["id"] == real_mid


@pytest.mark.asyncio
async def test_valid_placeholder_match_in_database_is_respected(mock_db, make_client):
    """
    Requirement 6:
    If a real match in the database has no participants assigned yet (e.g. waiting for competitor seeding),
    displaying 'Participants TBD' is acceptable only if that match record genuinely exists in the database.
    """
    await mock_db.tournaments.delete_many({})
    await mock_db.matches.delete_many({})
    await mock_db.live_matches.delete_many({})

    tid = generate_uuid()
    await mock_db.tournaments.insert_one({
        "_id": tid,
        "id": tid,
        "title": "Erangel Open",
        "slug": "erangel-open",
        "game_slug": "bgmi",
        "game_name": "BGMI",
        "status": "UPCOMING",
        "format": "Knockout",
    })

    # Real match record awaiting competitor seeding
    placeholder_mid = generate_uuid()
    await mock_db.matches.insert_one({
        "_id": placeholder_mid,
        "id": placeholder_mid,
        "tournament_id": tid,
        "match_number": 1,
        "round_number": 1,
        "round_name": "Round 1",
        "stage": "Match 01",
        "format": "BO1",
        "team_a_id": None,
        "team_b_id": None,
        "team_a_name": None,
        "team_b_name": None,
        "teams": [],
        "team_ids": [],
        "status": "SCHEDULED",
        "roster_status": "CONFIRMED",
        "scheduled_time": "20:00 IST",
    })

    await sync_live_match_for_tournament(tid, mock_db)

    client = make_client()
    resp = await client.get("/api/v1/matches")
    assert resp.status_code == 200
    cards = resp.json()
    assert len(cards) == 1
    assert cards[0]["id"] == placeholder_mid
    assert cards[0]["team_a"] is None
    assert cards[0]["team_b"] is None
    assert cards[0]["current_round_name"] == "Round 1"


@pytest.mark.asyncio
async def test_exclude_cancelled_and_deleted_matches(mock_db, make_client):
    """
    Requirement 2 & 3:
    Exclude deleted, cancelled or phantom matches from public upcoming matches.
    """
    await mock_db.tournaments.delete_many({})
    await mock_db.matches.delete_many({})
    await mock_db.live_matches.delete_many({})

    tid = generate_uuid()
    await mock_db.tournaments.insert_one({
        "_id": tid,
        "id": tid,
        "title": "Cancelled Cup",
        "slug": "cancelled-cup",
        "game_slug": "valorant",
        "status": "CANCELLED",
    })

    mid = generate_uuid()
    await mock_db.matches.insert_one({
        "_id": mid,
        "id": mid,
        "tournament_id": tid,
        "status": "CANCELLED",
        "is_deleted": True,
    })

    res = await sync_live_match_for_tournament(tid, mock_db)
    assert res is None

    client = make_client()
    resp = await client.get("/api/v1/matches")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_non_existent_match_detail_raises_404_no_dummy_fallback(mock_db, make_client):
    """
    Requirement 5 & 2:
    GET /api/v1/matches/{id} for non-existent match must raise 404, never fabricate a fallback match.
    """
    client = make_client()
    non_existent_id = generate_uuid()
    resp = await client.get(f"/api/v1/matches/{non_existent_id}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Match not found"
