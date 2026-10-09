import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport, Response
from unittest.mock import patch, AsyncMock
from datetime import datetime, timezone
from fastapi import HTTPException

from app.main import app
from app.core.database import get_db
from app.core.config import settings
from app.models.user import User
from app.api.deps import get_current_user
from app.api.v1.endpoints.auth import delete_clerk_user


@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
    return db


@pytest_asyncio.fixture
async def sample_user(mock_db):
    user_doc = {
        "_id": "user_del_123",
        "id": "user_del_123",
        "clerk_user_id": "clerk_del_123",
        "email": "delete_me@ametheus.gg",
        "username": "DoomedPlayer",
        "role": "PLAYER",
        "inr_balance_paise": 0,
        "inr_reserved_paise": 0,
        "inr_balance": "0.00",
    }
    await mock_db.users.insert_one(user_doc)
    return user_doc


@pytest.mark.asyncio
async def test_account_deletion_unauthenticated(mock_db):
    """
    Criterion 2: An unauthenticated request is rejected with 401 Unauthorized.
    """
    app.dependency_overrides[get_db] = lambda: mock_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.delete("/api/v1/auth/me")
        assert res.status_code == 401
        assert "Bearer" in res.json().get("detail", "")
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_account_deletion_cannot_delete_other_user(mock_db, sample_user):
    """
    Criterion 3: Identity is strictly bound to session token; user cannot delete another user.
    """
    victim_user = {
        "_id": "victim_456",
        "id": "victim_456",
        "clerk_user_id": "clerk_victim_456",
        "email": "victim@ametheus.gg",
        "username": "VictimPlayer",
        "role": "PLAYER",
        "inr_balance_paise": 0,
    }
    await mock_db.users.insert_one(victim_user)

    auth_user = User(
        id=sample_user["_id"],
        clerk_user_id=sample_user["clerk_user_id"],
        email=sample_user["email"],
        username=sample_user["username"],
        role="PLAYER",
    )
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: auth_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.delete("/api/v1/auth/me")
        assert res.status_code == 200

        # Victim user remains intact in DB!
        victim_check = await mock_db.users.find_one({"_id": "victim_456"})
        assert victim_check is not None
        assert victim_check["username"] == "VictimPlayer"

        # Calling user's account is removed
        caller_check = await mock_db.users.find_one({"_id": "user_del_123"})
        assert caller_check is None

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_account_deletion_with_active_wallet_balance_rejected(mock_db):
    """
    Criterion 8 & Section 5: Nonzero wallet balance halts deletion to prevent funds loss.
    """
    funded_user = {
        "_id": "user_funded_789",
        "id": "user_funded_789",
        "clerk_user_id": "clerk_funded_789",
        "email": "rich_player@ametheus.gg",
        "username": "RichPlayer",
        "role": "PLAYER",
        "inr_balance_paise": 50000,  # ₹500.00
        "inr_reserved_paise": 0,
        "inr_balance": "500.00",
    }
    await mock_db.users.insert_one(funded_user)

    auth_user = User(
        id="user_funded_789",
        clerk_user_id="clerk_funded_789",
        email="rich_player@ametheus.gg",
        username="RichPlayer",
        role="PLAYER",
    )
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: auth_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.delete("/api/v1/auth/me")
        assert res.status_code == 400
        assert "active wallet balance" in res.json().get("detail", "").lower()
        assert "500.00" in res.json().get("detail", "")

        # Verify account was NOT deleted
        db_user = await mock_db.users.find_one({"_id": "user_funded_789"})
        assert db_user is not None

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_account_deletion_with_pending_payment_rejected(mock_db, sample_user):
    """
    Criterion 8 & Section 5: Pending payment transactions halt deletion until settled.
    """
    await mock_db.payments.insert_one({
        "_id": "pay_pending_1",
        "user_id": "user_del_123",
        "amount_paise": 25000,
        "status": "PENDING",
        "created_at": datetime.now(timezone.utc),
    })

    auth_user = User(
        id=sample_user["_id"],
        clerk_user_id=sample_user["clerk_user_id"],
        email=sample_user["email"],
        username=sample_user["username"],
        role="PLAYER",
    )
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: auth_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.delete("/api/v1/auth/me")
        assert res.status_code == 400
        assert "pending payment transactions" in res.json().get("detail", "").lower()

        # Verify account was NOT deleted
        db_user = await mock_db.users.find_one({"_id": "user_del_123"})
        assert db_user is not None

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_account_deletion_with_pending_game_payment_rejected(mock_db, sample_user):
    """
    Pending payment transactions in game_payments also halt deletion until settled.
    """
    await mock_db.game_payments.insert_one({
        "_id": "gpay_pending_1",
        "user_id": "user_del_123",
        "amount_paise": 15000,
        "status": "INITIATED",
        "created_at": datetime.now(timezone.utc),
    })

    auth_user = User(
        id=sample_user["_id"],
        clerk_user_id=sample_user["clerk_user_id"],
        email=sample_user["email"],
        username=sample_user["username"],
        role="PLAYER",
    )
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: auth_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.delete("/api/v1/auth/me")
        assert res.status_code == 400
        assert "pending payment transactions" in res.json().get("detail", "").lower()

        # Verify account was NOT deleted
        db_user = await mock_db.users.find_one({"_id": "user_del_123"})
        assert db_user is not None

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_account_deletion_success_and_relationship_cleanup(mock_db, sample_user):
    """
    Criterion 1, 5, 6, 7, 9:
    - User can delete their own account.
    - Clerk server-side deletion is called.
    - Teams memberships and captaincies updated cleanly.
    - User notifications and sessions removed.
    - Support tickets anonymized.
    - Audit log recorded.
    """
    # 1. Setup team where user is a regular member
    await mock_db.teams.insert_one({
        "_id": "team_member_doc",
        "name": "Squad One",
        "captain_id": "other_captain",
        "members": [
            {"user_id": "other_captain", "role": "CAPTAIN"},
            {"user_id": "user_del_123", "role": "MEMBER"},
        ],
        "substitutes": [],
    })

    # 2. Setup team where user is captain with another teammate
    await mock_db.teams.insert_one({
        "_id": "team_captain_doc",
        "name": "Squad Two",
        "captain_id": "user_del_123",
        "members": [
            {"user_id": "user_del_123", "role": "CAPTAIN"},
            {"user_id": "teammate_99", "role": "MEMBER"},
        ],
        "substitutes": [],
    })

    # 3. Setup notifications and sessions
    await mock_db.notifications.insert_one({
        "_id": "notif_1",
        "user_id": "user_del_123",
        "title": "Welcome",
    })
    await mock_db.user_sessions.insert_one({
        "_id": "sess_1",
        "user_id": "user_del_123",
        "token": "tok_123",
    })

    # 4. Setup support ticket
    await mock_db.support_tickets.insert_one({
        "_id": "ticket_1",
        "user_id": "user_del_123",
        "user_email": "delete_me@ametheus.gg",
        "user_name": "DoomedPlayer",
        "subject": "Help with match",
    })

    auth_user = User(
        id=sample_user["_id"],
        clerk_user_id=sample_user["clerk_user_id"],
        email=sample_user["email"],
        username=sample_user["username"],
        role="PLAYER",
    )
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: auth_user

    # Mock Clerk API deletion helper
    with patch("app.api.v1.endpoints.auth.delete_clerk_user", new_callable=AsyncMock) as mock_clerk_del:
        mock_clerk_del.return_value = True

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.delete("/api/v1/auth/me")
            assert res.status_code == 200
            assert res.json()["status"] == "success"

            # Check Clerk delete was called with the user's clerk_user_id
            mock_clerk_del.assert_awaited_once_with("clerk_del_123")

            # Check user deleted from DB
            u_check = await mock_db.users.find_one({"_id": "user_del_123"})
            assert u_check is None

            # Check team 1: user removed from members, other_captain remains
            t1 = await mock_db.teams.find_one({"_id": "team_member_doc"})
            assert len(t1["members"]) == 1
            assert t1["members"][0]["user_id"] == "other_captain"

            # Check team 2: captain promoted to teammate_99
            t2 = await mock_db.teams.find_one({"_id": "team_captain_doc"})
            assert t2["captain_id"] == "teammate_99"
            assert len(t2["members"]) == 1
            assert t2["members"][0]["role"] == "CAPTAIN"

            # Check notifications & sessions deleted
            notif_cnt = await mock_db.notifications.count_documents({"user_id": "user_del_123"})
            assert notif_cnt == 0
            sess_cnt = await mock_db.user_sessions.count_documents({"user_id": "user_del_123"})
            assert sess_cnt == 0

            # Check support ticket anonymized
            tkt = await mock_db.support_tickets.find_one({"_id": "ticket_1"})
            assert tkt["user_email"] == "deleted_user@ametheus.gg"
            assert tkt["is_user_deleted"] is True

            # Check audit log entry exists
            audit_entry = await mock_db.audit_logs.find_one({"action": "DELETE_ACCOUNT"})
            assert audit_entry is not None
            assert "DoomedPlayer" in audit_entry["operator"]

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_account_deletion_clerk_api_failure_handled_safely(mock_db, sample_user):
    """
    Criterion 11 & Section 4: If Clerk server API call fails, return 502 and DO NOT delete MongoDB user!
    """
    auth_user = User(
        id=sample_user["_id"],
        clerk_user_id=sample_user["clerk_user_id"],
        email=sample_user["email"],
        username=sample_user["username"],
        role="PLAYER",
    )
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: auth_user

    with patch("app.api.v1.endpoints.auth.delete_clerk_user", new_callable=AsyncMock) as mock_clerk_del:
        mock_clerk_del.side_effect = HTTPException(status_code=502, detail="Identity provider failed")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            res = await ac.delete("/api/v1/auth/me")
            assert res.status_code == 502
            assert "identity provider" in res.json().get("detail", "").lower()

            # CRITICAL: User in MongoDB must NOT be deleted so they can retry!
            u_check = await mock_db.users.find_one({"_id": "user_del_123"})
            assert u_check is not None
            assert u_check["username"] == "DoomedPlayer"

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_clerk_user_helper_unit():
    """
    Criterion 10, 11: Unit test for delete_clerk_user testing HTTP responses from Clerk API directly.
    """
    with patch.object(settings, "CLERK_SECRET_KEY", "sk_test_mock_secret"):
        # 1. Success 200
        with patch("httpx.AsyncClient.delete", new_callable=AsyncMock) as mock_del:
            mock_del.return_value = Response(200, json={"deleted": True})
            result = await delete_clerk_user("clerk_user_1")
            assert result is True

        # 2. 404 (already deleted) -> Treated as success
        with patch("httpx.AsyncClient.delete", new_callable=AsyncMock) as mock_del:
            mock_del.return_value = Response(404, json={"errors": [{"code": "resource_not_found"}]})
            result = await delete_clerk_user("clerk_user_2")
            assert result is True

        # 3. 500 error -> Raises 502 Bad Gateway
        with patch("httpx.AsyncClient.delete", new_callable=AsyncMock) as mock_del:
            mock_del.return_value = Response(500, text="Internal Clerk Error")
            with pytest.raises(HTTPException) as exc_info:
                await delete_clerk_user("clerk_user_3")
            assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_account_deletion_alias_endpoint(mock_db, sample_user):
    """
    Verifies that DELETE /api/v1/auth/account functions identically to /api/v1/auth/me.
    """
    auth_user = User(
        id=sample_user["_id"],
        clerk_user_id=sample_user["clerk_user_id"],
        email=sample_user["email"],
        username=sample_user["username"],
        role="PLAYER",
    )
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: auth_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        res = await ac.delete("/api/v1/auth/account")
        assert res.status_code == 200
        assert res.json()["status"] == "success"

        u_check = await mock_db.users.find_one({"_id": "user_del_123"})
        assert u_check is None

    app.dependency_overrides.clear()
