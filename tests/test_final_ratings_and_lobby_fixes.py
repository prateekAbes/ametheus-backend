import pytest
import pytest_asyncio
from datetime import datetime, timezone
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport

from app.core.database import get_db, set_database
from app.main import app
from app.models.user import User
from app.models.base import generate_uuid
from app.api.deps import get_current_user, get_current_user_optional, require_organizer, require_super_admin
from app.services.rating_service import (
    resolve_match_players,
    get_or_create_player_rating,
    apply_manual_rating_adjustment,
)

@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_final_ratings_test"]
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
        id="admin_001",
        email="admin@ametheus.gg",
        username="AmetheusAdmin",
        display_name="Admin",
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
# 1. RATING IDENTITY RESOLUTION UNIT TESTS
# =========================================================================

@pytest.mark.asyncio
async def test_resolve_match_players_squad_teams(mock_db):
    """
    Verifies that resolve_match_players extracts authentic user IDs from team members,
    initializes their base MMR (500), and never outputs team IDs as player user IDs.
    """
    u1 = create_player("usr_alpha", "player_alpha", "Alpha Contender")
    u2 = create_player("usr_beta", "player_beta", "Beta Contender")
    await mock_db.users.insert_many([u1, u2])

    team_a = {
        "_id": "team_alpha",
        "id": "team_alpha",
        "name": "Team Alpha",
        "captain_id": "usr_alpha",
        "players": [{"user_id": "usr_alpha"}],
    }
    team_b = {
        "_id": "team_beta",
        "id": "team_beta",
        "name": "Team Beta",
        "captain_id": "usr_beta",
        "players": [{"user_id": "usr_beta"}],
    }
    await mock_db.teams.insert_many([team_a, team_b])

    match = {
        "_id": "m_squad_1",
        "id": "m_squad_1",
        "tournament_id": "tourn_01",
        "game_id": "game_bgmi",
        "team_a_id": "team_alpha",
        "team_b_id": "team_beta",
        "teams": [team_a, team_b],
    }

    players = await resolve_match_players(match, mock_db)
    assert len(players) == 2

    p_ids = [p["user_id"] for p in players]
    assert "usr_alpha" in p_ids
    assert "usr_beta" in p_ids
    assert "team_alpha" not in p_ids
    assert "team_beta" not in p_ids

    alpha = next(p for p in players if p["user_id"] == "usr_alpha")
    assert alpha["display_name"] == "Alpha Contender"
    assert alpha["team_name"] == "Team Alpha"
    assert alpha["current_rating"] == 500


@pytest.mark.asyncio
async def test_resolve_match_players_solo_contenders(mock_db):
    """
    Verifies that solo contenders are properly resolved from tournament registrations
    when no team rosters exist.
    """
    u1 = create_player("usr_solo1", "lone_wolf", "Lone Wolf")
    await mock_db.users.insert_one(u1)

    reg = {
        "_id": "reg_001",
        "tournament_id": "tourn_solo_1",
        "player_id": "usr_solo1",
        "status": "APPROVED",
    }
    await mock_db.tournament_registrations.insert_one(reg)

    match = {
        "_id": "m_solo_1",
        "id": "m_solo_1",
        "tournament_id": "tourn_solo_1",
        "game_id": "game_chess",
        "teams": [],
    }

    players = await resolve_match_players(match, mock_db)
    assert len(players) == 1
    assert players[0]["user_id"] == "usr_solo1"
    assert players[0]["team_name"] == "Solo Contender"
    assert players[0]["team_id"] is None
    assert players[0]["current_rating"] == 500


# =========================================================================
# 2. MATCH RATINGS API & IDEMPOTENT SKILL ADJUSTMENT TESTS
# =========================================================================

