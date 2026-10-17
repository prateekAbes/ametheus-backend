import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone

from app.main import app
from app.core.database import get_db
from app.models.user import User
from app.models.tournament import Tournament
from app.models.match import Match
from app.models.qualification import TournamentQualification
from app.api.deps import get_current_user, get_current_user_optional, require_super_admin, require_organizer


@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
    return db


@pytest.fixture
def make_client(mock_db):
    def _create(user: User):
        app.dependency_overrides[get_db] = lambda: mock_db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_current_user_optional] = lambda: user
        if user.role in ["SUPER_ADMIN", "ORGANIZER"]:
            app.dependency_overrides[require_super_admin] = lambda: user
            app.dependency_overrides[require_organizer] = lambda: user
        else:
            async def _forbidden():
                from fastapi import HTTPException
                raise HTTPException(status_code=403, detail="Forbidden: Organizer or Super Admin required")
            app.dependency_overrides[require_super_admin] = _forbidden
            app.dependency_overrides[require_organizer] = _forbidden

        transport = ASGITransport(app=app)
        return AsyncClient(transport=transport, base_url="http://test")
    return _create


@pytest.mark.asyncio
async def test_round_stream_persistence_and_match_inheritance(mock_db, make_client):
    """
    Test 1: Round Stream Persistence & Match Inheritance
    - Broadcast URLs strictly belong to rounds, not individual matches.
    - Saving round stream persists in tournament and propagates to matches in that round.
    """
    admin = User(
        id="usr_admin_stream",
        clerk_user_id="clerk_admin_stream",
        email="admin@ametheus.gg",
        username="stream_admin",
        role="SUPER_ADMIN",
    )
    await mock_db.users.insert_one(admin.to_mongo())

    now_str = datetime.now(timezone.utc).isoformat()
    tourn = Tournament(
        id="tourn_stream_01",
        title="BGMI Pro Series",
        slug="bgmi-pro-series",
        game_id="bgmi",
        format="Knockout",
        status="IN_PROGRESS",
        max_teams=16,
        registered_teams=16,
        min_team_size=4,
        max_team_size=4,
        starts_at=now_str,
        roster_lock_time=now_str,
        entry_fee_inr=0,
        prize_pool_inr=10000,
        round_streams={},
    )
    await mock_db.tournaments.insert_one(tourn.to_mongo())

    match1 = Match(
        id="match_stream_01",
        tournament_id="tourn_stream_01",
        stage="Round 1",
        round_name="Round 1",
        round_number=1,
        match_number=1,
        format="Knockout",
        status="SCHEDULED",
        team_a_id="team_alpha",
        team_b_id="team_beta",
    )
    await mock_db.matches.insert_one(match1.to_mongo())

    client = make_client(admin)

    # Update Round 1 stream
    resp = await client.patch(
        "/api/v1/admin/tournaments/tourn_stream_01/rounds/Round 1/stream",
        json={"stream_url": "https://www.youtube.com/watch?v=live_bgmi_round1"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "success"
    assert data["round_name"] == "Round 1"
    assert data["stream_url"] == "https://www.youtube.com/watch?v=live_bgmi_round1"

    # Verify tournament document in database
    tourn_doc = await mock_db.tournaments.find_one({"$or": [{"_id": "tourn_stream_01"}, {"id": "tourn_stream_01"}]})
    assert tourn_doc["round_streams"]["Round 1"] == "https://www.youtube.com/watch?v=live_bgmi_round1"

    # Verify match inherited round stream URL
    match_doc = await mock_db.matches.find_one({"$or": [{"_id": "match_stream_01"}, {"id": "match_stream_01"}]})
    assert match_doc["youtube_live_url"] == "https://www.youtube.com/watch?v=live_bgmi_round1"


@pytest.mark.asyncio
async def test_credential_confidentiality_and_controlled_release(mock_db, make_client):
    """
    Test 2: Credential Confidentiality and Controlled Release
    - Room credentials stay strictly confidential until explicit release.
    - Standard players cannot see unreleased credentials.
    - Releasing credentials opens check-in and makes credentials accessible.
    """
    admin = User(
        id="usr_admin_cred",
        clerk_user_id="clerk_admin_cred",
        email="admin@ametheus.gg",
        username="cred_admin",
        role="SUPER_ADMIN",
    )
    player = User(
        id="usr_player_cred",
        clerk_user_id="clerk_player_cred",
        email="player@ametheus.gg",
        username="pro_gamer",
        role="USER",
    )
    await mock_db.users.insert_one(admin.to_mongo())
    await mock_db.users.insert_one(player.to_mongo())

    team_a = {
        "_id": "team_a_id",
        "id": "team_a_id",
        "name": "Team A",
        "captain_id": "usr_player_cred",
    }
    await mock_db.teams.insert_one(team_a)

    reg = {
        "_id": "reg_cred_01",
        "tournament_id": "tourn_cred_01",
        "captain_id": "usr_player_cred",
        "player_id": "usr_player_cred",
        "team_id": "team_a_id",
        "payment_status": "FREE",
    }
    await mock_db.tournament_registrations.insert_one(reg)

    match = Match(
        id="match_cred_01",
        tournament_id="tourn_cred_01",
        stage="Round 1",
        round_name="Round 1",
        format="Knockout",
        status="SCHEDULED",
        team_a_id="team_a_id",
        team_b_id="team_b_id",
        credentials_released=False,
    )
    await mock_db.matches.insert_one(match.to_mongo())

    # 1. Admin saves confidential credentials
    admin_client = make_client(admin)
    save_resp = await admin_client.patch(
        "/api/v1/admin/matches/match_cred_01/credentials",
        json={
            "room_id": "BGMI-ROOM-99",
            "room_password": "SECRET_PASSWORD_XYZ",
            "custom_instructions": "Slot 1-4 for Team A. No emulators.",
        },
    )
    assert save_resp.status_code == 200, save_resp.text
    assert save_resp.json()["credentials_released"] is False

    # 2. Player queries match list: credentials must be masked
    player_client = make_client(player)
    list_resp = await player_client.get("/api/v1/matches?tournament_id=tourn_cred_01")
    assert list_resp.status_code == 200
    matches_list = list_resp.json()
    assert len(matches_list) == 1
    m_data = matches_list[0]
    assert m_data.get("lobby_code") is None
    assert m_data.get("lobby_password") is None
    assert m_data.get("room_id") is None
    assert m_data.get("room_password") is None

    # 3. Player queries join info: credentials must be masked
    join_resp = await player_client.get("/api/v1/matches/match_cred_01/join-info")
    assert join_resp.status_code == 200
    join_data = join_resp.json()
    assert join_data["credentials_released"] is False
    assert join_data["room_id"] is None
    assert join_data["room_password"] is None

    # 4. Admin releases credentials
    admin_client2 = make_client(admin)
    rel_resp = await admin_client2.post("/api/v1/admin/matches/match_cred_01/release-credentials")
    assert rel_resp.status_code == 200
    assert rel_resp.json()["status"] == "CHECK_IN_OPEN"
    assert rel_resp.json()["credentials_released"] is True

    # 5. Player now queries join info: credentials are visible
    player_client2 = make_client(player)
    join_resp2 = await player_client2.get("/api/v1/matches/match_cred_01/join-info")
    assert join_resp2.status_code == 200
    join_data2 = join_resp2.json()
    assert join_data2["credentials_released"] is True
    assert join_data2["room_id"] == "BGMI-ROOM-99"
    assert join_data2["room_password"] == "SECRET_PASSWORD_XYZ"
    assert join_data2["custom_instructions"] == "Slot 1-4 for Team A. No emulators."


@pytest.mark.asyncio
async def test_check_in_does_not_force_live(mock_db, make_client):
    """
    Test 3: Checking in must NOT automatically start the match or set it live.
    - Match status must remain CHECK_IN_OPEN or SCHEDULED.
    """
    player = User(
        id="usr_captain_01",
        clerk_user_id="clerk_captain_01",
        email="captain@ametheus.gg",
        username="captain_alpha",
        role="USER",
    )
    await mock_db.users.insert_one(player.to_mongo())

    # Create team with captain
    team = {
        "_id": "team_alpha_01",
        "id": "team_alpha_01",
        "name": "Team Alpha",
        "captain_id": "usr_captain_01",
    }
    await mock_db.teams.insert_one(team)

    match = Match(
        id="match_checkin_01",
        tournament_id="tourn_checkin_01",
        stage="Round 1",
        round_name="Round 1",
        format="Knockout",
        status="CHECK_IN_OPEN",
        team_a_id="team_alpha_01",
        team_b_id="team_beta_01",
        credentials_released=True,
    )
    await mock_db.matches.insert_one(match.to_mongo())

    client = make_client(player)
    resp = await client.post("/api/v1/matches/match_checkin_01/check-in")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "success"
    assert data["check_in_status"]["team_alpha_01"] is True

    # Critical check: match in database must NOT be LIVE
    db_match = await mock_db.matches.find_one({"$or": [{"_id": "match_checkin_01"}, {"id": "match_checkin_01"}]})
    assert db_match["status"] == "CHECK_IN_OPEN"
    assert db_match["status"] != "LIVE"
    assert db_match["check_in_status"]["team_alpha_01"] is True
    assert any(c.get("team_id") == "team_alpha_01" for c in db_match.get("check_ins", []))


@pytest.mark.asyncio
async def test_player_score_submission_rejected_with_403(mock_db, make_client):
    """
    Test 4: Score Submission Access Control
    - Players/participants are strictly prohibited from submitting match scores.
    - Must return HTTP 403 Forbidden.
    """
    player = User(
        id="usr_rogue_player",
        clerk_user_id="clerk_rogue",
        email="rogue@ametheus.gg",
        username="rogue_player",
        role="USER",
    )
    await mock_db.users.insert_one(player.to_mongo())

    match = Match(
        id="match_access_01",
        tournament_id="tourn_access_01",
        stage="Round 1",
        round_name="Round 1",
        format="Knockout",
        status="LIVE",
        team_a_id="team_alpha",
        team_b_id="team_beta",
    )
    await mock_db.matches.insert_one(match.to_mongo())

    client = make_client(player)

    # Attempt score submission as player
    resp = await client.post(
        "/api/v1/matches/match_access_01/submit-result",
        json={"score_a": 16, "score_b": 10, "winner_team_id": "team_alpha"},
    )
    assert resp.status_code == 403, f"Expected 403 Forbidden, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_manual_winner_qualification_no_auto_progression(mock_db, make_client):
    """
    Test 5: No Auto-Progression of Winners
    - Match completion records winners as QUALIFIED in tournament_qualifications.
    - Does NOT automatically instantiate downstream matches.
    - Organizer retrieves qualified contenders via GET /qualified-contenders.
    """
    admin = User(
        id="usr_admin_prog",
        clerk_user_id="clerk_admin_prog",
        email="admin@ametheus.gg",
        username="bracket_admin",
        role="SUPER_ADMIN",
    )
    await mock_db.users.insert_one(admin.to_mongo())

    now_iso = datetime.now(timezone.utc).isoformat()
    tourn = Tournament(
        id="tourn_prog_01",
        title="Progression Tournament",
        slug="tourn-prog-01",
        game_id="bgmi",
        starts_at=now_iso,
        roster_lock_time=now_iso,
    )
    await mock_db.tournaments.insert_one(tourn.to_mongo())

    match = Match(
        id="match_prog_01",
        tournament_id="tourn_prog_01",
        stage="Round 1",
        round_name="Round 1",
        round_number=1,
        match_number=1,
        format="Knockout",
        status="LIVE",
        team_a_id="team_winner_01",
        team_b_id="team_loser_01",
        team_a_name="Winner Squad",
        team_b_name="Loser Squad",
    )
    await mock_db.matches.insert_one(match.to_mongo())

    client = make_client(admin)

    # Admin completes match
    resp = await client.post(
        "/api/v1/admin/matches/match_prog_01/complete",
        json={"score_a": 2, "score_b": 0, "winner_team_id": "team_winner_01"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "success"

    # Verify no automatic round 2 matches were created in database
    r2_matches = await mock_db.matches.find({"tournament_id": "tourn_prog_01", "round_number": 2}).to_list(10)
    assert len(r2_matches) == 0, "Downstream match should not be auto-created!"

    # Verify qualification records
    winner_qual = await mock_db.tournament_qualifications.find_one({
        "tournament_id": "tourn_prog_01",
        "participant_id": "team_winner_01",
    })
    assert winner_qual is not None
    assert winner_qual["status"] == "QUALIFIED"

    loser_qual = await mock_db.tournament_qualifications.find_one({
        "tournament_id": "tourn_prog_01",
        "participant_id": "team_loser_01",
    })
    assert loser_qual is not None
    assert loser_qual["status"] == "ELIMINATED"

    # Verify GET /qualified-contenders endpoint
    contenders_resp = await client.get("/api/v1/admin/tournaments/tourn_prog_01/qualified-contenders")
    assert contenders_resp.status_code == 200
    contenders_data = contenders_resp.json()
    assert contenders_data["total_qualified"] == 1
    assert contenders_data["contenders"][0]["participant_id"] == "team_winner_01"


@pytest.mark.asyncio
async def test_battle_royale_squad_alive_status_and_results(mock_db, make_client):
    """
    Test 6: Battle Royale Dedicated Workflow
    - Multi-squad match with operational Alive/Eliminated toggles.
    - Scoring matrix and results finalization.
    """
    admin = User(
        id="usr_admin_br",
        clerk_user_id="clerk_admin_br",
        email="admin@ametheus.gg",
        username="br_admin",
        role="SUPER_ADMIN",
    )
    await mock_db.users.insert_one(admin.to_mongo())

    teams = [
        {"id": "squad_1", "name": "GodLike Esports", "tag": "GOD"},
        {"id": "squad_2", "name": "Team Soul", "tag": "SOUL"},
        {"id": "squad_3", "name": "Global Esports", "tag": "GE"},
    ]

    br_match = Match(
        id="match_br_01",
        tournament_id="tourn_br_01",
        stage="Round 1",
        round_name="Round 1",
        format="Battle Royale",
        status="LIVE",
        team_ids=["squad_1", "squad_2", "squad_3"],
        teams=teams,
        alive_status={"squad_1": True, "squad_2": True, "squad_3": True},
    )
    await mock_db.matches.insert_one(br_match.to_mongo())

    client = make_client(admin)

    # 1. Toggle squad 3 eliminated
    alive_resp = await client.patch(
        "/api/v1/admin/matches/match_br_01/alive-status",
        json={"team_id": "squad_3", "is_alive": False},
    )
    assert alive_resp.status_code == 200, alive_resp.text
    assert alive_resp.json()["is_alive"] is False

    match_doc = await mock_db.matches.find_one({"$or": [{"_id": "match_br_01"}, {"id": "match_br_01"}]})
    assert match_doc["alive_status"]["squad_3"] is False

    # 2. Finalize Battle Royale Results
    scoring_matrix = {"1": 10, "2": 6, "3": 5}
    team_results = [
        {
            "team_id": "squad_1",
            "team_name": "GodLike Esports",
            "placement": 1,
            "kills": 8,
            "placement_points": 10,
            "elimination_points": 8,
            "total_points": 18,
        },
        {
            "team_id": "squad_2",
            "team_name": "Team Soul",
            "placement": 2,
            "kills": 4,
            "placement_points": 6,
            "elimination_points": 4,
            "total_points": 10,
        },
        {
            "team_id": "squad_3",
            "team_name": "Global Esports",
            "placement": 3,
            "kills": 1,
            "placement_points": 5,
            "elimination_points": 1,
            "total_points": 6,
        },
    ]

    br_resp = await client.post(
        "/api/v1/admin/matches/match_br_01/battle-royale-results",
        json={"team_results": team_results, "scoring_matrix": scoring_matrix},
    )
    assert br_resp.status_code == 200, br_resp.text
    br_data = br_resp.json()
    assert br_data["winner_team_id"] == "squad_1"

    # Verify match completed in database
    final_match = await mock_db.matches.find_one({"$or": [{"_id": "match_br_01"}, {"id": "match_br_01"}]})
    assert final_match["status"] == "COMPLETED"
    assert len(final_match["placements"]) == 3


@pytest.mark.asyncio
async def test_participant_ratings_assignment(mock_db, make_client):
    """
    Test 7: Manual Post-Match Player Ratings (1-10 Scale)
    - Authorized organizers assign performance ratings to participants.
    """
    admin = User(
        id="usr_admin_ratings",
        clerk_user_id="clerk_admin_ratings",
        email="admin@ametheus.gg",
        username="ratings_admin",
        role="SUPER_ADMIN",
    )
    await mock_db.users.insert_one(admin.to_mongo())

    match = Match(
        id="match_ratings_01",
        tournament_id="tourn_ratings_01",
        stage="Finals",
        round_name="Grand Finals",
        format="Knockout",
        status="COMPLETED",
        team_a_id="team_a",
        team_b_id="team_b",
    )
    await mock_db.matches.insert_one(match.to_mongo())

    client = make_client(admin)

    ratings_payload = [
        {
            "user_id": "player_001",
            "username": "JONATHAN",
            "team_id": "team_a",
            "rating": 9.5,
            "review": "Exceptional clutch performance in game 3.",
        },
        {
            "user_id": "player_002",
            "username": "Mortal",
            "team_id": "team_b",
            "rating": 8.0,
            "review": "Solid rotations and utility usage.",
        },
    ]

    post_resp = await client.post(
        "/api/v1/admin/matches/match_ratings_01/ratings",
        json={"ratings": ratings_payload},
    )
    assert post_resp.status_code == 200, post_resp.text
    assert post_resp.json()["total_rated"] == 2

    # Fetch ratings
    get_resp = await client.get("/api/v1/admin/matches/match_ratings_01/ratings")
    assert get_resp.status_code == 200, get_resp.text
    ratings_data = get_resp.json()
    assert len(ratings_data["ratings"]) == 2
    assert ratings_data["ratings"][0]["rating"] == 9.5
    assert ratings_data["ratings"][1]["username"] == "Mortal"


@pytest.mark.asyncio
async def test_public_match_detail_masks_credentials_and_checkin_deduplication(mock_db, make_client):
    """
    Test 8: Public Match Detail Access Control & Check-in Deduplication
    - Even when credentials are released, unauthorized public viewers cannot see room_id/password via GET /matches/{id}.
    - Authorized participants DO see credentials via GET /matches/{id}.
    - Repeated check-ins do not generate duplicate check_in records.
    """
    admin = User(
        id="usr_admin_detail",
        clerk_user_id="clerk_admin_detail",
        email="admin@ametheus.gg",
        username="sec_admin",
        role="SUPER_ADMIN",
    )
    participant = User(
        id="usr_participant_01",
        clerk_user_id="clerk_participant_01",
        email="player@ametheus.gg",
        username="team_a_captain",
        role="USER",
    )
    spectator = User(
        id="usr_spectator_01",
        clerk_user_id="clerk_spectator_01",
        email="fan@ametheus.gg",
        username="random_viewer",
        role="USER",
    )
    await mock_db.users.insert_one(admin.to_mongo())
    await mock_db.users.insert_one(participant.to_mongo())
    await mock_db.users.insert_one(spectator.to_mongo())

    team_a = {
        "_id": "team_alpha_sec",
        "id": "team_alpha_sec",
        "name": "Alpha Squad",
        "captain_id": "usr_participant_01",
    }
    await mock_db.teams.insert_one(team_a)

    match = Match(
        id="match_sec_01",
        tournament_id="tourn_sec_01",
        stage="Round 1",
        round_name="Round 1",
        format="Knockout",
        status="SCHEDULED",
        team_a_id="team_alpha_sec",
        team_b_id="team_beta_sec",
        room_id="ROOM-SECRET-123",
        room_password="PASS-SECRET-456",
        lobby_code="LOBBY-123",
        lobby_password="LOBBY-PASS-456",
        credentials_released=True,
    )
    await mock_db.matches.insert_one(match.to_mongo())

    # 1. Spectator views match detail: credentials MUST be masked
    spectator_client = make_client(spectator)
    spec_resp = await spectator_client.get("/api/v1/matches/match_sec_01")
    assert spec_resp.status_code == 200
    spec_data = spec_resp.json()
    assert spec_data.get("room_id") is None
    assert spec_data.get("room_password") is None
    assert spec_data.get("lobby_code") is None
    assert spec_data.get("lobby_password") is None

    # 2. Participant views match detail: credentials ARE accessible
    part_client = make_client(participant)
    part_resp = await part_client.get("/api/v1/matches/match_sec_01")
    assert part_resp.status_code == 200
    part_data = part_resp.json()
    assert part_data.get("room_id") == "ROOM-SECRET-123"
    assert part_data.get("room_password") == "PASS-SECRET-456"

    # 3. Check-in deduplication: call check-in twice
    chk1 = await part_client.post("/api/v1/matches/match_sec_01/check-in")
    assert chk1.status_code == 200
    assert chk1.json()["check_in_status"]["team_a"] is True

    chk2 = await part_client.post("/api/v1/matches/match_sec_01/check-in")
    assert chk2.status_code == 200
    assert chk2.json()["status"] == "already_checked_in"

    # Verify database check_ins has exactly 1 entry
    updated_match = await mock_db.matches.find_one({"$or": [{"_id": "match_sec_01"}, {"id": "match_sec_01"}]})
    assert len(updated_match.get("check_ins", [])) == 1

