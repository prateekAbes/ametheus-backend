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
        id="admin_master",
        clerk_user_id="user_admin_master",
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
async def test_placements_listing_and_defaults(client, mock_db):
    """Verify that GET /api/v1/placements lists canonical defaults and seeds them."""
    resp = await client.get("/api/v1/placements")
    assert resp.status_code == 200
    placements = resp.json()
    assert len(placements) >= 6

    keys = {p["placement_key"] for p in placements}
    assert "homepage_hero_slot_1" in keys
    assert "homepage_hero_slot_2" in keys
    assert "homepage_hero_slot_3" in keys
    assert "homepage_hero_slot_4" in keys
    assert "homepage_featured_tournament" in keys
    assert "homepage_game_showcase" in keys

    slot1 = next(p for p in placements if p["placement_key"] == "homepage_hero_slot_1")
    assert slot1["target_aspect_ratio"] == "3:4"
    assert slot1["recommended_resolution"] == "720x960"
    assert slot1["min_width"] == 720
    assert slot1["min_height"] == 960

@pytest.mark.asyncio
async def test_placement_update_focal_point_and_zoom(client, mock_db):
    """Verify updating focal position, zoom, and fit mode for an independent placement."""
    update_payload = {
        "asset_url": "/api/v1/media/img_hero_custom_1",
        "focal_x": 35.5,
        "focal_y": 70.0,
        "zoom": 1.5,
        "fit_mode": "contain",
        "target_id": "valorant",
    }
    put_resp = await client.put("/api/v1/placements/homepage_hero_slot_1", json=update_payload)
    assert put_resp.status_code == 200
    updated = put_resp.json()
    assert updated["asset_url"] == "/api/v1/media/img_hero_custom_1"
    assert updated["focal_x"] == 35.5
    assert updated["focal_y"] == 70.0
    assert updated["zoom"] == 1.5
    assert updated["fit_mode"] == "contain"
    assert updated["target_id"] == "valorant"

    # Verify retrieval
    get_resp = await client.get("/api/v1/placements/homepage_hero_slot_1")
    assert get_resp.status_code == 200
    got = get_resp.json()
    assert got["focal_x"] == 35.5
    assert got["zoom"] == 1.5

@pytest.mark.asyncio
async def test_clear_placement_asset(client, mock_db):
    """Verify clearing asset URL from placement retains the placement record and settings."""
    await client.put("/api/v1/placements/homepage_hero_slot_2", json={"asset_url": "/api/v1/media/img_temp"})
    del_resp = await client.delete("/api/v1/placements/homepage_hero_slot_2/asset")
    assert del_resp.status_code == 200
    cleared = del_resp.json()
    assert cleared["asset_url"] is None
    assert cleared["placement_key"] == "homepage_hero_slot_2"

@pytest.mark.asyncio
async def test_media_library_listing_and_search(client, mock_db):
    """Verify GET /api/v1/media lists media assets with metadata and supports filename search."""
    # Seed mock media docs
    await mock_db.media.insert_many([
        {"id": "img_bgmi_1", "_id": "img_bgmi_1", "filename": "bgmi_poster.png", "content_type": "image/png", "size": 1024, "data": "b64string", "created_at": "2026-10-16T10:00:00Z"},
        {"id": "img_val_1", "_id": "img_val_1", "filename": "valorant_hero.jpg", "content_type": "image/jpeg", "size": 2048, "data": "b64string2", "created_at": "2026-10-16T11:00:00Z"},
    ])

    list_resp = await client.get("/api/v1/media")
    assert list_resp.status_code == 200
    data = list_resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    # Verify large base64 payload is NOT returned in list view
    assert "data" not in data["items"][0]
    assert data["items"][0]["url"].startswith("/api/v1/media/")

    # Search filter
    search_resp = await client.get("/api/v1/media?search=valorant")
    assert search_resp.status_code == 200
    search_data = search_resp.json()
    assert search_data["total"] == 1
    assert search_data["items"][0]["filename"] == "valorant_hero.jpg"

@pytest.mark.asyncio
async def test_game_media_config_persistence_and_sync(client, mock_db):
    """Verify that Game.media_config persists per-placement crop and syncs with poster_url."""
    # Seed game
    await mock_db.games.insert_one({
        "_id": "game_val",
        "id": "game_val",
        "slug": "valorant",
        "name": "VALORANT",
        "status": "ACTIVE",
        "category": "tactical-fps",
        "platform": "PC",
        "tag": "VAL",
        "banner_url": "/api/v1/media/img_val_banner",
        "logo_url": "/api/v1/media/img_val_logo",
    })

    # Update with media_config
    mc_payload = {
        "media_config": {
            "catalog_card": {
                "asset_url": "/api/v1/media/img_val_poster_v2",
                "focal_x": 50,
                "focal_y": 25,
                "zoom": 1.2,
                "fit_mode": "cover",
            },
            "showcase_card": {
                "asset_url": "/api/v1/media/img_val_showcase",
                "focal_x": 50,
                "focal_y": 35,
                "zoom": 1.0,
                "fit_mode": "cover",
            }
        }
    }
    update_resp = await client.put("/api/v1/games/valorant", json=mc_payload)
    assert update_resp.status_code == 200
    updated_game = update_resp.json()
    assert updated_game["poster_url"] == "/api/v1/media/img_val_poster_v2"
    assert updated_game["media_config"]["catalog_card"]["zoom"] == 1.2
    assert updated_game["media_config"]["showcase_card"]["focal_y"] == 35

@pytest.mark.asyncio
async def test_tournament_media_config_persistence_and_sync(client, mock_db):
    """Verify Tournament.media_config persists independent crops and syncs with banner_url."""
    # Seed game and tournament
    await mock_db.games.insert_one({
        "_id": "game_val",
        "id": "game_val",
        "slug": "valorant",
        "name": "VALORANT",
        "status": "ACTIVE",
    })
    await mock_db.tournaments.insert_one({
        "_id": "tourn_100",
        "id": "tourn_100",
        "slug": "ascent-cup",
        "title": "Ascent Cup",
        "game_id": "game_val",
        "status": "REGISTRATION_OPEN",
        "banner_url": "/api/v1/media/img_old_banner",
    })

    update_payload = {
        "media_config": {
            "card": {
                "asset_url": "/api/v1/media/img_card_banner",
                "focal_x": 50,
                "focal_y": 50,
                "zoom": 1.0,
                "fit_mode": "cover",
            },
            "hero": {
                "asset_url": "/api/v1/media/img_hero_panoramic",
                "focal_x": 75,
                "focal_y": 50,
                "zoom": 1.3,
                "fit_mode": "cover",
            }
        }
    }
    put_resp = await client.put("/api/v1/organizer/tournaments/tourn_100", json=update_payload)
    assert put_resp.status_code == 200
    updated_t = put_resp.json()
    assert updated_t["banner_url"] == "/api/v1/media/img_card_banner"
    assert updated_t["hero_banner_url"] == "/api/v1/media/img_hero_panoramic"
    assert updated_t["media_config"]["hero"]["zoom"] == 1.3