@pytest.mark.asyncio
async def test_match_ratings_requires_completed_status(mock_db, make_client):
    admin = create_admin()
    client = make_client(admin)

    match = {
        "_id": "m_live_1",
        "id": "m_live_1",
        "tournament_id": "tourn_01",
        "status": "LIVE",
    }
    await mock_db.matches.insert_one(match)

    res = await client.post(
        "/api/v1/admin/matches/m_live_1/ratings",
        json={
            "ratings": [{"user_id": "usr_1", "rating": 8}],
        },
    )
    assert res.status_code == 400
    assert "completed" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_match_ratings_validation_and_persistence(mock_db, make_client):
    admin = create_admin()
    client = make_client(admin)

    u1 = create_player("usr_val1", "val_player", "Val Player")
    await mock_db.users.insert_one(u1)

    team1 = {
        "_id": "team_val",
        "id": "team_val",
        "name": "Team Val",
        "captain_id": "usr_val1",
        "players": [{"user_id": "usr_val1"}],
    }
    await mock_db.teams.insert_one(team1)

    match = {
        "_id": "m_comp_1",
        "id": "m_comp_1",
        "tournament_id": "tourn_01",
        "game_id": "game_bgmi",
        "status": "COMPLETED",
        "teams": [team1],
    }
    await mock_db.matches.insert_one(match)

    # Rejects invalid scale < 1 or > 10
    bad_res = await client.post(
        "/api/v1/admin/matches/m_comp_1/ratings",
        json={"ratings": [{"user_id": "usr_val1", "rating": 12}]},
    )
    assert bad_res.status_code == 400

    # Submits valid rating
    good_res = await client.post(
        "/api/v1/admin/matches/m_comp_1/ratings",
        json={
            "ratings": [
                {
                    "user_id": "usr_val1",
                    "rating": 9,
                    "review": "Exceptional flanking in late circles.",
                }
            ],
            "submission_id": "sub_test_01",
        },
    )
    assert good_res.status_code == 200
    body = good_res.json()
    assert body["status"] == "success"
    assert body["total_rated"] == 1

    # Check match doc updated
    m_doc = await mock_db.matches.find_one({"_id": "m_comp_1"})
    assert len(m_doc["participant_ratings"]) == 1
    assert m_doc["participant_ratings"][0]["user_id"] == "usr_val1"
    assert m_doc["participant_ratings"][0]["rating"] == 9

    # Check GET endpoint returns real player profile
    get_res = await client.get("/api/v1/admin/matches/m_comp_1/ratings")
    assert get_res.status_code == 200
    get_body = get_res.json()
    assert len(get_body["participants"]) == 1
    p = get_body["participants"][0]
    assert p["user_id"] == "usr_val1"
    assert p["display_name"] == "Val Player"
    assert p["rating"] == 9
    assert p["rated"] is True


@pytest.mark.asyncio
async def test_match_ratings_skill_adjustment_and_idempotency(mock_db, make_client):
    admin = create_admin()
    client = make_client(admin)

    u1 = create_player("usr_adj1", "skill_pro", "Skill Pro")
    await mock_db.users.insert_one(u1)
    team1 = {
        "_id": "team_skill",
        "id": "team_skill",
        "name": "Team Skill",
        "captain_id": "usr_adj1",
        "players": [{"user_id": "usr_adj1"}],
    }
    await mock_db.teams.insert_one(team1)

    match = {
        "_id": "m_comp_adj",
        "id": "m_comp_adj",
        "tournament_id": "tourn_01",
        "game_id": "game_bgmi",
        "status": "COMPLETED",
        "teams": [team1],
    }
    await mock_db.matches.insert_one(match)

    payload = {
        "ratings": [
            {
                "user_id": "usr_adj1",
                "player_id": "usr_adj1",
                "rating": 9.5,
                "skill_adjustment": 25,
                "adjustment_reason": "Clutch MVP",
            }
        ],
        "submission_id": "idemp_key_101",
    }

    # First submission: increases rating 500 -> 525
    res1 = await client.post("/api/v1/admin/matches/m_comp_adj/ratings", json=payload)
    assert res1.status_code == 200

    pr_doc = await mock_db.player_ratings.find_one({"user_id": "usr_adj1"})
    assert pr_doc is not None
    assert pr_doc["rating"] == 525

    # Second submission with SAME submission_id: must NOT double-adjust!
    res2 = await client.post("/api/v1/admin/matches/m_comp_adj/ratings", json=payload)
    assert res2.status_code == 200

    pr_doc_again = await mock_db.player_ratings.find_one({"user_id": "usr_adj1"})
    assert pr_doc_again["rating"] == 525  # Still 525, not 550!


