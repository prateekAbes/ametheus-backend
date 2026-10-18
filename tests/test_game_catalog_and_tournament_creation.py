import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.database import get_db
from app.models.user import User
from app.api.deps import get_current_user, require_super_admin, require_organizer

@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
    return db

@pytest_asyncio.fixture
async def client(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db
    
    admin_user = User(
        id="admin_123",
        clerk_user_id="user_admin_123",
        email="admin@ametheus.gg",
        username="admin_prime",
        role="SUPER_ADMIN",
    )
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_super_admin] = lambda: admin_user
    app.dependency_overrides[require_organizer] = lambda: admin_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


# ==============================================================================
# PART 1: GAME CATALOG & SUPPORTED GAME MODES
# ==============================================================================

@pytest.mark.asyncio
async def test_game_catalog_all_7_mode_combinations(client, mock_db):
    """Test all 7 non-empty combinations of Solo, Duo, Squad modes for games."""
    combinations = [
        ["SOLO"],
        ["DUO"],
        ["SQUAD"],
        ["SOLO", "DUO"],
        ["SOLO", "SQUAD"],
        ["DUO", "SQUAD"],
        ["SOLO", "DUO", "SQUAD"],
    ]

    for idx, modes in enumerate(combinations):
        slug = f"test-game-combo-{idx}"
        payload = {
            "name": f"Test Game Combo {idx}",
            "slug": slug,
            "category": "Battle Royale",
            "genre": "Action / Shooter",
            "platform": "PC",
            "tag": "FPS",
            "supported_game_modes": modes,
            "description": f"Testing game combination {modes}",
        }
        res = await client.post("/api/v1/games", json=payload)
        assert res.status_code == 201, f"Failed for modes {modes}: {res.text}"
        data = res.json()
        assert data["supported_game_modes"] == modes
        assert data["genre"] == "Action / Shooter"

        # Check DB persistence
        saved = await mock_db.games.find_one({"slug": slug})
        assert saved is not None
        assert saved["supported_game_modes"] == modes
        assert (saved.get("genre") or saved.get("genre_name")) == "Action / Shooter"


@pytest.mark.asyncio
async def test_game_catalog_rejects_empty_or_invalid_modes(client, mock_db):
    """Test validation rejecting empty or unsupported mode names."""
    # Empty modes
    payload_empty = {
        "name": "Empty Modes Game",
        "slug": "empty-game",
        "category": "MOBA",
        "genre": "Strategy",
        "platform": "PC",
        "tag": "MOBA",
        "supported_game_modes": [],
        "description": "Empty modes",
    }
    res_empty = await client.post("/api/v1/games", json=payload_empty)
    assert res_empty.status_code == 422
    assert "Supported game modes cannot be empty" in res_empty.text

    # Invalid mode name
    payload_invalid = {
        "name": "Invalid Mode Game",
        "slug": "invalid-mode-game",
        "category": "MOBA",
        "genre": "Strategy",
        "platform": "PC",
        "tag": "MOBA",
        "supported_game_modes": ["TRIO", "SOLO"],
        "description": "Invalid mode TRIO",
    }
    res_invalid = await client.post("/api/v1/games", json=payload_invalid)
    assert res_invalid.status_code == 422
    assert "Invalid game mode" in res_invalid.text


@pytest.mark.asyncio
async def test_game_catalog_update_supported_modes(client, mock_db):
    """Test updating supported modes on an existing game."""
    await mock_db.games.insert_one({
        "_id": "custom-game",
        "id": "custom-game",
        "name": "Custom Game",
        "slug": "custom-game",
        "genre": "Tactical Shooter",
        "category": "FPS",
        "platform": "PC",
        "tag": "FPS",
        "supported_game_modes": ["SQUAD"],
    })

    # Update to Solo and Duo
    res = await client.put("/api/v1/games/custom-game", json={
        "supported_game_modes": ["SOLO", "DUO"],
    })
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["supported_game_modes"] == ["SOLO", "DUO"]
    assert data["genre"] == "Tactical Shooter"


