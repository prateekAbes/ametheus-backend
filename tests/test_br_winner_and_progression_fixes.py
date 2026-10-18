import pytest
import pytest_asyncio
from datetime import datetime, timezone
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport
from app.core.database import get_db
from app.main import app
from app.models.tournament import Tournament
from app.models.match import Match
from app.models.user import User
from app.models.base import generate_uuid
from app.api.deps import get_current_user, get_current_user_optional, require_organizer, require_super_admin

@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
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
            user_map[user.id] = user
            headers["Authorization"] = f"Bearer {user.id}"
        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test", headers=headers)
    return _create

@pytest.mark.asyncio
async def test_battle_royale_25_teams_select_10_qualifiers_and_advance(mock_db, make_client):
    """
    Requirements 1, 2, 3:
    1. Create a BR match with 25 teams in Round 1.
    2. Organizer selects 10 qualifiers.
    3. Finalize results.
    4. Verify tournament remains LIVE (not completed).
    5. Verify Round 2 match is created containing exactly the 10 qualifying teams.
    6. Verify results and selections persist upon reopening.
    """
    org_id = f"org_{generate_uuid()[:8]}"
    org_user = User(
        id=org_id,
        username="ApexOrganizer",
        role="ORGANIZER",
        email="apexorg@example.com"
    )
    client = make_client(org_user)

    tourn_id = f"tourn_br_{generate_uuid()[:8]}"
    tourn_doc = {
        "_id": tourn_id,
        "id": tourn_id,
        "title": "Apex Legends Pro Series Season 4",
        "format": "Battle Royale",
        "tournament_type": "Battle Royale",
        "match_format": "Battle Royale",
        "status": "LIVE",
        "created_by": org_id,
        "organizer_id": org_id,
        "room_capacity": 60,
        "players_per_team": 3,
        "server_region": "AWS Mumbai (ap-south-1)",
        "created_at": datetime.now(timezone.utc),
    }
    await mock_db.tournaments.insert_one(tourn_doc)

    # Create 25 teams
    teams_25 = []
    for i in range(1, 26):
        t_id = f"team_apex_{i:02d}"
        teams_25.append({
            "id": t_id,
            "name": f"Apex Squad {i:02d}",
            "captain_id": f"cap_{i:02d}",
            "captain_username": f"Player_{i:02d}",
        })

    match_r1_id = f"match_br_r1_{generate_uuid()[:8]}"
    match_r1_doc = Match(
        id=match_r1_id,
        tournament_id=tourn_id,
        stage_name="Round 1",
        round_name="Round 1",
        stage="Round 1",
        match_type="BATTLE_ROYALE",
        round_number=1,
        match_number=1,
        format="Battle Royale",
        series_format="BO1",
        status="ENDED",
        room_capacity=60,
        players_per_team=3,
        max_teams=25,
        teams=teams_25,
        team_ids=[t["id"] for t in teams_25],
        team_a_id=None,
        team_b_id=None,
        scheduled_time="18:00 IST",
        server_region="AWS Mumbai (ap-south-1)",
    ).to_mongo()
    await mock_db.matches.insert_one(match_r1_doc)

    # Verify 25 teams are present
    res_get = await client.get(f"/api/v1/matches/{match_r1_id}")
    assert res_get.status_code == 200, res_get.text
    match_data = res_get.json()
    assert len(match_data["teams"]) == 25

    # Select top 10 qualifiers
    qualifiers_10 = [teams_25[i]["id"] for i in range(10)]
    assert len(qualifiers_10) == 10

    # Build results list with placements 1 to 25
    results_payload_list = []
    for idx, t in enumerate(teams_25, start=1):
        is_qual = t["id"] in qualifiers_10
        results_payload_list.append({
            "team_id": t["id"],
            "team_name": t["name"],
            "placement": idx,
            "kills": 26 - idx,
            "qualified": is_qual,
        })

    # Submit results & advance
    payload = {
        "results": results_payload_list,
        "qualifier_team_ids": qualifiers_10,
        "qualifiers_count": 10,
        "advance_to_next_round": True,
        "next_round_name": "Round 2 — Semifinals",
        "is_final_round": False,
    }

    res_post = await client.post(
        f"/api/v1/admin/matches/{match_r1_id}/battle-royale-results",
        json=payload,
    )
    assert res_post.status_code == 200, res_post.text
    resp_json = res_post.json()
    assert resp_json["status"] == "success"
    assert resp_json["qualifiers_count"] == 10
    assert len(resp_json["qualifier_team_ids"]) == 10

    # Verify Match 1 is COMPLETED
    updated_r1 = await mock_db.matches.find_one({"_id": match_r1_id})
    assert updated_r1["status"] == "COMPLETED"
    assert updated_r1["qualifier_team_ids"] == qualifiers_10
    assert updated_r1["qualifiers_count"] == 10
    assert updated_r1["next_match_id"] is not None

    # CRITICAL: Verify Tournament is STILL LIVE (NOT completed prematurely!)
    updated_tourn = await mock_db.tournaments.find_one({"_id": tourn_id})
    assert updated_tourn["status"] == "LIVE", f"Expected LIVE but got {updated_tourn['status']}"

    # Verify Round 2 match was created
    next_match_id = updated_r1["next_match_id"]
    match_r2 = await mock_db.matches.find_one({"_id": next_match_id})
    assert match_r2 is not None
    assert match_r2["round_number"] == 2
    assert match_r2["round_name"] == "Round 2 — Semifinals"
    assert match_r2["format"] == "Battle Royale"
    assert match_r2["status"] == "SCHEDULED"
    assert match_r2["team_a_id"] is None
    assert match_r2["team_b_id"] is None

    # Verify Round 2 contains EXACTLY the 10 qualifying teams
    assert len(match_r2["teams"]) == 10
    assert len(match_r2["team_ids"]) == 10
    assert set(match_r2["team_ids"]) == set(qualifiers_10)
    for tm in match_r2["teams"]:
        assert not tm["name"].startswith("Team A")
        assert not tm["name"].startswith("Team B")
        assert "TBD" not in tm["name"]

    # Verify reopening match preserves qualifiers and placements
    res_reopen = await client.get(f"/api/v1/matches/{match_r1_id}")
    assert res_reopen.status_code == 200
    reopened_data = res_reopen.json()
    assert reopened_data["status"] == "COMPLETED"
    assert len(reopened_data["qualifier_team_ids"]) == 10
    assert len(reopened_data["placements"]) == 25


