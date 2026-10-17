import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
import jwt
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.config import settings
from app.api.deps import get_db
from app.services.avatar_service import get_random_default_avatar
from app.api.v1.endpoints.crests import DEFAULT_CRESTS

@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
    # Seed default crests
    for c in DEFAULT_CRESTS:
        await db.crests.update_one(
            {"id": c["id"]},
            {"$set": {**c, "is_active": True, "created_at": datetime.now(timezone.utc)}},
            upsert=True
        )
    return db

@pytest.fixture
def rsa_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    pub_pem = public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    priv_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    return priv_pem, pub_pem

@pytest.mark.asyncio
async def test_avatar_service_random_assignment_and_admin_catalog(mock_db):
    # Verify random default selection picks from active crests
    avatar_url, source = await get_random_default_avatar(mock_db)
    assert source == "DEFAULT"
    assert (
        avatar_url.startswith("data:image/")
        or avatar_url.startswith("/assets/crests/")
        or avatar_url.startswith("/api/v1/media/")
        or avatar_url.startswith("http")
    )

    # Admin adds a custom new crest to db.crests
    new_admin_crest = {
        "id": "crest_admin_vip",
        "name": "Admin VIP Valkyrie",
        "image_url": "/api/v1/media/valkyrie_custom.png",
        "description": "Special VIP operator crest",
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
    }
    await mock_db.crests.insert_one(new_admin_crest)

    # Disable all other crests except our new admin crest
    await mock_db.crests.update_many({"id": {"$ne": "crest_admin_vip"}}, {"$set": {"is_active": False}})

    # get_random_default_avatar must pick the active admin crest
    chosen_avatar, source = await get_random_default_avatar(mock_db)
    assert chosen_avatar == "/api/v1/media/valkyrie_custom.png"
    assert source == "DEFAULT"

    # Disable the admin crest as well (now zero active crests)
    await mock_db.crests.update_one({"id": "crest_admin_vip"}, {"$set": {"is_active": False}})
    # Service safely falls back to curated DEFAULT_CRESTS, never crashing
    fallback_avatar, fallback_source = await get_random_default_avatar(mock_db)
    assert fallback_source == "DEFAULT"
    assert fallback_avatar in [c["image_url"] for c in DEFAULT_CRESTS]


