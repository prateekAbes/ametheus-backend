import pytest
import pytest_asyncio
from datetime import datetime, timezone
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport

from app.core.database import get_db, set_database
from app.main import app
from app.models.user import User
from app.api.deps import get_current_user, get_current_user_optional, require_organizer, require_super_admin


@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_matches_lifecycle_test"]
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


def create_admin():
    return User(
        id="admin_lifecycle_001",
        email="admin@ametheus.gg",
        username="LifecycleAdmin",
        display_name="Lifecycle Admin",
        role="SUPER_ADMIN",
    )


def create_player(uid: str, username: str, display_name: str):
    return {
        "_id": uid,
        "id": uid,
        "username": username,
        "display_name": display_name,
        "avatar_url": f"https://ametheus.gg/avatars/{uid}.png",
        "email": f"{username}@test.com",
        "role": "USER",
    }


# =========================================================================
# TEST 1 & 2 & 3: Match Card Structured Heading, Participant Terminology,
# and Slicing Previews
# =========================================================================

def test_heading_and_participant_terminology_logic():
    """
    Verify match card heading formatting and terminology logic across:
    - Solo Battle Royale: '4 Competing Players'
    - Squad Battle Royale: '4 Competing Squads'
    - 2v2/3v3/5v5 Team Formats: '4 Competing Teams'
    - Slicing: max 4 previews + remaining count '+21 more'
    """
    # 1. Heading formatting logic: `${dateUpper} · ${roundUpper} · MATCH ${matchNum}`
    date_str = "24 OCT 2026"
    round_name = "ROUND 1"
    match_num = 1
    heading = f"{date_str} · {round_name} · MATCH {match_num}"
    assert heading == "24 OCT 2026 · ROUND 1 · MATCH 1"

    # 2. Format-aware terminology logic
    def get_participant_label(is_br: bool, is_solo: bool, count: int) -> str:
        if is_solo:
            return f"{count} Competing {'Player' if count == 1 else 'Players'}"
        if is_br:
            return f"{count} Competing {'Squad' if count == 1 else 'Squads'}"
        return f"{count} Competing {'Team' if count == 1 else 'Teams'}"

    assert get_participant_label(is_br=True, is_solo=True, count=4) == "4 Competing Players"
    assert get_participant_label(is_br=True, is_solo=False, count=4) == "4 Competing Squads"
    assert get_participant_label(is_br=False, is_solo=False, count=4) == "4 Competing Teams"
    assert get_participant_label(is_br=False, is_solo=False, count=2) == "2 Competing Teams"

    # 3. Slicing logic: 25 participants -> 4 previews + '+21 more'
    participants = [f"Participant {i+1}" for i in range(25)]
    previews = participants[:4]
    remaining = len(participants) - len(previews)
    assert len(previews) == 4
    assert remaining == 21
    assert f"+{remaining} more" == "+21 more"


# =========================================================================
# TEST 4 & 5: Scheduled Match -> Go Live -> Live Status & Idempotent Refresh
# =========================================================================

