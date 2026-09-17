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