@pytest.mark.asyncio
async def test_battle_royale_idempotency_on_retry(mock_db, make_client):
    """
    Requirement 6:
    Saving the same results twice must not duplicate next-round teams or matches.
    """
    org_id = f"org_{generate_uuid()[:8]}"
    org_user = User(id=org_id, username="ApexOrganizer", role="ORGANIZER", email="apexorg@example.com")
    client = make_client(org_user)

    tourn_id = f"tourn_idem_{generate_uuid()[:8]}"
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "title": "Idempotency Test Tourn",
        "status": "LIVE",
        "created_by": org_id,
    })

    teams_10 = [{"id": f"tm_{i}", "name": f"Team {i}"} for i in range(10)]
    match_id = f"match_idem_{generate_uuid()[:8]}"
    await mock_db.matches.insert_one(Match(
        id=match_id,
        tournament_id=tourn_id,
        round_number=1,
        match_number=1,
        format="Battle Royale",
        status="ENDED",
        teams=teams_10,
        team_ids=[t["id"] for t in teams_10],
    ).to_mongo())

    payload = {
        "qualifier_team_ids": ["tm_0", "tm_1", "tm_2", "tm_3"],
        "advance_to_next_round": True,
        "next_round_name": "Round 2",
        "is_final_round": False,
    }

    # Call 1
    res1 = await client.post(f"/api/v1/admin/matches/{match_id}/battle-royale-results", json=payload)
    assert res1.status_code == 200
    next_m1_id = res1.json()["next_match_id"]

    # Call 2 (Retry / update)
    res2 = await client.post(f"/api/v1/admin/matches/{match_id}/battle-royale-results", json=payload)
    assert res2.status_code == 200
    next_m2_id = res2.json()["next_match_id"]

    # Must reuse the same next match, no duplication!
    assert next_m1_id == next_m2_id

    # Verify only 1 match exists for round 2
    r2_matches = await mock_db.matches.find({"tournament_id": tourn_id, "round_number": 2}).to_list(10)
    assert len(r2_matches) == 1
    assert len(r2_matches[0]["teams"]) == 4