# =========================================================================
# 3. ROUND REVIEW WORKFLOW TESTS
# =========================================================================

@pytest.mark.asyncio
async def test_round_review_incomplete_matches_reporting(mock_db, make_client):
    admin = create_admin()
    client = make_client(admin)

    tourn = {
        "_id": "tourn_rnd_1",
        "id": "tourn_rnd_1",
        "title": "Ametheus Championship",
        "status": "LIVE",
    }
    await mock_db.tournaments.insert_one(tourn)

    # 1 match completed, 1 match scheduled
    m1 = {
        "_id": "m_r1_1",
        "id": "m_r1_1",
        "tournament_id": "tourn_rnd_1",
        "round_name": "Round 1",
        "status": "COMPLETED",
    }
    m2 = {
        "_id": "m_r1_2",
        "id": "m_r1_2",
        "tournament_id": "tourn_rnd_1",
        "round_name": "Round 1",
        "status": "SCHEDULED",
    }
    await mock_db.matches.insert_many([m1, m2])

    res = await client.get("/api/v1/admin/tournaments/tourn_rnd_1/rounds/Round%201/review")
    assert res.status_code == 200
    body = res.json()
    assert body["round_name"] == "Round 1"
    assert body["total_matches"] == 2
    assert body["completed_matches"] == 1
    assert body["all_matches_completed"] is False
    assert body["is_round_finalized"] is False


@pytest.mark.asyncio
async def test_round_review_complete_aggregates_standings_and_qualifiers(mock_db, make_client):
    admin = create_admin()
    client = make_client(admin)

    tourn = {
        "_id": "tourn_rnd_2",
        "id": "tourn_rnd_2",
        "title": "Ametheus BR Championship",
        "status": "LIVE",
    }
    await mock_db.tournaments.insert_one(tourn)

    # Completed BR match with placements
    m1 = {
        "_id": "m_br_r1",
        "id": "m_br_r1",
        "tournament_id": "tourn_rnd_2",
        "round_name": "Round 1",
        "status": "COMPLETED",
        "battle_royale_results": [
            {"team_id": "team_1", "team_name": "Squad One", "total_points": 25, "kills": 15, "placement": 1, "qualified": True},
            {"team_id": "team_2", "team_name": "Squad Two", "total_points": 18, "kills": 8, "placement": 2, "qualified": True},
            {"team_id": "team_3", "team_name": "Squad Three", "total_points": 5, "kills": 1, "placement": 3, "qualified": False},
        ],
    }
    await mock_db.matches.insert_one(m1)

    res = await client.get("/api/v1/admin/tournaments/tourn_rnd_2/rounds/Round%201/review")
    assert res.status_code == 200
    body = res.json()
    assert body["total_matches"] == 1
    assert body["completed_matches"] == 1
    assert body["all_matches_completed"] is True
    assert len(body["standings"]) == 3
    assert body["standings"][0]["participant_id"] == "team_1"
    assert body["standings"][0]["rank"] == 1
    assert "team_1" in body["proposed_qualifiers"]
    assert "team_2" in body["proposed_qualifiers"]


# =========================================================================
# 4. ROUND RATINGS SUBMISSION TESTS
# =========================================================================

