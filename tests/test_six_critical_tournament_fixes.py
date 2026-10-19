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
from app.services.live_match_service import sync_live_match_for_tournament


@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_six_fixes_test"]
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
        return AsyncClient(transport=transport, base_url="http://testserver", headers=headers)

    return _create


# ==============================================================================
# BUG 1 REGRESSION TESTS: Tournament Credentials Reaching Players vs Security
# ==============================================================================

@pytest.mark.asyncio
async def test_bug1_registered_br_player_receives_credentials_when_released(mock_db, make_client):
    """
    Verifies that a registered participant in a Battle Royale tournament receives
    credentials via GET /{id}/credentials when released, and returns both
    'is_released' and 'credentials_released' fields for frontend compatibility.
    """
    player = User(id="br_player_01", username="br_sniper", email="sniper@gg.io", role="USER")
    unregistered = User(id="unreg_player_99", username="random_guy", email="random@gg.io", role="USER")

    tourn_oid = ObjectId()
    tourn_id = str(tourn_oid)

    await mock_db.tournaments.insert_one({
        "_id": tourn_oid,
        "id": tourn_id,
        "title": "BGMI Pro Battle Royale",
        "slug": "bgmi-pro-br",
        "status": "LIVE",
        "format": "Battle Royale",
        "game_slug": "bgmi",
    })

    # Registered participant record
    await mock_db.tournament_registrations.insert_one({
        "tournament_id": tourn_id,
        "player_id": player.id,
        "payment_status": "PAID",
        "status": "CONFIRMED",
    })

    # Battle Royale match with participant in registered_participant_ids / team_ids
    match_oid = ObjectId()
    await mock_db.matches.insert_one({
        "_id": match_oid,
        "id": str(match_oid),
        "tournament_id": tourn_id,
        "round_number": 1,
        "match_number": 1,
        "round_name": "Round 1",
        "format": "Battle Royale",
        "status": "LIVE",
        "registered_participant_ids": [player.id],
        "team_ids": [player.id],
        "credentials_released": True,
        "room_id": "BGMI-ROOM-777",
        "room_password": "PASS_WINNER_99",
        "custom_instructions": "Drop Pochinki only",
        "server_region": "AWS Mumbai (ap-south-1)",
    })

    client_player = make_client(player)
    resp = await client_player.get(f"/api/v1/tournaments/{tourn_id}/credentials")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "success"
    assert data["is_released"] is True
    assert data["credentials_released"] is True
    assert data["room_id"] == "BGMI-ROOM-777"
    assert data["room_password"] == "PASS_WINNER_99"
    assert data["custom_instructions"] == "Drop Pochinki only"

    # Security check: Unregistered user MUST get 403 Forbidden
    client_unreg = make_client(unregistered)
    resp_unreg = await client_unreg.get(f"/api/v1/tournaments/{tourn_id}/credentials")
    assert resp_unreg.status_code == 403


# ==============================================================================
# BUG 2 REGRESSION TESTS: Live Battle Royale Matches on Matches Page
# ==============================================================================