@pytest.mark.asyncio
async def test_multi_round_progression_to_final_champion(mock_db, make_client):
    """
    Requirement 3 & 7:
    Full workflow:
    Round 1 (25 teams) -> 10 qualifiers
    Round 2 (10 teams) -> 3 qualifiers
    Round 3 Final (3 teams) -> 1 champion (tournament completes only here!)
    """
    org_id = f"org_{generate_uuid()[:8]}"
    org_user = User(id=org_id, username="Organizer", role="ORGANIZER", email="org@example.com")
    client = make_client(org_user)

    tourn_id = f"tourn_full_{generate_uuid()[:8]}"
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "title": "Championship BR Tourn",
        "status": "LIVE",
        "created_by": org_id,
    })

    # Round 1
    t25 = [{"id": f"t_{i:02d}", "name": f"Squad {i:02d}"} for i in range(1, 26)]
    m1_id = f"m1_{generate_uuid()[:8]}"
    await mock_db.matches.insert_one(Match(
        id=m1_id,
        tournament_id=tourn_id,
        round_number=1,
        format="Battle Royale",
        status="ENDED",
        teams=t25,
        team_ids=[t["id"] for t in t25],
    ).to_mongo())

    # Advance 10 teams from Round 1
    q10 = [t["id"] for t in t25[:10]]
    r1_res = await client.post(f"/api/v1/admin/matches/{m1_id}/battle-royale-results", json={
        "qualifier_team_ids": q10,
        "advance_to_next_round": True,
        "next_round_name": "Round 2",
        "is_final_round": False,
    })
    assert r1_res.status_code == 200
    m2_id = r1_res.json()["next_match_id"]

    # Tournament still LIVE
    t_live_1 = await mock_db.tournaments.find_one({"_id": tourn_id})
    assert t_live_1["status"] == "LIVE"

    # Advance 3 teams from Round 2 (gameplay concludes: marked ENDED)
    await mock_db.matches.update_one({"_id": m2_id}, {"$set": {"status": "ENDED"}})
    q3 = q10[:3]
    r2_res = await client.post(f"/api/v1/admin/matches/{m2_id}/battle-royale-results", json={
        "qualifier_team_ids": q3,
        "advance_to_next_round": True,
        "next_round_name": "Grand Finals",
        "is_final_round": False,
    })
    assert r2_res.status_code == 200
    m3_id = r2_res.json()["next_match_id"]

    # Verify Round 3 has 3 teams
    m3 = await mock_db.matches.find_one({"_id": m3_id})
    assert len(m3["teams"]) == 3
    assert set(m3["team_ids"]) == set(q3)

    # Tournament STILL LIVE!
    t_live_2 = await mock_db.tournaments.find_one({"_id": tourn_id})
    assert t_live_2["status"] == "LIVE"

    # Final Round (Round 3) Finalization (gameplay concludes: marked ENDED)
    await mock_db.matches.update_one({"_id": m3_id}, {"$set": {"status": "ENDED"}})
    champion_team_id = q3[0]
    final_res = await client.post(f"/api/v1/admin/matches/{m3_id}/battle-royale-results", json={
        "results": [
            {"team_id": q3[0], "team_name": "Squad 01", "placement": 1, "kills": 12},
            {"team_id": q3[1], "team_name": "Squad 02", "placement": 2, "kills": 5},
            {"team_id": q3[2], "team_name": "Squad 03", "placement": 3, "kills": 2},
        ],
        "is_final_round": True,
    })
    assert final_res.status_code == 200
    final_data = final_res.json()
    assert final_data["is_final_round"] is True
    assert final_data["winner_team_id"] == champion_team_id

    # Tournament is ONLY NOW COMPLETED!
    t_completed = await mock_db.tournaments.find_one({"_id": tourn_id})
    assert t_completed["status"] == "COMPLETED"
    assert t_completed["winner_team_id"] == champion_team_id