# ==============================================================================
# PART 2 & 3: TOURNAMENT CREATION, PARTICIPATION MODES, DYNAMIC CAPACITY & NO ROSTER LOCK
# ==============================================================================

@pytest.mark.asyncio
async def test_tournament_creation_solo_mode(client, mock_db):
    """Test creating a Solo tournament with dynamic capacity and no roster lock."""
    await mock_db.games.insert_one({
        "_id": "chess",
        "id": "chess",
        "name": "Chess",
        "slug": "chess",
        "genre": "Board Game",
        "supported_game_modes": ["SOLO"],
    })

    payload = {
        "game_id": "chess",
        "title": "Ametheus Blitz Chess Cup",
        "participation_mode": "SOLO",
        "max_teams": 64,  # Represents Max Players
        "format": "Single Elimination",
        "match_format": "BO1",
        "prize_pool_inr": 20000,
        "entry_fee_inr": 100,
        "start_date": "2026-11-20",
        "start_time": "18:00",
        "description": "Solo competitive chess",
    }
    res = await client.post("/api/v1/organizer/tournaments", json=payload)
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["title"] == "Ametheus Blitz Chess Cup"
    assert data["participation_mode"] == "SOLO"
    assert data["is_solo"] is True
    assert data["registration_type"] == "SOLO"
    assert data["capacity_label"] == "Max Players"
    assert data["max_teams"] == 64
    assert data["players_per_team"] == 1
    assert data["min_team_size"] == 1
    assert data["max_team_size"] == 1
    assert data["roster_lock_time"] is None
    assert "2026-11-20T18:00:00" in data["starts_at"]


@pytest.mark.asyncio
async def test_tournament_creation_duo_mode(client, mock_db):
    """Test creating a Duo tournament with dynamic capacity."""
    await mock_db.games.insert_one({
        "_id": "bgmi",
        "id": "bgmi",
        "name": "BGMI",
        "slug": "bgmi",
        "genre": "Battle Royale",
        "supported_game_modes": ["SOLO", "DUO", "SQUAD"],
    })

    payload = {
        "game_id": "bgmi",
        "title": "BGMI Duo Battlegrounds",
        "participation_mode": "DUO",
        "max_teams": 25,  # Represents Max Teams (Duos)
        "format": "Battle Royale",
        "prize_pool_inr": 50000,
        "entry_fee_inr": 200,
        "starts_at": "2026-11-25T14:00:00Z",
        "description": "Duo battle royale",
    }
    res = await client.post("/api/v1/organizer/tournaments", json=payload)
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["participation_mode"] == "DUO"
    assert data["is_solo"] is False
    assert data["registration_type"] == "TEAM"
    assert data["capacity_label"] == "Max Teams"
    assert data["max_teams"] == 25
    assert data["players_per_team"] == 2
    assert data["min_team_size"] == 2
    assert data["max_team_size"] == 2
    assert data["roster_lock_time"] is None


@pytest.mark.asyncio
async def test_tournament_creation_squad_mode(client, mock_db):
    """Test creating a Squad tournament with dynamic capacity."""
    await mock_db.games.insert_one({
        "_id": "valorant",
        "id": "valorant",
        "name": "Valorant",
        "slug": "valorant",
        "genre": "Shooter",
        "supported_game_modes": ["SQUAD"],
    })

    payload = {
        "game_id": "valorant",
        "title": "Valorant Champions Squad 2026",
        "participation_mode": "SQUAD",
        "max_teams": 16,  # Represents Max Teams (Squads)
        "format": "Knockout",
        "prize_pool_inr": 100000,
        "entry_fee_inr": 500,
        "starts_at": "2026-12-01T15:00:00Z",
        "description": "Squad competitive shooter",
    }
    res = await client.post("/api/v1/organizer/tournaments", json=payload)
    assert res.status_code == 201, res.text
    data = res.json()

    assert data["participation_mode"] == "SQUAD"
    assert data["is_solo"] is False
    assert data["registration_type"] == "TEAM"
    assert data["capacity_label"] == "Max Teams"
    assert data["max_teams"] == 16
    assert data["players_per_team"] == 4
    assert data["min_team_size"] == 4
    assert data["max_team_size"] == 4
    assert data["roster_lock_time"] is None


