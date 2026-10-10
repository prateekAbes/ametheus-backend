import pytest
import pytest_asyncio
import io
import base64
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
async def test_media_upload_and_binary_retrieval(client, mock_db):
    """Test multipart file upload, storage in MongoDB, and binary streaming retrieval."""
    test_image_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    files = {"file": ("test_banner.png", test_image_bytes, "image/png")}

    upload_resp = await client.post("/api/v1/media/upload", files=files)
    assert upload_resp.status_code == 200
    upload_data = upload_resp.json()

    assert upload_data["status"] == "success"
    media_id = upload_data["id"]
    media_url = upload_data["url"]
    assert media_id.startswith("img_")
    assert media_url == f"/api/v1/media/{media_id}"

    # Verify MongoDB persistence
    doc = await mock_db.media.find_one({"id": media_id})
    assert doc is not None
    assert doc["size"] == len(test_image_bytes)
    assert doc["content_type"] == "image/png"

    # Verify binary GET retrieval
    get_resp = await client.get(f"/api/v1/media/{media_id}")
    assert get_resp.status_code == 200
    assert get_resp.headers["content-type"] == "image/png"
    assert get_resp.content == test_image_bytes
    assert "public, max-age=" in get_resp.headers.get("cache-control", "")

    # Verify HEAD request
    head_resp = await client.head(f"/api/v1/media/{media_id}")
    assert head_resp.status_code == 200
    assert head_resp.headers["content-type"] == "image/png"

@pytest.mark.asyncio
async def test_tournament_responsive_banners_persistence_and_retrieval(client, mock_db):
    """Verify that tournament list, detail, and registrations return responsive banner URLs and logo."""
    # Seed a game
    await mock_db.games.insert_one({
        "_id": "game_val",
        "id": "game_val",
        "slug": "valorant",
        "name": "VALORANT",
        "status": "ACTIVE",
        "banner_url": "/api/v1/media/img_val_banner",
        "banner_tablet_url": "/api/v1/media/img_val_tablet",
        "banner_mobile_url": "/api/v1/media/img_val_mobile",
        "logo_url": "/api/v1/media/img_val_logo",
    })

    # Seed a tournament with distinct responsive media assets
    await mock_db.tournaments.insert_one({
        "_id": "tourn_123",
        "id": "tourn_123",
        "slug": "ascent-invitational",
        "title": "Ascent Invitational",
        "game_id": "game_val",
        "status": "REGISTRATION_OPEN",
        "banner_url": "/api/v1/media/img_tourn_desktop",
        "banner_tablet_url": "/api/v1/media/img_tourn_tablet",
        "banner_mobile_url": "/api/v1/media/img_tourn_mobile",
        "logo_url": "/api/v1/media/img_tourn_logo",
        "prize_pool_inr": 50000,
        "entry_fee_inr": 500,
        "max_teams": 16,
    })

    # Test list_tournaments
    list_resp = await client.get("/api/v1/tournaments")
    assert list_resp.status_code == 200
    tournaments = list_resp.json()
    assert len(tournaments) >= 1
    t = next(x for x in tournaments if x["slug"] == "ascent-invitational")
    assert t["banner_url"] == "/api/v1/media/img_tourn_desktop"
    assert t["banner_tablet_url"] == "/api/v1/media/img_tourn_tablet"
    assert t["banner_mobile_url"] == "/api/v1/media/img_tourn_mobile"
    assert t["logo_url"] == "/api/v1/media/img_tourn_logo"

    # Test get_tournament_by_slug
    detail_resp = await client.get("/api/v1/tournaments/ascent-invitational")
    assert detail_resp.status_code == 200
    td = detail_resp.json()
    assert td["banner_url"] == "/api/v1/media/img_tourn_desktop"
    assert td["banner_tablet_url"] == "/api/v1/media/img_tourn_tablet"
    assert td["banner_mobile_url"] == "/api/v1/media/img_tourn_mobile"
    assert td["logo_url"] == "/api/v1/media/img_tourn_logo"

@pytest.mark.asyncio
async def test_game_responsive_banners_serialization(client, mock_db):
    """Verify that games endpoints return responsive banner URLs and logo."""
    await mock_db.games.insert_one({
        "_id": "game_cs2",
        "id": "game_cs2",
        "slug": "counter-strike-2",
        "name": "Counter-Strike 2",
        "category": "fps",
        "platform": "PC",
        "tag": "TACTICAL",
        "status": "ACTIVE",
        "banner_url": "/api/v1/media/img_cs2_desk",
        "banner_tablet_url": "/api/v1/media/img_cs2_tab",
        "banner_mobile_url": "/api/v1/media/img_cs2_mob",
        "logo_url": "/api/v1/media/img_cs2_logo",
    })

    resp = await client.get("/api/v1/games/counter-strike-2")
    assert resp.status_code == 200
    g = resp.json()
    assert g["banner_url"] == "/api/v1/media/img_cs2_desk"
    assert g["banner_tablet_url"] == "/api/v1/media/img_cs2_tab"
    assert g["banner_mobile_url"] == "/api/v1/media/img_cs2_mob"
    assert g["logo_url"] == "/api/v1/media/img_cs2_logo"

@pytest.mark.asyncio
async def test_auth_sync_preserves_custom_uploaded_avatar(client, mock_db, monkeypatch):
    """Verify that sync_clerk_user preserves a custom uploaded media avatar instead of overwriting with Clerk/Google avatar."""
    from app.api.v1.endpoints import auth
    monkeypatch.setattr(auth, "decode_token", lambda tok: {"sub": "user_clerk_999", "email": "player@ametheus.gg"})

    # Pre-existing user with custom avatar in MongoDB
    await mock_db.users.insert_one({
        "_id": "user_doc_1",
        "clerk_user_id": "user_clerk_999",
        "email": "player@ametheus.gg",
        "username": "custom_avatar_player",
        "avatar_url": "/api/v1/media/img_custom_avatar_123",
        "role": "PLAYER",
    })

    # Incoming Clerk sync with generic Clerk/Google avatar
    payload = {
        "clerk_user_id": "user_clerk_999",
        "email": "player@ametheus.gg",
        "username": "custom_avatar_player",
        "display_name": "Player One",
        "avatar_url": "https://img.clerk.com/default-avatar.png",
        "has_chosen_username": True,
    }

    resp = await client.post(
        "/api/v1/auth/sync",
        json=payload,
        headers={"Authorization": "Bearer mock.clerk.token"}
    )
    assert resp.status_code == 200
    user_data = resp.json()

    # Must preserve the uploaded avatar!
    assert user_data["avatar_url"] == "/api/v1/media/img_custom_avatar_123"

    # Also verify MongoDB record directly
    db_user = await mock_db.users.find_one({"clerk_user_id": "user_clerk_999"})
    assert db_user["avatar_url"] == "/api/v1/media/img_custom_avatar_123"