@pytest.mark.asyncio
async def test_round_ratings_submission_across_multiple_matches(mock_db, make_client):
    admin = create_admin()
    client = make_client(admin)

    u1 = create_player("usr_r_1", "player_r1", "Player R1")
    u2 = create_player("usr_r_2", "player_r2", "Player R2")
    await mock_db.users.insert_many([u1, u2])

    team1 = {
        "_id": "t_r1",
        "id": "t_r1",
        "name": "Team R1",
        "captain_id": "usr_r_1",
        "players": [{"user_id": "usr_r_1"}],
    }
    team2 = {
        "_id": "t_r2",
        "id": "t_r2",
        "name": "Team R2",
        "captain_id": "usr_r_2",
        "players": [{"user_id": "usr_r_2"}],
    }
    await mock_db.teams.insert_many([team1, team2])

    tourn = {
        "_id": "tourn_rnd_rat",
        "id": "tourn_rnd_rat",
        "game_id": "game_bgmi",
    }
    await mock_db.tournaments.insert_one(tourn)

    m1 = {
        "_id": "m_rr_1",
        "id": "m_rr_1",
        "tournament_id": "tourn_rnd_rat",
        "round_name": "Quarterfinals",
        "status": "COMPLETED",
        "teams": [team1],
    }
    m2 = {
        "_id": "m_rr_2",
        "id": "m_rr_2",
        "tournament_id": "tourn_rnd_rat",
        "round_name": "Quarterfinals",
        "status": "COMPLETED",
        "teams": [team2],
    }
    await mock_db.matches.insert_many([m1, m2])

    payload = {
        "ratings": [
            {
                "user_id": "usr_r_1",
                "player_id": "usr_r_1",
                "match_id": "m_rr_1",
                "rating": 8.5,
                "skill_adjustment": 15,
                "notes": "Solid defense",
            },
            {
                "user_id": "usr_r_2",
                "player_id": "usr_r_2",
                "match_id": "m_rr_2",
                "rating": 9.0,
                "skill_adjustment": 20,
                "notes": "Great clutch",
            },
        ],
        "submission_id": "sub_rnd_ratings_01",
    }

    res = await client.post(
        "/api/v1/admin/tournaments/tourn_rnd_rat/rounds/Quarterfinals/ratings",
        json=payload,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["total_rated"] == 2

    # Verify ratings in match_ratings collection
    mr1 = await mock_db.match_ratings.find_one({"user_id": "usr_r_1", "round_name": "Quarterfinals"})
    assert mr1 is not None
    assert mr1["rating"] == 8.5
    assert mr1["skill_adjustment"] == 15

    # Verify MMR updated for both players
    pr1 = await mock_db.player_ratings.find_one({"user_id": "usr_r_1"})
    pr2 = await mock_db.player_ratings.find_one({"user_id": "usr_r_2"})
    assert pr1["rating"] == 515
    assert pr2["rating"] == 520


# =========================================================================
# 5. ROUND FINALIZATION & QUALIFICATION PERSISTENCE TESTS
# =========================================================================

@pytest.mark.asyncio
async def test_finalize_round_rejects_incomplete_matches(mock_db, make_client):
    admin = create_admin()
    client = make_client(admin)

    tourn = {
        "_id": "tourn_fin_fail",
        "id": "tourn_fin_fail",
        "status": "LIVE",
    }
    await mock_db.tournaments.insert_one(tourn)

    m1 = {
        "_id": "m_fin_incomplete",
        "id": "m_fin_incomplete",
        "tournament_id": "tourn_fin_fail",
        "round_name": "Semifinals",
        "status": "LIVE",
    }
    await mock_db.matches.insert_one(m1)

    res = await client.post(
        "/api/v1/admin/tournaments/tourn_fin_fail/rounds/Semifinals/finalize",
        json={"qualifier_ids": ["team_a"]},
    )
    assert res.status_code == 400
    assert "not yet completed" in res.json()["detail"].lower()


@pytest.mark.asyncio
async def test_finalize_round_persists_qualifications_no_auto_next_round(mock_db, make_client):
    admin = create_admin()
    client = make_client(admin)

    tourn = {
        "_id": "tourn_fin_ok",
        "id": "tourn_fin_ok",
        "status": "LIVE",
        "stages": [
            {
                "name": "Stage 1",
                "rounds": [
                    {"id": "r_semi", "name": "Semifinals", "status": "ACTIVE"}
                ],
            }
        ],
    }
    await mock_db.tournaments.insert_one(tourn)

    team1 = {"_id": "t_qual_1", "id": "t_qual_1", "name": "Phoenix Squad"}
    team2 = {"_id": "t_qual_2", "id": "t_qual_2", "name": "Viper Squad"}
    await mock_db.teams.insert_many([team1, team2])

    m1 = {
        "_id": "m_fin_done",
        "id": "m_fin_done",
        "tournament_id": "tourn_fin_ok",
        "round_name": "Semifinals",
        "status": "COMPLETED",
    }
    await mock_db.matches.insert_one(m1)

    payload = {
        "qualifier_ids": ["t_qual_1", "t_qual_2"],
        "round_number": 1,
        "notes": "Top 2 advanced by points",
    }

    res = await client.post(
        "/api/v1/admin/tournaments/tourn_fin_ok/rounds/Semifinals/finalize",
        json=payload,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "success"
    assert body["qualified_count"] == 2

    # Check tournament_qualifications collection
    quals = await mock_db.tournament_qualifications.find({"tournament_id": "tourn_fin_ok"}).to_list(10)
    assert len(quals) == 2
    qual_pids = [q["participant_id"] for q in quals]
    assert "t_qual_1" in qual_pids
    assert "t_qual_2" in qual_pids
    assert all(q["status"] == "QUALIFIED" for q in quals)

    # Check tournament stage marked FINALIZED
    t_doc = await mock_db.tournaments.find_one({"_id": "tourn_fin_ok"})
    rnd_doc = t_doc["stages"][0]["rounds"][0]
    assert rnd_doc["status"] == "FINALIZED"
    assert rnd_doc["is_finalized"] is True

    # CRITICAL: Verify NO automatic next round was created!
    matches_after = await mock_db.matches.find({"tournament_id": "tourn_fin_ok"}).to_list(10)
    assert len(matches_after) == 1  # Only original m_fin_done exists, no auto-created Round 2!


# =========================================================================
# 6. EDIT LOBBY / CREDENTIALS SCOPING & RELEASE TESTS
# =========================================================================

@pytest.mark.asyncio
async def test_lobby_credentials_persistence_and_release(mock_db, make_client):
    admin = create_admin()
    client = make_client(admin)

    match = {
        "_id": "m_cred_01",
        "id": "m_cred_01",
        "tournament_id": "tourn_01",
        "status": "SCHEDULED",
        "credentials_released": False,
    }
    await mock_db.matches.insert_one(match)

    # 1. Save confidential credentials
    save_res = await client.patch(
        "/api/v1/admin/matches/m_cred_01/credentials",
        json={
            "room_id": "AMT-ROOM-101",
            "room_password": "SECRET_PASS_99",
            "custom_instructions": "Join slot 1-4. Map: Erangel.",
        },
    )
    assert save_res.status_code == 200

    m_after_save = await mock_db.matches.find_one({"_id": "m_cred_01"})
    assert m_after_save["room_id"] == "AMT-ROOM-101"
    assert m_after_save["room_password"] == "SECRET_PASS_99"
    assert m_after_save["credentials_released"] is False  # Confidential!

    # 2. Release credentials
    rel_res = await client.post("/api/v1/admin/matches/m_cred_01/release-credentials")
    assert rel_res.status_code == 200

    m_after_rel = await mock_db.matches.find_one({"_id": "m_cred_01"})
    assert m_after_rel["credentials_released"] is True
    assert m_after_rel["status"] == "SCHEDULED"