@pytest.mark.asyncio
async def test_tournament_creation_unsupported_mode_rejected_with_available_list(client, mock_db):
    """Test that attempting to create a tournament with an unsupported mode is rejected."""
    await mock_db.games.insert_one({
        "_id": "val-squad-only",
        "id": "val-squad-only",
        "name": "Valorant Squad Only",
        "slug": "val-squad-only",
        "genre": "Shooter",
        "supported_game_modes": ["SQUAD"],
    })

    payload = {
        "game_id": "val-squad-only",
        "title": "Illegal Solo Valorant",
        "participation_mode": "SOLO",
        "starts_at": "2026-12-01T15:00:00Z",
        "description": "Should fail",
    }
    res = await client.post("/api/v1/organizer/tournaments", json=payload)
    assert res.status_code == 422
    err = res.json()["detail"]
    assert "Participation mode 'Solo' is not supported by Valorant Squad Only" in err
    assert "Available mode(s): Squad" in err


@pytest.mark.asyncio
async def test_dynamic_capacity_validation_labels(client, mock_db):
    """Test that capacity validation mentions Max Players for Solo and Max Teams for Duo/Squad."""
    await mock_db.games.insert_one({
        "_id": "multi-game",
        "id": "multi-game",
        "name": "Multi Game",
        "slug": "multi-game",
        "genre": "Action",
        "supported_game_modes": ["SOLO", "SQUAD"],
    })

    # Solo with capacity 1
    res_solo = await client.post("/api/v1/organizer/tournaments", json={
        "game_id": "multi-game",
        "title": "Low Capacity Solo",
        "participation_mode": "SOLO",
        "max_teams": 1,
        "starts_at": "2026-12-01T15:00:00Z",
        "description": "Should fail capacity",
    })
    assert res_solo.status_code == 422
    assert "Max Players must be at least 2" in res_solo.json()["detail"]

    # Squad with capacity 1
    res_squad = await client.post("/api/v1/organizer/tournaments", json={
        "game_id": "multi-game",
        "title": "Low Capacity Squad",
        "participation_mode": "SQUAD",
        "max_teams": 1,
        "starts_at": "2026-12-01T15:00:00Z",
        "description": "Should fail capacity",
    })
    assert res_squad.status_code == 422
    assert "Max Teams must be at least 2" in res_squad.json()["detail"]


@pytest.mark.asyncio
async def test_tournament_detail_api_returns_mode_and_no_roster_lock(client, mock_db):
    """Test public tournament detail and listing endpoints return participation_mode, capacity_label, and roster_lock_time=None."""
    await mock_db.games.insert_one({
        "_id": "bgmi-pub",
        "id": "bgmi-pub",
        "name": "BGMI Public",
        "slug": "bgmi-pub",
        "genre": "Battle Royale",
        "supported_game_modes": ["DUO"],
    })
    await mock_db.tournaments.insert_one({
        "_id": "t-bgmi-duo",
        "id": "t-bgmi-duo",
        "slug": "bgmi-duo-invitational",
        "title": "BGMI Duo Invitational",
        "game_id": "bgmi-pub",
        "participation_mode": "DUO",
        "max_teams": 20,
        "status": "REGISTRATION_OPEN",
        "starts_at": "2026-12-10T12:00:00Z",
        "description": "Public tournament test",
        "is_dummy": False,
    })

    # Public detail
    res = await client.get("/api/v1/tournaments/bgmi-duo-invitational")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["participation_mode"] == "DUO"
    assert data["capacity_label"] == "Max Teams"
    assert data["roster_lock_time"] is None

    # Public list
    res_list = await client.get("/api/v1/tournaments")
    assert res_list.status_code == 200, res_list.text
    t_items = [t for t in res_list.json() if t["slug"] == "bgmi-duo-invitational"]
    assert len(t_items) == 1
    assert t_items[0]["participation_mode"] == "DUO"
    assert t_items[0]["capacity_label"] == "Max Teams"
    assert t_items[0]["roster_lock_time"] is None