@pytest.mark.asyncio
async def test_knockout_regression_does_not_close_prematurely(mock_db, make_client):
    """
    Requirement 3 & 7:
    Verify normal 2-team knockout bracket progression is preserved
    and does not close the tournament on Round 1 match completion.
    """
    org_id = f"org_{generate_uuid()[:8]}"
    org_user = User(id=org_id, username="Organizer", role="ORGANIZER", email="org@example.com")
    client = make_client(org_user)

    tourn_id = f"tourn_ko_{generate_uuid()[:8]}"
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "title": "Knockout Championship",
        "format": "Knockout",
        "status": "LIVE",
        "created_by": org_id,
    })

    # Semi-Final Match (Round 1)
    m_semi_id = f"m_semi_{generate_uuid()[:8]}"
    # Grand Final Match (Round 2)
    m_final_id = f"m_final_{generate_uuid()[:8]}"

    await mock_db.matches.insert_one(Match(
        id=m_semi_id,
        tournament_id=tourn_id,
        round_number=1,
        round_name="Semifinals",
        stage="Semifinals",
        match_number=1,
        format="BO1",
        status="LIVE",
        team_a_id="team_alpha",
        team_a_name="Team Alpha",
        team_b_id="team_beta",
        team_b_name="Team Beta",
        next_match_id=m_final_id,
        next_match_slot="A",
    ).to_mongo())

    await mock_db.matches.insert_one(Match(
        id=m_final_id,
        tournament_id=tourn_id,
        round_number=2,
        round_name="Grand Final",
        stage="Grand Final",
        match_number=1,
        format="BO1",
        status="SCHEDULED",
        team_a_id=None,
        team_b_id="team_gamma",
        team_b_name="Team Gamma",
    ).to_mongo())

    # Complete Semifinal Match
    res_semi = await client.post(f"/api/v1/admin/matches/{m_semi_id}/complete", json={
        "score_a": 16,
        "score_b": 10,
        "winner_team_id": "team_alpha",
    })
    assert res_semi.status_code == 200

    # Verify team_alpha advanced to Slot A in Grand Final
    final_match = await mock_db.matches.find_one({"_id": m_final_id})
    assert final_match["team_a_id"] == "team_alpha"
    assert final_match["team_a_name"] == "Team Alpha"

    # Tournament must NOT be COMPLETED because Grand Final is still SCHEDULED!
    tourn_check = await mock_db.tournaments.find_one({"_id": tourn_id})
    assert tourn_check["status"] == "LIVE"


@pytest.mark.asyncio
async def test_br_authorization_and_validation(mock_db, make_client):
    """
    Requirement 1 & 7:
    - Unauthorized user rejected (403)
    - Team not in match rejected (400)
    - Duplicate qualifier selection rejected (400)
    - Mismatched qualifier count rejected (400)
    """
    org_id = f"org_{generate_uuid()[:8]}"
    stranger_id = f"stranger_{generate_uuid()[:8]}"

    org_user = User(id=org_id, username="Organizer", role="ORGANIZER", email="org@example.com")
    stranger_user = User(id=stranger_id, username="Stranger", role="ORGANIZER", email="stranger@example.com")

    tourn_id = f"tourn_val_{generate_uuid()[:8]}"
    await mock_db.tournaments.insert_one({
        "_id": tourn_id,
        "title": "Validation Tourn",
        "status": "LIVE",
        "created_by": org_id,
    })

    teams = [{"id": f"team_{i}", "name": f"Team {i}"} for i in range(5)]
    m_id = f"m_val_{generate_uuid()[:8]}"
    await mock_db.matches.insert_one(Match(
        id=m_id,
        tournament_id=tourn_id,
        round_number=1,
        format="Battle Royale",
        status="ENDED",
        teams=teams,
        team_ids=[t["id"] for t in teams],
    ).to_mongo())

    client_stranger = make_client(stranger_user)
    client_org = make_client(org_user)

    # 1. Stranger cannot submit
    res_unauth = await client_stranger.post(f"/api/v1/admin/matches/{m_id}/battle-royale-results", json={
        "qualifier_team_ids": ["team_0"],
    })
    assert res_unauth.status_code == 403

    # 2. Team not in match rejected
    res_alien = await client_org.post(f"/api/v1/admin/matches/{m_id}/battle-royale-results", json={
        "qualifier_team_ids": ["team_0", "team_intruder_99"],
    })
    assert res_alien.status_code == 400
    assert "does not belong to this match" in res_alien.text

    # 3. Duplicate selection rejected
    res_dup = await client_org.post(f"/api/v1/admin/matches/{m_id}/battle-royale-results", json={
        "qualifier_team_ids": ["team_0", "team_0"],
    })
    assert res_dup.status_code == 400
    assert "Duplicate" in res_dup.text

    # 4. Qualifier count mismatch rejected
    res_count = await client_org.post(f"/api/v1/admin/matches/{m_id}/battle-royale-results", json={
        "qualifier_team_ids": ["team_0", "team_1"],
        "qualifiers_count": 3,
    })
    assert res_count.status_code == 400
    assert "Expected exactly 3 qualifiers" in res_count.text
