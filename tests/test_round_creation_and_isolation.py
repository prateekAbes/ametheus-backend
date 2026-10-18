import pytest
import pytest_asyncio
from datetime import datetime, timezone
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport
from fastapi import Header, Depends, HTTPException

from app.core.database import get_db, set_database
from app.main import app
from app.models.user import User
from app.services.bracket_engine import advance_bracket_match
from app.api.deps import get_current_user, get_current_user_optional, require_organizer, require_super_admin


@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_round_creation_isolation_test"]
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
            token = f"token_{user.id}"
            user_map[token] = user
            headers["Authorization"] = f"Bearer {token}"
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://testserver", headers=headers)

    return _create


def create_admin():
    return User(
        id="admin_iso_001",
        email="admin@ametheus.gg",
        username="IsolationAdmin",
        display_name="Isolation Admin",
        role="SUPER_ADMIN",
    )


def create_player(uid: str, username: str):
    return User(
        id=uid,
        email=f"{username}@test.com",
        username=username,
        display_name=username,
        role="USER",
    )


# =========================================================================
# TEST 1: Match completion does NOT automatically create Round 2 or downstream matches
# =========================================================================
@pytest.mark.asyncio
async def test_no_automatic_downstream_round_creation_on_br_results(mock_db, make_client):
    """
    Submitting Battle Royale results for Round 1 must record qualifications
    but must NEVER automatically spawn an unapproved Round 2 Match document.
    """
    admin = create_admin()
    client = make_client(admin)

    tid = "t_br_auto_prevent"
    m_r1_id = "m_r1_br_001"

    await mock_db.tournaments.insert_one({
        "_id": tid,
        "id": tid,
        "title": "BR Championship",
        "organizer_id": admin.id,
        "status": "LIVE",
        "format": "Battle Royale",
        "total_rounds": 3,
        "rules": {"qualification_threshold": 2},
    })

    # Round 1 match with 4 squads (status is ENDED so results can be submitted)
    await mock_db.matches.insert_one({
        "_id": m_r1_id,
        "id": m_r1_id,
        "tournament_id": tid,
        "round_name": "Round 1",
        "round_number": 1,
        "match_number": 1,
        "format": "Battle Royale",
        "status": "ENDED",
        "team_ids": ["squad_a", "squad_b", "squad_c", "squad_d"],
        "teams": [
            {"id": "squad_a", "name": "Squad Alpha", "tag": "ALPH"},
            {"id": "squad_b", "name": "Squad Bravo", "tag": "BRAV"},
            {"id": "squad_c", "name": "Squad Charlie", "tag": "CHAR"},
            {"id": "squad_d", "name": "Squad Delta", "tag": "DELT"},
        ],
    })

    # Submit results for Round 1
    results_payload = {
        "results": [
            {"team_id": "squad_a", "rank": 1, "kills": 10, "points": 25, "qualified": True},
            {"team_id": "squad_b", "rank": 2, "kills": 6, "points": 18, "qualified": True},
            {"team_id": "squad_c", "rank": 3, "kills": 2, "points": 10, "qualified": False},
            {"team_id": "squad_d", "rank": 4, "kills": 0, "points": 5, "qualified": False},
        ],
        "qualifier_team_ids": ["squad_a", "squad_b"],
        "draft": False,
        "is_final_round": False,
    }

    resp = await client.post(
        f"/api/v1/admin/matches/{m_r1_id}/battle-royale-results",
        json=results_payload,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "success"

    # Verify Round 1 match is updated to RESULTS_SAVED / COMPLETED
    m1 = await mock_db.matches.find_one({"_id": m_r1_id})
    assert m1["status"] in ["RESULTS_SAVED", "COMPLETED"]

    # Verify that NO match for Round 2 was auto-created in the database
    r2_matches = await mock_db.matches.find({"tournament_id": tid, "round_number": 2}).to_list(10)
    assert len(r2_matches) == 0, "Downstream round match was automatically spawned when it should not be!"

    # Qualifications were safely recorded in tournament_qualifications
    quals = await mock_db.tournament_qualifications.find({"tournament_id": tid}).to_list(10)
    assert len(quals) >= 1
    qualified_ids = [q["participant_id"] for q in quals if q.get("status") == "QUALIFIED"]
    assert "squad_a" in qualified_ids
    assert "squad_b" in qualified_ids


# =========================================================================
# TEST 2: Round-specific credential isolation (Round 1 credentials do not bleed into Round 2)
# =========================================================================
@pytest.mark.asyncio
async def test_round_specific_credential_isolation(mock_db, make_client):
    """
    Test that credentials for Round 1 are isolated and do not bleed into Round 2.
    When Round 2 credentials have NOT been released, a qualified participant in Round 2
    cannot see Round 2 credentials (nor Round 1's).
    """
    admin = create_admin()
    player = create_player("user_qual_001", "QualPlayer")

    tid = "t_cred_isolation"
    m_r1_id = "m_r1_cred_001"
    m_r2_id = "m_r2_cred_002"

    await mock_db.users.insert_one(player.to_mongo())
    await mock_db.tournaments.insert_one({
        "_id": tid,
        "id": tid,
        "title": "Credential Isolation Test",
        "organizer_id": admin.id,
        "status": "LIVE",
        "registration_type": "SOLO",
        "is_solo": True,
        "format": "Battle Royale",
    })

    await mock_db.tournament_registrations.insert_one({
        "tournament_id": tid,
        "player_id": player.id,
        "payment_status": "PAID",
        "is_active": True,
    })

    # Round 1 Match: credentials RELEASED
    await mock_db.matches.insert_one({
        "_id": m_r1_id,
        "id": m_r1_id,
        "tournament_id": tid,
        "round_name": "Round 1",
        "round_number": 1,
        "match_number": 1,
        "status": "COMPLETED",
        "credentials_released": True,
        "custom_room_id": "ROOM_ROUND_1",
        "custom_room_password": "PASSWORD_ROUND_1",
        "room_id": "ROOM_ROUND_1",
        "room_password": "PASSWORD_ROUND_1",
        "registered_participant_ids": [player.id],
    })

    # Round 2 Match: credentials NOT YET RELEASED
    await mock_db.matches.insert_one({
        "_id": m_r2_id,
        "id": m_r2_id,
        "tournament_id": tid,
        "round_name": "Round 2",
        "round_number": 2,
        "match_number": 1,
        "status": "SCHEDULED",
        "credentials_released": False,
        "custom_room_id": "ROOM_ROUND_2",
        "custom_room_password": "PASSWORD_ROUND_2",
        "room_id": "ROOM_ROUND_2",
        "room_password": "PASSWORD_ROUND_2",
        "registered_participant_ids": [player.id],
    })

    player_client = make_client(player)

    # 1. Fetch Round 2 join info as player: credentials_released must be False and room details masked
    r2_resp = await player_client.get(f"/api/v1/matches/{m_r2_id}/join-info")
    assert r2_resp.status_code == 200
    r2_data = r2_resp.json()
    assert r2_data["credentials_released"] is False
    assert r2_data["room_id"] is None, "Round 2 room_id was leaked before release!"
    assert r2_data["room_password"] is None, "Round 2 room_password was leaked before release!"
    assert r2_data["room_id"] != "ROOM_ROUND_1", "Round 1 room credentials bled into Round 2!"

    # 2. Organizer releases credentials for Round 2 via release-credentials endpoint
    admin_client = make_client(admin)
    rel_resp = await admin_client.post(f"/api/v1/admin/matches/{m_r2_id}/release-credentials")
    assert rel_resp.status_code == 200, rel_resp.text

    # 3. Now player fetches Round 2 join info: Round 2 credentials are now visible
    r2_after_resp = await player_client.get(f"/api/v1/matches/{m_r2_id}/join-info")
    assert r2_after_resp.status_code == 200
    r2_after_data = r2_after_resp.json()
    assert r2_after_data["credentials_released"] is True
    assert r2_after_data["room_id"] == "ROOM_ROUND_2"
    assert r2_after_data["room_password"] == "PASSWORD_ROUND_2"


# =========================================================================
# TEST 3: Intermediate round completion does not prematurely close tournament
# =========================================================================
@pytest.mark.asyncio
async def test_intermediate_round_completion_does_not_close_tournament(mock_db):
    """
    In a 3-round tournament, advancing/completing a match in Round 1 or Round 2
    must not mark the tournament as COMPLETED.
    """
    from app.models.match import Match
    tid = "t_intermediate_premature_close"
    m_id = "m_round_1_ko"

    await mock_db.tournaments.insert_one({
        "_id": tid,
        "id": tid,
        "title": "3-Round Championship",
        "status": "LIVE",
        "total_rounds": 3,
        "format": "Single Elimination",
    })

    match_dict = {
        "_id": m_id,
        "id": m_id,
        "tournament_id": tid,
        "round_name": "Round 1",
        "round_number": 1,
        "match_number": 1,
        "status": "LIVE",
        "team_a_id": "team_1",
        "team_b_id": "team_2",
    }
    await mock_db.matches.insert_one(match_dict)
    completed_match = Match.from_mongo(match_dict)

    # Advance Round 1 match with team_1 as winner
    res = await advance_bracket_match(
        tournament_id=tid,
        completed_match=completed_match,
        winner_team_id="team_1",
        db=mock_db,
    )

    # Tournament must NOT be completed because curr_round (1) < total_rounds (3)
    t_doc = await mock_db.tournaments.find_one({"_id": tid})
    assert t_doc["status"] != "COMPLETED", "Tournament was prematurely marked COMPLETED after Round 1!"
    assert t_doc["status"] == "LIVE"


# =========================================================================
# TEST 4: Immediate credential access without check-in gating
# =========================================================================
@pytest.mark.asyncio
async def test_immediate_credential_access_without_check_in(mock_db, make_client):
    """
    Verify that registered participants can access match credentials immediately
    when released, without requiring a check-in action or CHECK_IN_OPEN status.
    """
    admin = create_admin()
    player = create_player("user_immediate_001", "FastPlayer")

    tid = "t_immediate_access"
    mid = "m_immediate_001"

    await mock_db.users.insert_one(player.to_mongo())
    await mock_db.tournaments.insert_one({
        "_id": tid,
        "id": tid,
        "title": "No Check-in Required Tournament",
        "organizer_id": admin.id,
        "status": "LIVE",
        "registration_type": "SOLO",
        "is_solo": True,
    })

    await mock_db.tournament_registrations.insert_one({
        "tournament_id": tid,
        "player_id": player.id,
        "payment_status": "PAID",
        "is_active": True,
    })

    # Match in SCHEDULED status with credentials released
    await mock_db.matches.insert_one({
        "_id": mid,
        "id": mid,
        "tournament_id": tid,
        "round_name": "Round 1",
        "round_number": 1,
        "status": "SCHEDULED",
        "credentials_released": True,
        "custom_room_id": "DIRECT_ACCESS_ROOM",
        "custom_room_password": "DIRECT_ACCESS_PASS",
        "registered_participant_ids": [player.id],
    })

    player_client = make_client(player)

    # Participant accesses join-info directly without any check-in
    resp = await player_client.get(f"/api/v1/matches/{mid}/join-info")
    assert resp.status_code == 200
    data = resp.json()
    assert data["credentials_released"] is True
    assert data["room_id"] == "DIRECT_ACCESS_ROOM"
    assert data["room_password"] == "DIRECT_ACCESS_PASS"
    assert "check-in" not in data.get("instructions", "").lower()