@pytest.mark.asyncio
async def test_bug2_live_br_matches_synthesized_and_visible_on_matches_page(mock_db, make_client):
    """
    Verifies that a tournament with an active Battle Royale round in tournament_rounds
    is correctly synthesized and appears on the Matches page (/matches), even if
    valid_encounters from db.matches already had records or was empty.
    """
    tourn_oid = ObjectId()
    tourn_id = str(tourn_oid)

    await mock_db.tournaments.insert_one({
        "_id": tourn_oid,
        "id": tourn_id,
        "title": "Apex Legends BR Clash",
        "slug": "apex-br-clash",
        "status": "LIVE",
        "format": "Battle Royale",
        "game_slug": "apex",
    })

    # Stale/completed knockout match in db.matches
    await mock_db.matches.insert_one({
        "_id": ObjectId(),
        "tournament_id": tourn_id,
        "status": "COMPLETED",
        "round_number": 1,
        "roster_status": "PUBLISHED",
        "team_a_name": "Old Squad 1",
        "team_b_name": "Old Squad 2",
    })

    # Active LIVE Battle Royale round in tournament_rounds
    round_oid = ObjectId()
    await mock_db.tournament_rounds.insert_one({
        "_id": round_oid,
        "id": str(round_oid),
        "tournament_id": tourn_id,
        "round_number": 2,
        "round_name": "Round 2 - Semis",
        "stage_name": "Round 2",
        "format": "Battle Royale",
        "status": "LIVE",
        "eligible_contenders": [
            {"id": "team_1", "name": "Apex Kings"},
            {"id": "team_2", "name": "Viper Squad"},
            {"id": "team_3", "name": "Shadow Ravens"},
        ],
        "room_id": "APEX-LIVE-ROOM",
        "room_password": "SECURE",
        "credentials_released": True,
    })

    # Run sync service
    live_doc = await sync_live_match_for_tournament(tourn_id, mock_db)
    assert live_doc is not None
    assert live_doc["tournament_id"] == tourn_id
    assert live_doc["status"] == "LIVE"

    # Query public matches endpoint
    public_client = make_client()
    resp = await public_client.get("/api/v1/matches")
    assert resp.status_code == 200, resp.text
    matches = resp.json()
    assert len(matches) >= 1

    br_match = next((m for m in matches if m.get("tournament_id") == tourn_id), None)
    assert br_match is not None
    assert br_match["status"] == "LIVE"
    assert br_match["format"] == "Battle Royale"


# ==============================================================================
# BUG 3 REGRESSION TESTS: Match Number and Round Display Distinction
# ==============================================================================

@pytest.mark.asyncio
async def test_bug3_round_label_and_encounter_number_distinction(mock_db):
    """
    Verifies that the round label correctly displays 'Round 2' instead of being
    overwritten with 'Match 1'.
    """
    tourn_oid = ObjectId()
    tourn_id = str(tourn_oid)

    await mock_db.tournaments.insert_one({
        "_id": tourn_oid,
        "id": tourn_id,
        "title": "Valorant Championship",
        "slug": "val-champ",
        "status": "LIVE",
        "format": "Single Elimination",
    })

    match_oid = ObjectId()
    await mock_db.matches.insert_one({
        "_id": match_oid,
        "id": str(match_oid),
        "tournament_id": tourn_id,
        "round_number": 2,
        "round_name": "Round 2",
        "match_number": 1,
        "order": 1,
        "stage": "Round 2",
        "status": "LIVE",
        "team_a_name": "Sentinels",
        "team_b_name": "Fnatic",
        "roster_status": "PUBLISHED",
    })

    live_doc = await sync_live_match_for_tournament(tourn_id, mock_db)
    assert live_doc is not None
    assert live_doc["current_round_name"] == "Round 2"
    assert live_doc["current_round_number"] == 2
    assert live_doc["current_encounter_number"] == 1
    assert live_doc["current_encounter_title"] == "Match 1"


# ==============================================================================
# BUG 5 REGRESSION TESTS: Second-Round Credentials Return Round 2
# ==============================================================================

