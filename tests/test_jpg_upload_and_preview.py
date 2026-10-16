import pytest
import pytest_asyncio
import io
import struct
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.database import get_db
from app.models.user import User
from app.api.deps import get_current_user, require_super_admin, require_organizer

def _create_minimal_jpeg(width: int, height: int) -> bytes:
    """Creates a valid minimal JPEG byte stream with SOF0 header encoding specified width and height."""
    soi = b"\xff\xd8"
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    # SOF0 segment: marker 0xFF 0xC0, length 17, precision 8, height 2-bytes, width 2-bytes, 3 components
    sof0_payload = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    sof0_len = struct.pack(">H", len(sof0_payload) + 2)
    sof0 = b"\xff\xc0" + sof0_len + sof0_payload
    eoi = b"\xff\xd9"
    return soi + app0 + sof0 + eoi

@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
    return db

@pytest_asyncio.fixture
async def client(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db

    admin_user = User(
        id="admin_jpg_tester",
        clerk_user_id="user_admin_jpg_tester",
        email="admin@ametheus.games",
        username="admin_tester",
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
async def test_case_1_valid_jpg_recommended_ratio(client, mock_db):
    """Case 1: A valid JPG with the recommended aspect ratio (960x1200, 4:5 portrait)."""
    jpg_bytes = _create_minimal_jpeg(width=960, height=1200)
    files = {"file": ("catalog_card.jpg", io.BytesIO(jpg_bytes), "image/jpeg")}

    resp = await client.post("/api/v1/media/upload", files=files)
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "success"
    assert data["content_type"] == "image/jpeg"
    assert data["width"] == 960
    assert data["height"] == 1200
    assert data["url"].startswith("/api/v1/media/img_")

@pytest.mark.asyncio
async def test_case_2_valid_jpeg_different_aspect_ratio(client, mock_db):
    """Case 2: A valid JPEG with a different aspect ratio (2560x1080, 21:9 ultrawide)."""
    jpg_bytes = _create_minimal_jpeg(width=2560, height=1080)
    files = {"file": ("header_banner.jpeg", io.BytesIO(jpg_bytes), "image/jpeg")}

    resp = await client.post("/api/v1/media/upload", files=files)
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "success"
    assert data["content_type"] == "image/jpeg"
    assert data["width"] == 2560
    assert data["height"] == 1080

@pytest.mark.asyncio
async def test_case_3_uppercase_extension_jpg(client, mock_db):
    """Case 3: An uppercase .JPG filename."""
    jpg_bytes = _create_minimal_jpeg(width=1000, height=1000)
    files = {"file": ("HERO_FRAGMENT.JPG", io.BytesIO(jpg_bytes), "image/jpeg")}

    resp = await client.post("/api/v1/media/upload", files=files)
    assert resp.status_code == 200
    data = resp.json()

    assert data["status"] == "success"
    assert data["content_type"] == "image/jpeg"
    assert data["width"] == 1000
    assert data["height"] == 1000
    assert data["filename"] == "HERO_FRAGMENT.JPG"

@pytest.mark.asyncio
async def test_case_4_missing_or_unexpected_mime_type(client, mock_db):
    """Case 4: A valid JPG with a missing, generic (application/octet-stream), or non-standard MIME type."""
    jpg_bytes = _create_minimal_jpeg(width=900, height=1200)

    # 4a. application/octet-stream
    files_octet = {"file": ("showcase.jpg", io.BytesIO(jpg_bytes), "application/octet-stream")}
    resp_octet = await client.post("/api/v1/media/upload", files=files_octet)
    assert resp_octet.status_code == 200
    data_octet = resp_octet.json()
    assert data_octet["status"] == "success"
    assert data_octet["content_type"] == "image/jpeg"
    assert data_octet["width"] == 900
    assert data_octet["height"] == 1200

    # 4b. image/jpg non-standard MIME
    files_jpg = {"file": ("showcase2.jpg", io.BytesIO(jpg_bytes), "image/jpg")}
    resp_jpg = await client.post("/api/v1/media/upload", files=files_jpg)
    assert resp_jpg.status_code == 200
    data_jpg = resp_jpg.json()
    assert data_jpg["content_type"] == "image/jpeg"

@pytest.mark.asyncio
async def test_case_5_unsupported_or_corrupted_file(client, mock_db):
    """Case 5: An unsupported or corrupted file (e.g. text/script claiming to be a JPG)."""
    fake_content = b"This is plain text with random characters, definitely not a JPEG image file."
    files = {"file": ("malicious.jpg", io.BytesIO(fake_content), "image/jpeg")}

    resp = await client.post("/api/v1/media/upload", files=files)
    assert resp.status_code == 400
    assert "Unsupported or corrupted file" in resp.json()["detail"]

@pytest.mark.asyncio
async def test_case_6_exceeds_upload_size_limit(client, mock_db):
    """Case 6: An image that exceeds the existing upload-size limit (>20MB)."""
    oversized_content = b"\xff\xd8\xff" + (b"0" * (21 * 1024 * 1024))
    files = {"file": ("huge.jpg", io.BytesIO(oversized_content), "image/jpeg")}

    resp = await client.post("/api/v1/media/upload", files=files)
    assert resp.status_code == 413
    assert "exceeds maximum limit" in resp.json()["detail"]

@pytest.mark.asyncio
async def test_case_7_persistence_and_retrieval_after_reload(client, mock_db):
    """Case 7: Successfully uploaded image is persisted in MongoDB and retrievable via GET streaming endpoint."""
    jpg_bytes = _create_minimal_jpeg(width=1920, height=1080)
    files = {"file": ("persisted_banner.jpg", io.BytesIO(jpg_bytes), "image/jpeg")}

    upload_resp = await client.post("/api/v1/media/upload", files=files)
    assert upload_resp.status_code == 200
    upload_data = upload_resp.json()
    media_id = upload_data["id"]

    # Verify MongoDB persistence directly
    doc = await mock_db.media.find_one({"id": media_id})
    assert doc is not None
    assert doc["content_type"] == "image/jpeg"
    assert doc["width"] == 1920
    assert doc["height"] == 1080

    # Verify binary GET stream retrieval (simulates browser rendering <img src="/api/v1/media/...">)
    get_resp = await client.get(f"/api/v1/media/{media_id}")
    assert get_resp.status_code == 200
    assert get_resp.headers["content-type"] == "image/jpeg"
    assert get_resp.headers["access-control-allow-origin"] == "*"
    assert get_resp.content == jpg_bytes
