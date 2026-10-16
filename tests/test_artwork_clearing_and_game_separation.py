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
        username="superadmin",
        role="SUPER_ADMIN",
    )
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_super_admin] = lambda: admin_user
    app.dependency_overrides[require_organizer] = lambda: admin_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_game_creation_without_artwork(client, mock_db):
    """Ensure game can be created without requiring or auto-assigning any artwork or fallbacks."""
    payload = {
        "name": "Valorant Champions",
        "slug": "val-champs",
        "tag": "VAL",
        "description": "Tactical 5v5 shooter",
        "category": "Tactical FPS",
        "platform": "PC",
        "status": "ACTIVE",
    }

    create_resp = await client.post("/api/v1/games", json=payload)
    assert create_resp.status_code == 200 or create_resp.status_code == 201
    game_data = create_resp.json()

    assert game_data["slug"] == "val-champs"
    # Ensure no automatic fallback images (e.g. Unsplash) were injected
    assert game_data.get("banner_url") in ("", None)
    assert game_data.get("poster_url") in ("", None)
    assert game_data.get("logo_url") in ("", None)

    # Verify GET single game
    get_resp = await client.get("/api/v1/games/val-champs")
    assert get_resp.status_code == 200
    retrieved = get_resp.json()
    assert retrieved.get("banner_url") in ("", None)
    assert retrieved.get("poster_url") in ("", None)
    assert retrieved.get("logo_url") in ("", None)

@pytest.mark.asyncio
async def test_clearing_all_game_artwork_persists_empty_state(client, mock_db):
    """Ensure administrators can clear all images for a game and successfully persist the empty state."""
    # First insert a game with existing artwork
    await mock_db.games.insert_one({
        "id": "game_test_clear",
        "name": "Apex Legends",
        "slug": "apex-legends",
        "banner_url": "/api/v1/media/img_apex_banner",
        "poster_url": "/api/v1/media/img_apex_poster",
        "logo_url": "/api/v1/media/img_apex_logo",
        "media_config": {
            "catalog_card": {"asset_url": "/api/v1/media/img_apex_poster", "fit_mode": "cover"},
            "banner": {"asset_url": "/api/v1/media/img_apex_banner", "fit_mode": "ambient"},
            "logo": {"asset_url": "/api/v1/media/img_apex_logo"},
        },
        "status": "ACTIVE",
    })

    # Admin clears all slots and artwork fields to empty strings
    update_payload = {
        "banner_url": "",
        "poster_url": "",
        "logo_url": "",
        "media_config": {
            "catalog_card": {"asset_url": ""},
            "banner": {"asset_url": ""},
            "logo": {"asset_url": ""},
        },
    }

    put_resp = await client.put("/api/v1/games/game_test_clear", json=update_payload)
    assert put_resp.status_code == 200
    updated_game = put_resp.json()

    # Verify API response has empty strings (not reverted to fallback or old image)
    assert updated_game["banner_url"] == ""
    assert updated_game["poster_url"] == ""
    assert updated_game["logo_url"] == ""
    assert updated_game["media_config"]["catalog_card"]["asset_url"] == ""

    # Verify MongoDB persistence
    db_game = await mock_db.games.find_one({"id": "game_test_clear"})
    assert db_game["banner_url"] == ""
    assert db_game["poster_url"] == ""
    assert db_game["logo_url"] == ""
    assert db_game["media_config"]["catalog_card"]["asset_url"] == ""

    # Verify GET endpoint does not inject fallback images on retrieval
    get_resp = await client.get("/api/v1/games/apex-legends")
    assert get_resp.status_code == 200
    retrieved = get_resp.json()
    assert retrieved["banner_url"] == ""
    assert retrieved["poster_url"] == ""
    assert retrieved["logo_url"] == ""

@pytest.mark.asyncio
async def test_partial_game_artwork_update(client, mock_db):
    """Ensure updating one artwork slot does not overwrite other empty slots or re-inject defaults."""
    await mock_db.games.insert_one({
        "id": "game_partial",
        "name": "Tekken 8",
        "slug": "tekken-8",
        "banner_url": "",
        "poster_url": "",
        "logo_url": "",
        "media_config": {},
        "status": "ACTIVE",
    })

    # Update only the banner
    update_payload = {
        "banner_url": "/api/v1/media/img_tekken_banner",
        "media_config": {
            "banner": {"asset_url": "/api/v1/media/img_tekken_banner", "fit_mode": "ambient"},
        },
    }

    put_resp = await client.put("/api/v1/games/game_partial", json=update_payload)
    assert put_resp.status_code == 200
    updated = put_resp.json()

    assert updated["banner_url"] == "/api/v1/media/img_tekken_banner"
    assert updated["poster_url"] in ("", None)
    assert updated["logo_url"] in ("", None)

@pytest.mark.asyncio
async def test_clearing_tournament_artwork(client, mock_db):
    """Ensure tournament artwork can be cleared to empty strings without fallback injection."""
    await mock_db.tournaments.insert_one({
        "id": "tour_test_clear",
        "title": "Ametheus Winter Championship",
        "slug": "winter-champ",
        "banner_url": "/api/v1/media/img_tour_card",
        "hero_banner_url": "/api/v1/media/img_tour_hero",
        "logo_url": "/api/v1/media/img_tour_logo",
        "media_config": {
            "card": {"asset_url": "/api/v1/media/img_tour_card"},
            "hero": {"asset_url": "/api/v1/media/img_tour_hero"},
        },
        "status": "REGISTRATION_OPEN",
        "organizer_id": "admin_123",
    })

    # Clear tournament media
    clear_payload = {
        "banner_url": "",
        "hero_banner_url": "",
        "logo_url": "",
        "media_config": {
            "card": {"asset_url": ""},
            "hero": {"asset_url": ""},
            "logo": {"asset_url": ""},
        },
    }

    put_resp = await client.put("/api/v1/organizer/tournaments/tour_test_clear", json=clear_payload)
    assert put_resp.status_code == 200

    # Verify MongoDB persistence
    db_tour = await mock_db.tournaments.find_one({"id": "tour_test_clear"})
    assert db_tour["banner_url"] == ""
    assert db_tour.get("hero_banner_url") in ("", None)
    assert db_tour["media_config"]["card"]["asset_url"] == ""