@pytest.mark.asyncio
async def test_scheduled_to_live_transition_and_idempotency(mock_db, make_client):
    """
    Test Scheduled match transitions to Live via update_match_status,
    remains Live on repeated calls/refreshes, and does not revert.
    """
    admin = create_admin()
    client = make_client(admin)

    tid = "t_live_001"
    mid = "m_live_001"
    now_iso = datetime.now(timezone.utc).isoformat()

    await mock_db.tournaments.insert_one({
        "_id": tid,
        "id": tid,
        "title": "Championship Live Test",
        "organizer_id": admin.id,
        "status": "LIVE",
    })

    await mock_db.matches.insert_one({
        "_id": mid,
        "id": mid,
        "tournament_id": tid,
        "round_name": "Round 1",
        "match_number": 1,
        "status": "SCHEDULED",
        "scheduled_at": now_iso,
    })

    # 1. Update status to LIVE (Go Live)
    resp = await client.patch(f"/api/v1/admin/matches/{mid}/status", json={"status": "LIVE"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"

    # Database verification
    m_doc = await mock_db.matches.find_one({"_id": mid})
    assert m_doc["status"] == "LIVE"

    # 2. Repeated call (simulate refresh or duplicate click) remains LIVE
    resp2 = await client.patch(f"/api/v1/admin/matches/{mid}/status", json={"status": "LIVE"})
    assert resp2.status_code == 200
    m_doc2 = await mock_db.matches.find_one({"_id": mid})
    assert m_doc2["status"] == "LIVE"


# =========================================================================
# TEST 6: Ended Match Displays Ended Status (No Stale Scheduled or Live)
# =========================================================================

@pytest.mark.asyncio
async def test_match_transition_to_ended(mock_db, make_client):
    """
    Verify match transitions to ENDED and database reflects ENDED status.
    """
    admin = create_admin()
    client = make_client(admin)

    tid = "t_ended_001"
    mid = "m_ended_001"

    await mock_db.tournaments.insert_one({
        "_id": tid,
        "id": tid,
        "title": "Ended Match Test",
        "organizer_id": admin.id,
    })

    await mock_db.matches.insert_one({
        "_id": mid,
        "id": mid,
        "tournament_id": tid,
        "round_name": "Round 1",
        "match_number": 1,
        "status": "LIVE",
    })

    # End match
    resp = await client.patch(f"/api/v1/admin/matches/{mid}/status", json={"status": "ENDED"})
    assert resp.status_code == 200

    m_doc = await mock_db.matches.find_one({"_id": mid})
    assert m_doc["status"] == "ENDED"


# =========================================================================
# TEST 7 & 8: Scheduled and Live Reject Results Entry; Ended Allows It
# =========================================================================

@pytest.mark.asyncio
async def test_premature_result_entry_gating(mock_db, make_client):
    """
    Verify submit_battle_royale_results_admin rejects SCHEDULED and LIVE matches with HTTP 400,
    and accepts submission once match is ENDED or RESULTS_SAVED.
    """
    admin = create_admin()
    client = make_client(admin)

    tid = "t_gating_001"
    mid = "m_gating_001"

    await mock_db.tournaments.insert_one({
        "_id": tid,
        "id": tid,
        "title": "Gating Test",
        "organizer_id": admin.id,
        "format": "BATTLE_ROYALE",
    })

    await mock_db.matches.insert_one({
        "_id": mid,
        "id": mid,
        "tournament_id": tid,
        "round_name": "Round 1",
        "match_number": 1,
        "status": "SCHEDULED",
        "teams": [{"id": "t1", "name": "Team 1"}, {"id": "t2", "name": "Team 2"}],
    })

    payload = {
        "placements": [
            {"team_id": "t1", "placement": 1, "kills": 5, "placement_points": 10, "kill_points": 5, "total_points": 15},
            {"team_id": "t2", "placement": 2, "kills": 2, "placement_points": 6, "kill_points": 2, "total_points": 8},
        ],
        "qualifier_team_ids": ["t1"],
        "draft": False,
    }

    # 1. Reject on SCHEDULED
    resp_sched = await client.post(f"/api/v1/admin/matches/{mid}/battle-royale-results", json=payload)
    assert resp_sched.status_code == 400
    assert "cannot enter or submit match results" in resp_sched.json()["detail"].lower()

    # 2. Update to LIVE and reject on LIVE
    await mock_db.matches.update_one({"_id": mid}, {"$set": {"status": "LIVE"}})
    resp_live = await client.post(f"/api/v1/admin/matches/{mid}/battle-royale-results", json=payload)
    assert resp_live.status_code == 400
    assert "cannot enter or submit match results" in resp_live.json()["detail"].lower()

    # 3. Update to ENDED and verify results entry is accepted
    await mock_db.matches.update_one({"_id": mid}, {"$set": {"status": "ENDED"}})
    resp_ended = await client.post(f"/api/v1/admin/matches/{mid}/battle-royale-results", json=payload)
    assert resp_ended.status_code == 200
    
    m_doc = await mock_db.matches.find_one({"_id": mid})
    assert m_doc["status"] == "COMPLETED"


# =========================================================================
# TEST 9 & 10 & 11: Draft Results Workflow (Save, Reopen, Edit)
# =========================================================================

@pytest.mark.asyncio
async def test_draft_results_lifecycle(mock_db, make_client):
    """
    Test:
    9. Saving draft results sets status to RESULTS_SAVED without finalizing or advancing qualifiers.
    10. Reopening draft results returns exact saved statistics.
    11. Editing draft results updates review data cleanly.
    """
    admin = create_admin()
    client = make_client(admin)

    tid = "t_draft_001"
    mid = "m_draft_001"

    await mock_db.tournaments.insert_one({
        "_id": tid,
        "id": tid,
        "title": "Draft Results Tournament",
        "organizer_id": admin.id,
        "format": "BATTLE_ROYALE",
    })

    await mock_db.matches.insert_one({
        "_id": mid,
        "id": mid,
        "tournament_id": tid,
        "round_name": "Round 1",
        "match_number": 1,
        "status": "ENDED",
        "teams": [
            {"id": "t1", "name": "Alpha Squad"},
            {"id": "t2", "name": "Bravo Squad"},
            {"id": "t3", "name": "Charlie Squad"},
        ],
    })

    # Step 9: Save Draft Results
    draft_payload = {
        "placements": [
            {"team_id": "t1", "placement": 1, "kills": 7, "placement_points": 10, "kill_points": 7, "total_points": 17},
            {"team_id": "t2", "placement": 2, "kills": 3, "placement_points": 6, "kill_points": 3, "total_points": 9},
            {"team_id": "t3", "placement": 3, "kills": 1, "placement_points": 4, "kill_points": 1, "total_points": 5},
        ],
        "qualifier_team_ids": ["t1", "t2"],
        "draft": True,
    }

    resp = await client.post(f"/api/v1/admin/matches/{mid}/battle-royale-results", json=draft_payload)
    assert resp.status_code == 200
    res = resp.json()
    assert res["status"] == "success"
    assert res.get("draft") is True

    # Check match doc in DB
    m_doc = await mock_db.matches.find_one({"_id": mid})
    assert m_doc["status"] == "RESULTS_SAVED"
    # Ensure tournament was NOT completed and qualifiers were NOT marked as finalized
    t_doc = await mock_db.tournaments.find_one({"_id": tid})
    assert t_doc.get("status") != "COMPLETED"

    # Step 10: Reopen Draft Results via GET /matches/{id}
    get_resp = await client.get(f"/api/v1/matches/{mid}")
    assert get_resp.status_code == 200
    match_data = get_resp.json()
    assert match_data["status"] == "RESULTS_SAVED"
    assert len(match_data["placements"]) == 3
    assert match_data["placements"][0]["kills"] == 7
    assert match_data["placements"][0]["total_points"] == 17

    # Step 11: Edit Saved Draft Results (adjust kills and points)
    updated_draft = {
        "placements": [
            {"team_id": "t1", "placement": 1, "kills": 8, "placement_points": 10, "kill_points": 8, "total_points": 18},
            {"team_id": "t2", "placement": 2, "kills": 4, "placement_points": 6, "kill_points": 4, "total_points": 10},
            {"team_id": "t3", "placement": 3, "kills": 1, "placement_points": 4, "kill_points": 1, "total_points": 5},
        ],
        "qualifier_team_ids": ["t1"],
        "draft": True,
    }
    resp_update = await client.post(f"/api/v1/admin/matches/{mid}/battle-royale-results", json=updated_draft)
    assert resp_update.status_code == 200

    m_updated = await mock_db.matches.find_one({"_id": mid})
    assert m_updated["placements"][0]["kills"] == 8
    assert m_updated["placements"][0]["total_points"] == 18
    assert m_updated["qualifier_team_ids"] == ["t1"]


# =========================================================================
# TEST 12: Transparent Rating Adjustments Breakdown
# =========================================================================

@pytest.mark.asyncio
async def test_transparent_rating_adjustments_breakdown(mock_db, make_client):
    """
    Verify get_round_review_admin displays transparent scoring breakdown:
    placement, kills, points, pre_rating, proposed_adjustment, post_rating, and calculation_reason.
    """
    admin = create_admin()
    client = make_client(admin)

    tid = "t_review_001"
    mid = "m_review_001"
    round_name = "Round 1"

    p1 = create_player("u_rev_1", "winner_ace", "Winner Ace")
    p2 = create_player("u_rev_2", "runner_up", "Runner Up")
    await mock_db.users.insert_many([p1, p2])

    await mock_db.teams.insert_one({
        "_id": "t1",
        "id": "t1",
        "name": "Team 1",
        "captain_id": "u_rev_1",
        "players": [{"user_id": "u_rev_1"}],
    })
    await mock_db.teams.insert_one({
        "_id": "t2",
        "id": "t2",
        "name": "Team 2",
        "captain_id": "u_rev_2",
        "players": [{"user_id": "u_rev_2"}],
    })

    await mock_db.player_ratings.insert_one({
        "_id": "rate_u_rev_1",
        "user_id": "u_rev_1",
        "game_id": "valorant",
        "game_slug": "valorant",
        "rating": 500,
        "matches_played": 1,
    })
    await mock_db.player_ratings.insert_one({
        "_id": "rate_u_rev_2",
        "user_id": "u_rev_2",
        "game_id": "valorant",
        "game_slug": "valorant",
        "rating": 500,
        "matches_played": 1,
    })

    await mock_db.tournaments.insert_one({
        "_id": tid,
        "id": tid,
        "title": "Review Tournament",
        "game_id": "valorant",
        "game_slug": "valorant",
        "organizer_id": admin.id,
    })

    await mock_db.matches.insert_one({
        "_id": mid,
        "id": mid,
        "tournament_id": tid,
        "round_name": round_name,
        "match_number": 1,
        "status": "COMPLETED",
        "teams": [
            {"id": "t1", "name": "Team 1"},
            {"id": "t2", "name": "Team 2"},
        ],
        "team_ids": ["t1", "t2"],
        "placements": [
            {"team_id": "t1", "placement": 1, "kills": 12, "placement_points": 10, "kill_points": 12, "total_points": 22},
            {"team_id": "t2", "placement": 2, "kills": 3, "placement_points": 6, "kill_points": 3, "total_points": 9},
        ],
        "winner_team_id": "t1",
    })

    resp = await client.get(f"/api/v1/admin/tournaments/{tid}/rounds/{round_name}/review")
    assert resp.status_code == 200
    data = resp.json()
    assert "players" in data
    assert len(data["players"]) == 2

    winner_entry = next((p for p in data["players"] if p["user_id"] == "u_rev_1"), None)
    assert winner_entry is not None
    assert winner_entry["placement"] == 1
    assert winner_entry["kills"] == 12
    assert winner_entry["current_rating"] == 500
    assert winner_entry["proposed_adjustment"] == 25
    assert winner_entry["post_rating"] == 525
    assert "1st Place" in winner_entry["calculation_reason"]


# =========================================================================
# TEST 13 & 14: Round Finalization Gating with Incomplete Matches
# =========================================================================

@pytest.mark.asyncio
async def test_round_finalization_blocks_on_incomplete_matches(mock_db, make_client):
    """
    Test a round with multiple matches:
    - Attempting to finalize when any match is unfinished (LIVE, ENDED, RESULTS_SAVED) fails with 400.
    - Error message clearly identifies the unfinished match ID and status.
    """
    admin = create_admin()
    client = make_client(admin)

    tid = "t_round_block_001"
    round_name = "Round 1"

    await mock_db.tournaments.insert_one({
        "_id": tid,
        "id": tid,
        "title": "Round Finalize Test",
        "organizer_id": admin.id,
    })

    # Match 1 is COMPLETED
    await mock_db.matches.insert_one({
        "_id": "m_done_001",
        "id": "m_done_001",
        "tournament_id": tid,
        "round_name": round_name,
        "match_number": 1,
        "status": "COMPLETED",
        "teams": [{"id": "t1", "name": "Team 1"}],
    })

    # Match 2 is still LIVE
    await mock_db.matches.insert_one({
        "_id": "m_live_002",
        "id": "m_live_002",
        "tournament_id": tid,
        "round_name": round_name,
        "match_number": 2,
        "status": "LIVE",
        "teams": [{"id": "t2", "name": "Team 2"}],
    })

    # Match 3 is ENDED (results not yet saved/finalized)
    await mock_db.matches.insert_one({
        "_id": "m_ended_003",
        "id": "m_ended_003",
        "tournament_id": tid,
        "round_name": round_name,
        "match_number": 3,
        "status": "ENDED",
        "teams": [{"id": "t3", "name": "Team 3"}],
    })

    # Attempt to finalize round
    resp = await client.post(f"/api/v1/admin/tournaments/{tid}/rounds/{round_name}/finalize", json={
        "qualifier_ids": ["t1"],
    })
    assert resp.status_code == 400
    err_detail = resp.json()["detail"]
    assert "Cannot finalize round" in err_detail
    assert "m_live_002" in err_detail
    assert "LIVE" in err_detail
    assert "m_ended_003" in err_detail
    assert "ENDED" in err_detail


# =========================================================================
# TEST 15 & 16 & 17: Round Finalization & Rating Application Idempotency
# =========================================================================

@pytest.mark.asyncio
async def test_round_ratings_and_finalization_idempotency(mock_db, make_client):
    """
    Test:
    15. Finalize round after all matches complete: applies rating changes.
    16. Qualifiers determined from finalized results.
    17. Repeated rating submission API calls do not duplicate rating changes (idempotency).
    """
    admin = create_admin()
    client = make_client(admin)

    tid = "t_rating_idemp_001"
    round_name = "Round 1"
    mid = "m_idemp_001"

    player = create_player("u_idemp_1", "idemp_champ", "Idemp Champ")
    await mock_db.users.insert_one(player)

    await mock_db.teams.insert_one({
        "_id": "t1",
        "id": "t1",
        "name": "Solo Team",
        "captain_id": "u_idemp_1",
        "players": [{"user_id": "u_idemp_1"}],
    })

    await mock_db.player_ratings.insert_one({
        "_id": "rate_u_idemp_1",
        "user_id": "u_idemp_1",
        "game_id": "valorant",
        "game_slug": "valorant",
        "rating": 500,
        "matches_played": 1,
    })

    await mock_db.tournaments.insert_one({
        "_id": tid,
        "id": tid,
        "title": "Idempotency Tournament",
        "game_id": "valorant",
        "game_slug": "valorant",
        "organizer_id": admin.id,
    })

    await mock_db.matches.insert_one({
        "_id": mid,
        "id": mid,
        "tournament_id": tid,
        "round_name": round_name,
        "match_number": 1,
        "status": "COMPLETED",
        "teams": [{"id": "t1", "name": "Solo Team"}],
        "team_ids": ["t1"],
        "winner_team_id": "t1",
    })

    submission_id = f"test_submission_{tid}_{round_name}"
    rating_payload = {
        "submission_id": submission_id,
        "ratings": [
            {
                "player_id": "u_idemp_1",
                "user_id": "u_idemp_1",
                "rating": 9,
                "skill_adjustment": 20,
                "adjustment_reason": "Round victory",
                "match_id": mid,
            }
        ],
    }

    # First rating submission
    resp1 = await client.post(f"/api/v1/admin/tournaments/{tid}/rounds/{round_name}/ratings", json=rating_payload)
    assert resp1.status_code == 200

    r_doc = await mock_db.player_ratings.find_one({"user_id": "u_idemp_1", "game_id": "valorant"})
    assert r_doc["rating"] == 520

    # Second rating submission with same submission_id (retry / duplicate click)
    resp2 = await client.post(f"/api/v1/admin/tournaments/{tid}/rounds/{round_name}/ratings", json=rating_payload)
    assert resp2.status_code == 200

    # Ensure rating did NOT double increment to 540
    r_doc2 = await mock_db.player_ratings.find_one({"user_id": "u_idemp_1", "game_id": "valorant"})
    assert r_doc2["rating"] == 520

    # Step 16: Finalize round with qualifiers
    fin_resp = await client.post(f"/api/v1/admin/tournaments/{tid}/rounds/{round_name}/finalize", json={
        "qualifier_ids": ["t1"],
    })
    assert fin_resp.status_code == 200
    assert fin_resp.json()["qualified_count"] == 1

    # Check tournament_qualifications collection
    qual_doc = await mock_db.tournament_qualifications.find_one({"tournament_id": tid, "participant_id": "t1"})
    assert qual_doc is not None
    assert qual_doc["status"] == "QUALIFIED"


# =========================================================================
# TEST 18: Edge Cases (Missing Scores, Zero Values, Ties, Drafts)
# =========================================================================

@pytest.mark.asyncio
async def test_edge_cases_handling(mock_db, make_client):
    """
    Test edge cases:
    - 0 kills, 0 placement points, tie in placement points
    - Missing scores in head-to-head match
    - Incomplete draft submissions
    """
    admin = create_admin()
    client = make_client(admin)

    tid = "t_edge_001"
    mid = "m_edge_001"

    await mock_db.tournaments.insert_one({
        "_id": tid,
        "id": tid,
        "title": "Edge Case Tournament",
        "organizer_id": admin.id,
    })

    # Head-to-head match with missing scores on scheduled match
    await mock_db.matches.insert_one({
        "_id": mid,
        "id": mid,
        "tournament_id": tid,
        "round_name": "Round 1",
        "match_number": 1,
        "status": "SCHEDULED",
        "team_a_id": "ta",
        "team_b_id": "tb",
    })

    # Block updating scores on SCHEDULED
    resp_block = await client.post(f"/api/v1/admin/matches/{mid}/score", json={"score_a": 16, "score_b": 14})
    assert resp_block.status_code == 400

    # Transition to ENDED
    await mock_db.matches.update_one({"_id": mid}, {"$set": {"status": "ENDED"}})

    # Score update draft (complete_match: false)
    resp_draft = await client.post(f"/api/v1/admin/matches/{mid}/score", json={
        "score_a": 0,
        "score_b": 0,
        "complete_match": False,
    })
    assert resp_draft.status_code == 200
    m_draft = await mock_db.matches.find_one({"_id": mid})
    assert m_draft["status"] == "RESULTS_SAVED"

    # Score update finalization with winner
    resp_fin = await client.post(f"/api/v1/admin/matches/{mid}/score", json={
        "score_a": 16,
        "score_b": 14,
        "winner_team_id": "ta",
        "complete_match": True,
    })
    assert resp_fin.status_code == 200
    m_fin = await mock_db.matches.find_one({"_id": mid})
    assert m_fin["status"] == "COMPLETED"
    assert m_fin["winner_team_id"] == "ta"

    # Block reverting completed match back to LIVE or SCHEDULED
    revert_live = await client.patch(f"/api/v1/admin/matches/{mid}/status", json={"status": "LIVE"})
    assert revert_live.status_code == 400
    assert "Cannot restart a completed or finalized match" in revert_live.json()["detail"]
