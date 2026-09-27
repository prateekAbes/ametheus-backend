import io
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.core.config import settings

@pytest.mark.asyncio
async def test_media_upload_base64_and_retrieve():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Upload a 1x1 transparent PNG as base64
        sample_b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        res = await ac.post(
            f"{settings.API_V1_STR}/media/upload-base64",
            json={"data_url": sample_b64, "filename": "pixel.png"}
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        media_id = data["id"]
        assert media_id.startswith("img_")
        assert data["content_type"] == "image/png"

        # Retrieve binary image
        get_res = await ac.get(f"{settings.API_V1_STR}/media/{media_id}")
        assert get_res.status_code == 200
        assert get_res.headers["content-type"] == "image/png"
        assert len(get_res.content) > 0

@pytest.mark.asyncio
async def test_media_upload_multipart():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        fake_image = io.BytesIO(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4")
        res = await ac.post(
            f"{settings.API_V1_STR}/media/upload",
            files={"file": ("test.png", fake_image, "image/png")}
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["id"].startswith("img_")


@pytest.mark.asyncio
async def test_page_media_get_and_update(client):
    from tests.conftest import make_auth_headers

    # 1. Fetch all page media configs
    res = await client.get(f"{settings.API_V1_STR}/media/pages")
    assert res.status_code == 200
    pages = res.json()
    assert isinstance(pages, list)
    page_ids = [p["page_id"] for p in pages]
    assert "home" in page_ids
    assert "tournaments" in page_ids
    assert "tournament_detail" in page_ids
    assert "matches" in page_ids
    assert "leaderboard" in page_ids

    # 2. Fetch specific page media
    res = await client.get(f"{settings.API_V1_STR}/media/pages/home")
    assert res.status_code == 200
    home_cfg = res.json()
    assert home_cfg["page_id"] == "home"
    assert home_cfg["media_type"] in ["gif", "image"]

    # 3. Update page media as admin
    headers_admin = make_auth_headers("usr_super_admin")
    update_payload = {
        "page_id": "home",
        "media_type": "gif",
        "media_url": "https://assets.ametheus.gg/hero-new.gif",
        "fallback_url": "https://assets.ametheus.gg/hero-fallback.jpg",
        "is_active": True,
        "opacity": 0.35,
    }
    put_res = await client.put(
        f"{settings.API_V1_STR}/media/pages/home",
        json=update_payload,
        headers=headers_admin,
    )
    assert put_res.status_code == 200
    updated_data = put_res.json()
    assert updated_data["media_url"] == "https://assets.ametheus.gg/hero-new.gif"
    assert updated_data["fallback_url"] == "https://assets.ametheus.gg/hero-fallback.jpg"
    assert updated_data["opacity"] == 0.35

    # 4. Fetch updated config to verify persistence
    get_res = await client.get(f"{settings.API_V1_STR}/media/pages/home")
    assert get_res.status_code == 200
    assert get_res.json()["media_url"] == "https://assets.ametheus.gg/hero-new.gif"
    assert get_res.json()["fallback_url"] == "https://assets.ametheus.gg/hero-fallback.jpg"
    assert get_res.json()["opacity"] == 0.35

