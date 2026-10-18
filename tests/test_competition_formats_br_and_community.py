import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from mongomock_motor import AsyncMongoMockClient
from datetime import datetime, timezone

from app.main import app
from app.core.database import get_db
from app.api.deps import get_current_user, require_organizer, require_super_admin
from app.models.user import User
from app.models.tournament import Tournament
from app.models.match import Match


@pytest.fixture
def mock_db():
    client = AsyncMongoMockClient()
    return client["test_competition_formats_db"]


@pytest_asyncio.fixture
async def client_with_admin(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db

    admin_user = User(
        id="admin_test_01",
        clerk_user_id="clerk_admin_01",
        email="admin@ametheus.gg",
        username="super_arbiter",
        display_name="Super Arbiter",
        role="SUPER_ADMIN",
    )
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_organizer] = lambda: admin_user
    app.dependency_overrides[require_super_admin] = lambda: admin_user

    await mock_db.users.insert_one(admin_user.to_mongo())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, admin_user, mock_db

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client_with_auth(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db

    player_user = User(
        id="player_test_01",
        clerk_user_id="clerk_player_01",
        email="player@ametheus.gg",
        username="pro_gamer_99",
        display_name="Pro Gamer",
        role="PLAYER",
    )
    app.dependency_overrides[get_current_user] = lambda: player_user

    await mock_db.users.insert_one(player_user.to_mongo())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, player_user, mock_db

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_competition_format_and_series_format_normalization(client_with_admin):
    """
    Verifies that legacy or unsupported formats ('Single Elimination', 'Double Elimination',
    'Drop-in', 'BO5', 'BO2') are normalized strictly to Knockout, Round Robin, or Battle Royale,
    and series format to BO1 or BO3.
    """
    ac, admin_user, mock_db = client_with_admin

    # Seed game discipline
    await mock_db.games.insert_one({
        "id": "valorant",
        "slug": "valorant",
        "name": "Valorant",
        "min_team_size": 5,
        "max_team_size": 5,
    })

    # Test creation with legacy formats
    payload = {
        "title": "Legacy Normalization Cup",
        "game_id": "valorant",
        "description": "Standard sanctioned competitive esports championship tournament.",
        "format": "Single Elimination Bracket",
        "tournament_type": "Double Elimination",
        "match_format": "Best of 5",
        "series_format": "BO5",
        "whatsapp_group_url": "https://chat.whatsapp.com/test12345",
        "discord_url": "https://discord.gg/esports123",
        "starts_at": datetime.now(timezone.utc).isoformat(),
        "roster_lock_time": datetime.now(timezone.utc).isoformat(),
        "prize_pool_inr": 20000,
        "entry_fee_inr": 0,
        "max_teams": 16,
        "min_team_size": 5,
        "max_team_size": 5,
    }

    res = await ac.post("/api/v1/organizer/tournaments", json=payload)
    assert res.status_code in [200, 201], res.text
    data = res.json()

    assert data["format"] == "Knockout"
    assert data["tournament_type"] == "Knockout"
    assert data["series_format"] in ["BO1", "BO3"]
    assert data["whatsapp_group_url"] == "https://chat.whatsapp.com/test12345"
    assert data["discord_url"] == "https://discord.gg/esports123"


@pytest.mark.asyncio
async def test_battle_royale_lobby_no_forced_team_a_b(client_with_admin):
    """
    Verifies that generating Battle Royale matches does not fabricate a fake Team A vs Team B
    versus layout and populates multi-participant teams directly.
    """
    ac, admin_user, mock_db = client_with_admin

    now_iso = datetime.now(timezone.utc).isoformat()
    tour_id = "br_tourn_test_99"
    tour = Tournament(
        id=tour_id,
        slug="br-tourn-test-99",
        title="Apex Masters BR",
        game_id="apex-legends",
        format="Battle Royale",
        tournament_type="Battle Royale",
        status="REGISTRATION_CLOSED",
        registered_teams=12,
        max_teams=20,
        starts_at=now_iso,
        roster_lock_time=now_iso,
    )
    await mock_db.tournaments.insert_one(tour.to_mongo())

    # Insert mock registrations
    for i in range(1, 13):
        await mock_db.tournament_registrations.insert_one({
            "id": f"reg_{i}",
            "tournament_id": tour_id,
            "team_id": f"team_{i}",
            "team_name": f"Squad Alpha {i}",
            "captain_id": f"user_{i}",
            "status": "CONFIRMED",
            "payment_status": "PAID",
        })

    # Generate battle royale matches via admin endpoint
    res = await ac.post(f"/api/v1/admin/tournaments/{tour_id}/generate-battle-royale-matches?round_number=1&lobby_size=20")
    assert res.status_code == 200, res.text

    # Verify generated match
    m_doc = await mock_db.matches.find_one({"tournament_id": tour_id})
    assert m_doc is not None
    assert m_doc.get("team_a_id") is None
    assert m_doc.get("team_b_id") is None
    assert m_doc.get("team_a_name") is None
    assert m_doc.get("team_b_name") is None
    assert len(m_doc.get("team_ids", [])) == 12
    assert len(m_doc.get("teams", [])) == 12


@pytest.mark.asyncio
async def test_community_links_confidentiality_and_gating(client_with_auth):
    """
    Verifies that whatsapp_group_url and discord_url are NOT leaked in public tournament listings,
    and are accessible via /tournaments/{id}/community for confirmed participants.
    """
    ac_user, user, mock_db = client_with_auth

    now_iso = datetime.now(timezone.utc).isoformat()
    tour_id = "comm_tourn_test_101"
    tour = Tournament(
        id=tour_id,
        slug="comm-tourn-test-101",
        title="Confidential Community Cup",
        game_id="valorant",
        format="Knockout",
        status="REGISTRATION_OPEN",
        whatsapp_group_url="https://chat.whatsapp.com/secret_wa",
        discord_url="https://discord.gg/secret_dc",
        starts_at=now_iso,
        roster_lock_time=now_iso,
    )
    await mock_db.tournaments.insert_one(tour.to_mongo())

    # 1. Public endpoint should NOT include private community links
    res_pub = await ac_user.get(f"/api/v1/tournaments/{tour_id}")
    assert res_pub.status_code == 200
    pub_data = res_pub.json()
    assert pub_data.get("whatsapp_group_url") is None
    assert pub_data.get("discord_url") is None

    # 2. Before registration, user requesting /community gets 403 Forbidden
    res_comm_unauth = await ac_user.get(f"/api/v1/tournaments/{tour_id}/community")
    assert res_comm_unauth.status_code == 403

    # 3. Register user and confirm, and set tournament status to closed
    await mock_db.tournament_registrations.insert_one({
        "id": "reg_user_101",
        "tournament_id": tour_id,
        "player_id": user.id,
        "team_name": "User Squad",
        "status": "CONFIRMED",
        "payment_status": "PAID",
    })
    await mock_db.tournaments.update_one({"id": tour_id}, {"$set": {"status": "REGISTRATION_CLOSED"}})

    # 4. Now user has access to both WhatsApp and Discord links
    res_comm_auth = await ac_user.get(f"/api/v1/tournaments/{tour_id}/community")
    assert res_comm_auth.status_code == 200
    comm_auth_data = res_comm_auth.json()
    assert comm_auth_data.get("whatsapp_group_url") == "https://chat.whatsapp.com/secret_wa"
    assert comm_auth_data.get("discord_url") == "https://discord.gg/secret_dc"


@pytest.mark.asyncio
async def test_solo_player_captain_check_in(client_with_auth):
    """
    Verifies that a solo player in competing_teams or tournament_registrations can check in
    without requiring a record in the teams collection.
    """
    ac_user, user, mock_db = client_with_auth

    match_id = "solo_match_checkin_01"
    tour_id = "solo_tourn_01"

    # Insert match in CHECK_IN_OPEN status with user as a contender
    match = Match(
        id=match_id,
        tournament_id=tour_id,
        game_slug="valorant",
        round_name="Round 1",
        status="CHECK_IN_OPEN",
        competing_teams=[user.id, "other_player_02"],
        teams=[{"id": user.id, "name": user.username}],
        team_ids=[user.id, "other_player_02"],
    )
    await mock_db.matches.insert_one(match.to_mongo())

    # Insert registration for solo user
    await mock_db.tournament_registrations.insert_one({
        "id": "reg_solo_01",
        "tournament_id": tour_id,
        "player_id": user.id,
        "status": "CONFIRMED",
        "payment_status": "PAID",
    })

    # Execute check-in
    res = await ac_user.post(f"/api/v1/matches/{match_id}/check-in")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "success"
    assert "Participation confirmed" in data["message"]
    assert data["check_in_status"][user.id] is True
