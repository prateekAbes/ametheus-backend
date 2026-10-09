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
from app.core.security import decode_token, format_pem_public_key
from app.api.deps import get_db

@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
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
async def test_clerk_jwt_key_verification_rs256(rsa_key_pair, monkeypatch):
    priv_pem, pub_pem = rsa_key_pair
    monkeypatch.setattr(settings, "CLERK_JWT_KEY", pub_pem)
    monkeypatch.setattr(settings, "CLERK_ISSUER", "https://clerk.ametheus.gg")

    payload = {
        "sub": "user_2new_clerk_999",
        "iss": "https://clerk.ametheus.gg",
        "email": "player_clerk@ametheus.gg",
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    token = jwt.encode(payload, priv_pem, algorithm="RS256")

    decoded = decode_token(token)
    assert decoded is not None
    assert decoded["sub"] == "user_2new_clerk_999"

@pytest.mark.asyncio
async def test_clerk_expired_token_rejected(rsa_key_pair, monkeypatch):
    priv_pem, pub_pem = rsa_key_pair
    monkeypatch.setattr(settings, "CLERK_JWT_KEY", pub_pem)

    payload = {
        "sub": "user_2expired",
        "iss": "https://clerk.ametheus.gg",
        "exp": datetime.now(timezone.utc) - timedelta(hours=2),
    }
    token = jwt.encode(payload, priv_pem, algorithm="RS256")

    decoded = decode_token(token)
    assert decoded is None

@pytest.mark.asyncio
async def test_deleted_clerk_app_token_rejected(rsa_key_pair, monkeypatch):
    priv_pem, pub_pem = rsa_key_pair
    monkeypatch.setattr(settings, "CLERK_JWT_KEY", pub_pem)

    # Token claiming to be from the deleted valid-goose-2403 domain
    payload = {
        "sub": "user_stale_123",
        "iss": "https://valid-goose-2403.clerk.accounts.dev",
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    token = jwt.encode(payload, priv_pem, algorithm="RS256")

    decoded = decode_token(token)
    assert decoded is None

@pytest.mark.asyncio
async def test_protected_endpoint_rejects_missing_and_invalid_auth(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Missing Authorization header -> 401
        res = await client.get("/api/v1/wallet/balance")
        assert res.status_code == 401
        assert "Bearer" in res.json().get("detail", "")

        # 2. Invalid Bearer format -> 401
        res = await client.get("/api/v1/wallet/balance", headers={"Authorization": "Basic 12345"})
        assert res.status_code == 401

        # 3. Invalid/garbage JWT -> 401
        res = await client.get("/api/v1/wallet/balance", headers={"Authorization": "Bearer invalid.jwt.token"})
        assert res.status_code == 401

    app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_clerk_authenticated_user_sync_and_wallet_protection(rsa_key_pair, mock_db, monkeypatch):
    priv_pem, pub_pem = rsa_key_pair
    monkeypatch.setattr(settings, "CLERK_JWT_KEY", pub_pem)
    monkeypatch.setattr(settings, "CLERK_ISSUER", "https://clerk.ametheus.gg")

    app.dependency_overrides[get_db] = lambda: mock_db

    # Create token for fresh Clerk user
    token_payload = {
        "sub": "user_2new_contender_444",
        "iss": "https://clerk.ametheus.gg",
        "email": "contender444@ametheus.gg",
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    token = jwt.encode(token_payload, priv_pem, algorithm="RS256")
    auth_header = {"Authorization": f"Bearer {token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Access wallet balance directly with Clerk token -> auto provisions in DB with zero balance
        res = await client.get("/api/v1/wallet/balance", headers=auth_header)
        assert res.status_code == 200
        data = res.json()
        assert data["available_paise"] == 0

        # Verify DB document was mapped by clerk_user_id
        user_doc = await mock_db.users.find_one({"clerk_user_id": "user_2new_contender_444"})
        assert user_doc is not None
        assert user_doc["role"] == "PLAYER"

        # 2. Credit wallet in DB directly
        await mock_db.users.update_one(
            {"_id": user_doc["_id"]},
            {"$set": {"inr_balance_paise": 50000, "inr_balance": "500.00"}}
        )

        # 3. Re-authenticate: must NOT reset wallet balance!
        res2 = await client.get("/api/v1/wallet/balance", headers=auth_header)
        assert res2.status_code == 200
        assert res2.json()["available_paise"] == 50000

        # 4. Sync profile endpoint with same token
        sync_payload = {
            "clerk_user_id": "user_2new_contender_444",
            "email": "contender444@ametheus.gg",
            "username": "contender444",
            "display_name": "Contender 444",
        }
        sync_res = await client.post("/api/v1/auth/sync", json=sync_payload, headers=auth_header)
        assert sync_res.status_code == 200

        # Verify wallet balance remained intact after profile sync
        res3 = await client.get("/api/v1/wallet/balance", headers=auth_header)
        assert res3.status_code == 200
        assert res3.json()["available_paise"] == 50000

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_new_user_google_signup_empty_username_and_onboarding(rsa_key_pair, mock_db, monkeypatch):
    """
    Verifies that a new user signing up via Google (who has no username yet):
    1. Successfully synchronizes without duplicate key errors on empty username.
    2. Receives a unique temporary player handle.
    3. Has needs_username=True and has_chosen_username=False.
    4. A second new user with empty username also syncs successfully without collision.
    5. Completing username setup via PUT /api/v1/auth/profile establishes the chosen handle and clears needs_username.
    """
    priv_pem, pub_pem = rsa_key_pair
    monkeypatch.setattr(settings, "CLERK_JWT_KEY", pub_pem)
    monkeypatch.setattr(settings, "CLERK_ISSUER", "https://clerk.ametheus.gg")

    app.dependency_overrides[get_db] = lambda: mock_db

    # User 1: New Google user with empty username
    user1_id = "user_2google_alpha_111"
    user1_token = jwt.encode(
        {"sub": user1_id, "iss": "https://clerk.ametheus.gg", "email": "alpha@gmail.com", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        priv_pem,
        algorithm="RS256",
    )
    user1_auth = {"Authorization": f"Bearer {user1_token}"}

    # User 2: Second new Google user with empty username
    user2_id = "user_2google_beta_222"
    user2_token = jwt.encode(
        {"sub": user2_id, "iss": "https://clerk.ametheus.gg", "email": "beta@gmail.com", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        priv_pem,
        algorithm="RS256",
    )
    user2_auth = {"Authorization": f"Bearer {user2_token}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. First Google user syncs with empty username
        payload1 = {
            "clerk_user_id": user1_id,
            "email": "alpha@gmail.com",
            "username": "",
            "display_name": "Alpha Contender",
            "has_chosen_username": False,
        }
        res1 = await client.post("/api/v1/auth/sync", json=payload1, headers=user1_auth)
        assert res1.status_code == 200, res1.text
        data1 = res1.json()
        assert data1["needs_username"] is True
        assert data1["has_chosen_username"] is False
        assert data1["username"].startswith("player_")
        assert len(data1["username"]) >= 10

        # 2. Second Google user syncs with empty username (MUST NOT collide!)
        payload2 = {
            "clerk_user_id": user2_id,
            "email": "beta@gmail.com",
            "username": "",
            "display_name": "Beta Contender",
            "has_chosen_username": False,
        }
        res2 = await client.post("/api/v1/auth/sync", json=payload2, headers=user2_auth)
        assert res2.status_code == 200, res2.text
        data2 = res2.json()
        assert data2["needs_username"] is True
        assert data2["has_chosen_username"] is False
        assert data2["username"].startswith("player_")
        assert data2["username"] != data1["username"]

        # 3. First user completes onboarding with their chosen handle
        profile_res = await client.put(
            "/api/v1/auth/profile",
            json={"username": "AlphaStrike", "display_name": "Alpha Strike"},
            headers=user1_auth,
        )
        assert profile_res.status_code == 200, profile_res.text
        updated1 = profile_res.json()
        assert updated1["username"] == "AlphaStrike"
        assert updated1["needs_username"] is False
        assert updated1["has_chosen_username"] is True

        # 4. Verify GET /api/v1/auth/me returns the authoritative chosen handle
        me_res = await client.get("/api/v1/auth/me", headers=user1_auth)
        assert me_res.status_code == 200
        assert me_res.json()["username"] == "AlphaStrike"
        assert me_res.json()["needs_username"] is False
        assert me_res.json()["has_chosen_username"] is True

    app.dependency_overrides.clear()