@pytest.mark.asyncio
async def test_new_google_user_gets_random_avatar_and_preserves_google_pic(rsa_key_pair, mock_db, monkeypatch):
    priv_pem, pub_pem = rsa_key_pair
    monkeypatch.setattr(settings, "CLERK_JWT_KEY", pub_pem)
    app.dependency_overrides[get_db] = lambda: mock_db

    google_img = "https://lh3.googleusercontent.com/a/ACg8ocISomeGooglePhoto123"
    token = jwt.encode(
        {
            "sub": "user_google_new_101",
            "email": "gamer_google@gmail.com",
            "picture": google_img,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        priv_pem,
        algorithm="RS256",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # First-time sync for Google OAuth user
        resp = await ac.post(
            "/api/v1/auth/sync",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "clerk_user_id": "user_google_new_101",
                "email": "gamer_google@gmail.com",
                "username": "gamer_google",
                "display_name": "Gamer Google",
                "google_avatar_url": google_img,
            },
        )
        assert resp.status_code == 200
        data = resp.json()

        # The displayed avatar must be an Ametheus gaming avatar, NOT the Google picture!
        assert data["avatar_url"] != google_img
        assert (
            data["avatar_url"].startswith("data:image/")
            or data["avatar_url"].startswith("/assets/crests/")
            or data["avatar_url"].startswith("/api/v1/media/")
            or data["avatar_url"].startswith("http")
        )
        assert data["avatar_source"] == "DEFAULT"
        assert data["google_avatar_url"] == google_img

        user_doc = await mock_db.users.find_one({"_id": "user_google_new_101"})
        assert user_doc is not None
        assigned_avatar = user_doc["avatar_url"]
        assert assigned_avatar != google_img
        assert user_doc.get("avatar_source") == "DEFAULT"
        assert user_doc.get("google_avatar_url") == google_img

        # Subsequent sync (user logs in again) must NOT overwrite the assigned avatar
        resp2 = await ac.post(
            "/api/v1/auth/sync",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "clerk_user_id": "user_google_new_101",
                "email": "gamer_google@gmail.com",
                "username": "gamer_google",
                "display_name": "Gamer Google",
                "google_avatar_url": google_img,
            },
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["avatar_url"] == assigned_avatar
        assert data2["avatar_url"] != google_img
        assert data2["avatar_source"] == "DEFAULT"

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_manual_selection_of_google_picture_and_preservation(rsa_key_pair, mock_db, monkeypatch):
    priv_pem, pub_pem = rsa_key_pair
    monkeypatch.setattr(settings, "CLERK_JWT_KEY", pub_pem)
    app.dependency_overrides[get_db] = lambda: mock_db

    google_img = "https://lh3.googleusercontent.com/a/ACg8ocISomeGooglePhoto202"
    token = jwt.encode(
        {
            "sub": "user_manual_google_202",
            "email": "manual_google@gmail.com",
            "picture": google_img,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        priv_pem,
        algorithm="RS256",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # First sync
        await ac.post(
            "/api/v1/auth/sync",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "clerk_user_id": "user_manual_google_202",
                "email": "manual_google@gmail.com",
                "username": "manual_google",
                "google_avatar_url": google_img,
            },
        )

        # User goes to profile customization and explicitly clicks "Equip Google Photo"
        update_resp = await ac.put(
            "/api/v1/auth/profile",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "avatar_url": google_img,
                "avatar_source": "GOOGLE",
            },
        )
        assert update_resp.status_code == 200
        update_data = update_resp.json()
        assert update_data["avatar_url"] == google_img
        assert update_data["avatar_source"] == "GOOGLE"

        # Subsequent syncs must preserve the user's voluntary choice of Google photo
        sync_resp = await ac.post(
            "/api/v1/auth/sync",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "clerk_user_id": "user_manual_google_202",
                "email": "manual_google@gmail.com",
                "username": "manual_google",
                "google_avatar_url": google_img,
            },
        )
        assert sync_resp.status_code == 200
        sync_data = sync_resp.json()
        assert sync_data["avatar_url"] == google_img
        assert sync_data["avatar_source"] == "GOOGLE"

        # User now switches to a sanctioned crest
        crest_resp = await ac.put(
            "/api/v1/auth/profile",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "avatar_url": "/assets/crests/crest-apex-predator.png",
                "avatar_source": "CREST",
            },
        )
        assert crest_resp.status_code == 200
        crest_data = crest_resp.json()
        assert crest_data["avatar_url"] == "/assets/crests/crest-apex-predator.png"
        assert crest_data["avatar_source"] == "CREST"

        # Subsequent syncs must preserve the crest
        sync_resp2 = await ac.post(
            "/api/v1/auth/sync",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "clerk_user_id": "user_manual_google_202",
                "email": "manual_google@gmail.com",
                "username": "manual_google",
                "google_avatar_url": google_img,
            },
        )
        assert sync_resp2.status_code == 200
        assert sync_resp2.json()["avatar_url"] == "/assets/crests/crest-apex-predator.png"
        assert sync_resp2.json()["avatar_source"] == "CREST"

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_existing_user_avatar_never_overwritten_on_sync(rsa_key_pair, mock_db, monkeypatch):
    priv_pem, pub_pem = rsa_key_pair
    monkeypatch.setattr(settings, "CLERK_JWT_KEY", pub_pem)
    app.dependency_overrides[get_db] = lambda: mock_db

    # Pre-populate an existing veteran user who already has a custom uploaded avatar
    await mock_db.users.insert_one({
        "_id": "user_veteran_303",
        "clerk_user_id": "user_veteran_303",
        "email": "veteran@gmail.com",
        "username": "veteran_pro",
        "username_lower": "veteran_pro",
        "avatar_url": "/api/v1/media/veteran_custom_badge.png",
        "avatar_source": "UPLOADED",
        "role": "PLAYER",
        "created_at": datetime.now(timezone.utc),
    })

    token = jwt.encode(
        {
            "sub": "user_veteran_303",
            "email": "veteran@gmail.com",
            "picture": "https://lh3.googleusercontent.com/a/DifferentGooglePhoto",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        priv_pem,
        algorithm="RS256",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/api/v1/auth/sync",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "clerk_user_id": "user_veteran_303",
                "email": "veteran@gmail.com",
                "username": "veteran_pro",
                "google_avatar_url": "https://lh3.googleusercontent.com/a/DifferentGooglePhoto",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["avatar_url"] == "/api/v1/media/veteran_custom_badge.png"
        assert data["avatar_source"] == "UPLOADED"
        assert data["google_avatar_url"] == "https://lh3.googleusercontent.com/a/DifferentGooglePhoto"

    app.dependency_overrides.clear()
