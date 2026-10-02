import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.database import get_db
from app.models.admin import OFFICIAL_HERO_GIF_URL, HeroPresentationSettings
from app.models.user import User
from app.api.deps import get_current_user, require_super_admin

@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
    return db

@pytest_asyncio.fixture
async def client(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db
    
    # Mock Super Admin user
    admin_user = User(
        id="admin_123",
        clerk_user_id="user_admin_123",
        email="admin@ametheus.gg",
        username="superadmin",
        role="SUPER_ADMIN",
    )
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[require_super_admin] = lambda: admin_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_hero_settings_defaults(client):
    """Verify that GET /admin/hero/settings returns official defaults with immutable asset."""
    resp = await client.get("/api/v1/admin/hero/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["asset_url"] == OFFICIAL_HERO_GIF_URL
    assert data["is_locked"] is True
    assert data["animation_speed"] == 1.0
    assert data["opacity"] == 0.40

@pytest.mark.asyncio
async def test_hero_settings_rejects_asset_url_mutation(client):
    """Verify that PATCH /admin/hero/settings rejects any attempt to mutate asset URL."""
    payload = {
        "asset_url": "https://malicious.example.com/fake.gif",
        "brightness": 1.2,
    }
    resp = await client.patch("/api/v1/admin/hero/settings", json=payload)
    assert resp.status_code == 400
    assert "locked and protected" in resp.json()["detail"]

    # Also test with media_url or hero_gif
    resp2 = await client.patch("/api/v1/admin/hero/settings", json={"media_url": "http://test.com"})
    assert resp2.status_code == 400

@pytest.mark.asyncio
async def test_hero_settings_updates_presentation(client):
    """Verify that PATCH /admin/hero/settings allows tuning presentation settings."""
    payload = {
        "animation_speed": 1.5,
        "brightness": 1.2,
        "opacity": 0.5,
        "contrast": 1.1,
        "saturation": 1.3,
        "overlay_opacity": 0.25,
    }
    resp = await client.patch("/api/v1/admin/hero/settings", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["animation_speed"] == 1.5
    assert data["brightness"] == 1.2
    assert data["opacity"] == 0.5
    assert data["contrast"] == 1.1
    assert data["saturation"] == 1.3
    assert data["overlay_opacity"] == 0.25
    assert data["asset_url"] == OFFICIAL_HERO_GIF_URL

@pytest.mark.asyncio
async def test_media_page_home_rejects_asset_mutation(client):
    """Verify that PUT /media/pages/home blocks altering the hero media asset."""
    payload = {
        "page_id": "home",
        "title": "Homepage Hero",
        "media_type": "gif",
        "media_url": "https://attacker.com/fake.gif",
        "is_active": True,
    }
    resp = await client.put("/api/v1/media/pages/home", json=payload)
    assert resp.status_code == 400
    assert "locked and protected" in resp.json()["detail"]

@pytest.mark.asyncio
async def test_public_hero_settings_endpoint(client):
    """Verify that public GET /media/hero/settings is accessible."""
    resp = await client.get("/api/v1/media/hero/settings")
    assert resp.status_code == 200
    assert resp.json()["asset_url"] == OFFICIAL_HERO_GIF_URL

@pytest.mark.asyncio
async def test_get_current_user_super_admin_resolution(mock_db):
    """Verify that get_current_user resolves super admin role authoritatively from email/headers."""
    from app.core.security import create_access_token
    token = create_access_token(data={"sub": "user_clerk_admin_999"})

    # Call get_current_user with X-User-Email matching designated admin
    user = await get_current_user(
        authorization=f"Bearer {token}",
        x_user_email="pk.4081988@gmail.com",
        db=mock_db,
    )
    assert user.role == "SUPER_ADMIN"
    assert user.email == "pk.4081988@gmail.com"

    # Verify document in DB has SUPER_ADMIN and authentic email
    doc = await mock_db.users.find_one({"clerk_user_id": "user_clerk_admin_999"})
    assert doc is not None
    assert doc["role"] == "SUPER_ADMIN"
    assert doc["email"] == "pk.4081988@gmail.com"

@pytest.mark.asyncio
async def test_get_current_user_account_linking(mock_db):
    """Verify that an existing user without clerk_user_id is linked upon login."""
    # Seed an existing organizer in DB with no clerk_user_id
    await mock_db.users.insert_one({
        "_id": "existing_org_1",
        "email": "organizer@ametheus.gg",
        "username": "chief_org",
        "role": "ORGANIZER",
        "inr_balance_paise": 50000,
    })

    from app.core.security import create_access_token
    token = create_access_token(data={"sub": "user_clerk_org_888"})

    user = await get_current_user(
        authorization=f"Bearer {token}",
        x_user_email="organizer@ametheus.gg",
        db=mock_db,
    )
    assert user.role == "ORGANIZER"
    assert user.id == "existing_org_1"
    assert user.clerk_user_id == "user_clerk_org_888"

    # Check DB was linked
    doc = await mock_db.users.find_one({"_id": "existing_org_1"})
    assert doc["clerk_user_id"] == "user_clerk_org_888"

@pytest.mark.asyncio
async def test_auth_sync_merges_duplicate_placeholder(mock_db):
    """Verify that /auth/sync merges an existing seeded account with a placeholder clerk account."""
    # 1. Pre-existing database record
    await mock_db.users.insert_one({
        "_id": "seed_admin_1",
        "email": "pk.4081988@gmail.com",
        "username": "pk_original",
        "role": "SUPER_ADMIN",
        "inr_balance_paise": 100000,
    })

    # 2. Duplicate auto-provisioned placeholder record
    await mock_db.users.insert_one({
        "_id": "user_2testclerk123",
        "clerk_user_id": "user_2testclerk123",
        "email": "user_2testclerk123@ametheus.gg",
        "username": "user_2testcl",
        "role": "PLAYER",
    })

    from app.core.security import create_access_token
    token = create_access_token(data={"sub": "user_2testclerk123"})

    app.dependency_overrides[get_db] = lambda: mock_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        sync_payload = {
            "clerk_user_id": "user_2testclerk123",
            "email": "pk.4081988@gmail.com",
            "username": "pk_super",
            "display_name": "Prateek",
        }
        resp = await ac.post(
            "/api/v1/auth/sync",
            json=sync_payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["role"] == "SUPER_ADMIN"
        assert data["email"] == "pk.4081988@gmail.com"
        assert data["clerk_user_id"] == "user_2testclerk123"

        # Verify duplicate placeholder was consolidated/removed
        placeholder_doc = await mock_db.users.find_one({"_id": "user_2testclerk123"})
        assert placeholder_doc is None

        # Verify canonical user retained balances
        canonical_doc = await mock_db.users.find_one({"_id": "seed_admin_1"})
        assert canonical_doc["clerk_user_id"] == "user_2testclerk123"
        assert canonical_doc["inr_balance_paise"] == 100000
        assert canonical_doc["role"] == "SUPER_ADMIN"
    app.dependency_overrides.clear()