@pytest.mark.asyncio
async def test_bug5_second_round_credentials_return_latest_active_round(mock_db, make_client):
    """
    Verifies that when Round 1 is completed and Round 2 is LIVE,
    GET /{id}/credentials returns Round 2 credentials (not Round 1)
    by sorting tournament_rounds descending by round_number.
    """
    player = User(id="qualifier_01", username="champion", email="champ@gg.io", role="USER")
    tourn_oid = ObjectId()
    tourn_id = str(tourn_oid)

    await mock_db.tournaments.insert_one({
        "_id": tourn_oid,
        "id": tourn_id,
        "title": "Multi-Round Battle Royale",
        "slug": "mr-br",
        "status": "LIVE",
        "format": "Battle Royale",
    })

    await mock_db.tournament_registrations.insert_one({
        "tournament_id": tourn_id,
        "player_id": player.id,
        "payment_status": "PAID",
        "status": "CONFIRMED",
    })

    # Round 1: Completed
    await mock_db.tournament_rounds.insert_one({
        "_id": ObjectId(),
        "tournament_id": tourn_id,
        "round_number": 1,
        "round_name": "Round 1",
        "stage_name": "Round 1",
        "status": "COMPLETED",
        "room_id": "OLD_ROUND_1_ROOM",
        "room_password": "OLD_PASS",
        "credentials_released": True,
    })

    # Round 2: LIVE with new credentials
    await mock_db.tournament_rounds.insert_one({
        "_id": ObjectId(),
        "tournament_id": tourn_id,
        "round_number": 2,
        "round_name": "Round 2",
        "stage_name": "Round 2",
        "status": "LIVE",
        "room_id": "NEW_ROUND_2_ROOM",
        "room_password": "NEW_PASS_SECURE",
        "custom_instructions": "Round 2 custom lobby settings",
        "credentials_released": True,
    })

    client = make_client(player)
    resp = await client.get(f"/api/v1/tournaments/{tourn_id}/credentials")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["is_released"] is True
    assert data["room_id"] == "NEW_ROUND_2_ROOM"
    assert data["room_password"] == "NEW_PASS_SECURE"
    assert data["round_name"] == "Round 2"


# ==============================================================================
# BUG 6 REGRESSION TESTS: Match Publishing and ObjectId String Lookup
# ==============================================================================

@pytest.mark.asyncio
async def test_bug6_mark_match_live_and_publish_with_objectid(mock_db, make_client):
    """
    Verifies that calling mark-live, publish-roster, and update on a match
    identified by a 24-character ObjectId string succeeds with 200 OK
    instead of failing with 404 'Match not found'.
    """
    admin = User(id="admin_pub_01", username="admin_publisher", email="pub@gg.io", role="SUPER_ADMIN")
    tourn_oid = ObjectId()
    tourn_id = str(tourn_oid)

    await mock_db.tournaments.insert_one({
        "_id": tourn_oid,
        "id": tourn_id,
        "title": "Valorant Masters",
        "slug": "val-masters",
        "status": "REGISTRATION_CLOSED",
    })

    match_oid = ObjectId()
    match_hex = str(match_oid)

    # Persisted with ObjectId as _id in MongoDB
    await mock_db.matches.insert_one({
        "_id": match_oid,
        "id": match_hex,
        "tournament_id": tourn_id,
        "round_number": 1,
        "match_number": 1,
        "status": "SCHEDULED",
        "roster_status": "DRAFT",
        "team_a_name": "Team Liquid",
        "team_b_name": "Cloud9",
    })

    client = make_client(admin)

    # 1. Update match details
    resp_update = await client.patch(f"/api/v1/admin/matches/{match_hex}/update", json={
        "status": "SCHEDULED",
        "team_a_name": "Team Liquid Pro",
    })
    assert resp_update.status_code == 200, resp_update.text
    assert resp_update.json()["status"] == "success"

    # 2. Publish match roster
    resp_publish = await client.post(f"/api/v1/admin/matches/{match_hex}/publish-roster", json={
        "notify_leaders": False,
    })
    assert resp_publish.status_code == 200, resp_publish.text
    assert resp_publish.json()["roster_status"] == "PUBLISHED"

    # 3. Mark match live with YouTube URL
    resp_live = await client.post(f"/api/v1/admin/matches/{match_hex}/mark-live", json={
        "youtube_live_url": "https://www.youtube.com/watch?v=live12345",
    })
    assert resp_live.status_code == 200, resp_live.text
    assert resp_live.json()["status"] == "success"

    # Verify persisted state in database
    persisted = await mock_db.matches.find_one({"_id": match_oid})
    assert persisted["status"] == "LIVE"
    assert persisted["roster_status"] == "PUBLISHED"
    assert persisted["youtube_live_url"] == "https://www.youtube.com/watch?v=live12345"
