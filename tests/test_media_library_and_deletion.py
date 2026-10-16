import pytest
import pytest_asyncio
import io
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
async def test_media_upload_references_and_deletion(client, mock_db):
    # 1. Upload a dummy media file (1920x1080 PNG header)
    file_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x07\x80\x00\x00\x048\x08\x02\x00\x00\x00"
    files = {"file": ("hero_bg.png", io.BytesIO(file_content), "image/png")}
    up_res = await client.post("/api/v1/media/upload", files=files)
    assert up_res.status_code == 200
    up_data = up_res.json()
    media_id = up_data["id"]
    media_url = up_data["url"]
    assert up_data["width"] == 1920
    assert up_data["height"] == 1080

    # 2. Verify media appears in list
    list_res = await client.get("/api/v1/media")
    assert list_res.status_code == 200
    items = list_res.json()["items"]
    matching = [item for item in items if item["id"] == media_id]
    assert len(matching) == 1
    assert matching[0]["width"] == 1920
    assert matching[0]["height"] == 1080
    assert matching[0]["in_use"] is False

    # 3. Create a game and assign this asset to its artwork
    game_res = await client.post(
        "/api/v1/games",
        json={
            "name": "Counter-Strike 2",
            "slug": "cs2",
            "tag": "CS2",
            "platform": "PC",
            "category": "TACTICAL_FPS",
            "poster_url": media_url,
            "media_config": {
                "homepage_hero_floating": {
                    "asset_url": media_url,
                    "focal_x": 50,
                    "focal_y": 50,
                    "zoom": 1.0,
                }
            },
        },
    )
    assert game_res.status_code == 201
    game_data = game_res.json()

    # 4. Check references endpoint
    ref_res = await client.get(f"/api/v1/media/{media_id}/references")
    assert ref_res.status_code == 200
    ref_data = ref_res.json()
    assert ref_data["in_use"] is True
    assert len(ref_data["references"]) >= 1
    assert any(r["entity_name"] == "Counter-Strike 2" for r in ref_data["references"])

    # 5. Attempt deletion without force -> should be rejected with 409
    del_conflict = await client.delete(f"/api/v1/media/{media_id}")
    assert del_conflict.status_code == 409
    assert "currently in use" in del_conflict.json()["detail"]["message"]

    # 6. Delete with force=True -> should clear references and delete file permanently
    del_ok = await client.delete(f"/api/v1/media/{media_id}?force=true")
    assert del_ok.status_code == 200
    del_data = del_ok.json()
    assert del_data["status"] == "success"
    assert del_data["cleared_references_count"] >= 1

    # 7. Verify media file no longer exists
    get_res = await client.get(f"/api/v1/media/{media_id}")
    assert get_res.status_code == 404

    # 8. Verify game reference was safely cleared
    check_game = await client.get(f"/api/v1/games/{game_data['id']}")
    assert check_game.status_code == 200
    updated_game = check_game.json()
    assert updated_game["poster_url"] == "" or updated_game["poster_url"] is None
