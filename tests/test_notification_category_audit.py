import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.database import get_db
from app.models.user import User
from app.api.deps import get_current_user, require_super_admin
from app.services.notification import create_notification, migrate_notification_categories
from app.services.notification_category import (
    resolve_canonical_category,
    get_category_query_filter,
    resolve_notification_label,
    CANONICAL_CATEGORIES,
)


@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
    return db


@pytest_asyncio.fixture
async def client(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db

    test_user = User(
        id="user_test_999",
        clerk_user_id="clerk_user_999",
        email="testuser@ametheus.gg",
        username="contender_one",
        role="PLAYER",
    )
    app.dependency_overrides[get_current_user] = lambda: test_user
    app.dependency_overrides[require_super_admin] = lambda: test_user

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


def test_canonical_category_resolution():
    """Verify that all event types and titles normalize to the 5 canonical categories."""
    assert resolve_canonical_category("PAYMENT") == "WALLET"
    assert resolve_canonical_category("WALLET") == "WALLET"
    assert resolve_canonical_category("WALLET_TOPUP") == "WALLET"
    assert resolve_canonical_category("PAYOUT") == "WALLET"
    assert resolve_canonical_category("WITHDRAWAL") == "WALLET"
    assert resolve_canonical_category("DEPOSIT") == "WALLET"

    assert resolve_canonical_category("REGISTRATION") == "REGISTRATION"
    assert resolve_canonical_category("TOURNAMENT", "Tournament Registered") == "REGISTRATION"
    assert resolve_canonical_category(None, "Registration Confirmed") == "REGISTRATION"

    assert resolve_canonical_category("TEAM") == "TEAM"
    assert resolve_canonical_category("TEAM_JOIN") == "TEAM"
    assert resolve_canonical_category("ROSTER") == "TEAM"
    assert resolve_canonical_category(None, "Team Created") == "TEAM"

    assert resolve_canonical_category("BATCH") == "BATCHES"
    assert resolve_canonical_category("BATCHES") == "BATCHES"
    assert resolve_canonical_category("BATCH_PUBLISH") == "BATCHES"

    assert resolve_canonical_category("SYSTEM") == "SYSTEM"
    assert resolve_canonical_category("ADMIN") == "SYSTEM"
    assert resolve_canonical_category("SUPPORT") == "SYSTEM"
    assert resolve_canonical_category("WELCOME") == "SYSTEM"


@pytest.mark.asyncio
async def test_wallet_notifications_category_filter(client, mock_db):
    """Verify wallet notifications (modern + legacy) appear under WALLET category and not others."""
    # 1. Modern wallet notification
    await create_notification(
        db=mock_db,
        user_id="user_test_999",
        title="Deposit Approved",
        message="₹500 has been credited to your wallet.",
        notification_type="WALLET",
        category="WALLET",
    )

    # 2. Legacy payment notification (no category field, type='PAYMENT')
    await mock_db.notifications.insert_one({
        "_id": "legacy_pay_1",
        "user_id": "user_test_999",
        "type": "PAYMENT",
        "title": "Payment Approved",
        "message": "Legacy payment receipt for ₹250",
        "is_read": False,
        "is_cleared": False,
        "created_at": "2026-01-01T00:00:00Z",
    })

    # Query WALLET filter
    resp = await client.get("/api/v1/notifications?category=WALLET")
    assert resp.status_code == 200
    data = resp.json()
    items = data.get("items", [])
    assert len(items) == 2
    for it in items:
        assert it["category"] == "WALLET"

    # Query TEAM filter -> should be empty
    resp_team = await client.get("/api/v1/notifications?category=TEAM")
    assert resp_team.status_code == 200
    assert len(resp_team.json().get("items", [])) == 0


@pytest.mark.asyncio
async def test_registration_notifications_category_filter(client, mock_db):
    """Verify tournament and game registrations appear under REGISTRATION category."""
    # 1. Modern registration notification
    await create_notification(
        db=mock_db,
        user_id="user_test_999",
        title="Tournament Registered",
        message="You are confirmed for BGMI Masters Cup.",
        notification_type="REGISTRATION",
        category="REGISTRATION",
    )

    # 2. Legacy tournament registration record
    await mock_db.notifications.insert_one({
        "_id": "legacy_reg_1",
        "user_id": "user_test_999",
        "type": "TOURNAMENT",
        "title": "Tournament Registered",
        "message": "Solo entry confirmed",
        "is_read": False,
        "is_cleared": False,
        "created_at": "2026-01-01T00:00:00Z",
    })

    resp = await client.get("/api/v1/notifications?category=REGISTRATION")
    assert resp.status_code == 200
    items = resp.json().get("items", [])
    assert len(items) == 2
    for it in items:
        assert it["category"] == "REGISTRATION"


@pytest.mark.asyncio
async def test_team_notifications_category_filter(client, mock_db):
    """Verify team actions (create, join, roster change) appear under TEAM category."""
    await create_notification(
        db=mock_db,
        user_id="user_test_999",
        title="Team Created",
        message="You successfully formed team 'Apex Predators'.",
        notification_type="TEAM",
        category="TEAM",
    )

    await create_notification(
        db=mock_db,
        user_id="user_test_999",
        title="New Team Member",
        message="Ghost joined your team roster.",
        notification_type="TEAM",
        category="TEAM",
    )

    resp = await client.get("/api/v1/notifications?category=TEAM")
    assert resp.status_code == 200
    items = resp.json().get("items", [])
    assert len(items) == 2
    for it in items:
        assert it["category"] == "TEAM"


@pytest.mark.asyncio
async def test_batches_category_filter_empty_and_populated(client, mock_db):
    """Verify Batches category displays empty state gracefully and returns batch notifications when present."""
    # Initially user has no batch notifications
    resp_empty = await client.get("/api/v1/notifications?category=BATCHES")
    assert resp_empty.status_code == 200
    assert resp_empty.json().get("items", []) == []

    # Insert batch announcement
    await create_notification(
        db=mock_db,
        user_id="user_test_999",
        title="Batch Roster Announcement",
        message="Round 1 match brackets published in batch.",
        notification_type="BATCHES",
        category="BATCHES",
    )

    resp_populated = await client.get("/api/v1/notifications?category=BATCHES")
    assert resp_populated.status_code == 200
    items = resp_populated.json().get("items", [])
    assert len(items) == 1
    assert items[0]["category"] == "BATCHES"


@pytest.mark.asyncio
async def test_system_category_filter(client, mock_db):
    """Verify system announcements and support messages appear under SYSTEM."""
    await create_notification(
        db=mock_db,
        user_id="user_test_999",
        title="Welcome to Ametheus",
        message="Platform maintenance scheduled at midnight.",
        notification_type="SYSTEM",
        category="SYSTEM",
    )

    resp = await client.get("/api/v1/notifications?category=SYSTEM")
    assert resp.status_code == 200
    items = resp.json().get("items", [])
    assert len(items) == 1
    assert items[0]["category"] == "SYSTEM"


@pytest.mark.asyncio
async def test_all_filter_and_unread_count(client, mock_db):
    """Verify 'All' category returns all notifications across all categories without exclusion."""
    await create_notification(mock_db, "user_test_999", "Wallet Topup", "₹100 added", "WALLET", "WALLET")
    await create_notification(mock_db, "user_test_999", "Tournament Registered", "Entry confirmed", "REGISTRATION", "REGISTRATION")
    await create_notification(mock_db, "user_test_999", "Team Created", "Team active", "TEAM", "TEAM")
    await create_notification(mock_db, "user_test_999", "Batch Published", "Brackets ready", "BATCHES", "BATCHES")
    await create_notification(mock_db, "user_test_999", "System Update", "New features", "SYSTEM", "SYSTEM")

    # Fetch with category=ALL
    resp_all = await client.get("/api/v1/notifications?category=ALL")
    assert resp_all.status_code == 200
    items_all = resp_all.json().get("items", [])
    assert len(items_all) == 5

    # Fetch without category param
    resp_default = await client.get("/api/v1/notifications")
    assert resp_default.status_code == 200
    assert len(resp_default.json().get("items", [])) == 5

    # Check unread count
    resp_count = await client.get("/api/v1/notifications/unread-count")
    assert resp_count.status_code == 200
    assert resp_count.json().get("unread_count") == 5


@pytest.mark.asyncio
async def test_mark_read_and_clear_operations(client, mock_db):
    """Verify marking read and soft-clearing works correctly without deleting records."""
    notif = await create_notification(
        mock_db, "user_test_999", "Alert", "Testing actions", "SYSTEM", "SYSTEM"
    )
    nid = str(notif.id)

    # 1. Mark as read
    resp_read = await client.post(f"/api/v1/notifications/{nid}/read")
    assert resp_read.status_code == 200
    updated_doc = await mock_db.notifications.find_one({"_id": notif.id})
    assert updated_doc["is_read"] is True

    # 2. Clear (soft-clear)
    resp_clear = await client.post(f"/api/v1/notifications/{nid}/clear")
    assert resp_clear.status_code == 200
    # Still exists in DB!
    db_doc = await mock_db.notifications.find_one({"_id": notif.id})
    assert db_doc is not None
    assert db_doc.get("is_cleared") is True

    # But does not appear in active notifications list
    resp_list = await client.get("/api/v1/notifications")
    assert resp_list.status_code == 200
    assert len(resp_list.json().get("items", [])) == 0


@pytest.mark.asyncio
async def test_legacy_category_migration(mock_db):
    """Verify non-destructive backfill updates legacy records with canonical categories."""
    await mock_db.notifications.insert_one({
        "_id": "leg_1",
        "user_id": "u1",
        "type": "PAYMENT",
        "title": "Payment Approved",
        "message": "Legacy payment",
    })
    await mock_db.notifications.insert_one({
        "_id": "leg_2",
        "user_id": "u1",
        "type": "TOURNAMENT",
        "title": "Tournament Registered",
        "message": "Legacy registration",
    })

    migrated = await migrate_notification_categories(mock_db)
    assert migrated == 2

    doc1 = await mock_db.notifications.find_one({"_id": "leg_1"})
    assert doc1["category"] == "WALLET"
    assert doc1["type"] == "WALLET"

    doc2 = await mock_db.notifications.find_one({"_id": "leg_2"})
    assert doc2["category"] == "REGISTRATION"


def test_resolve_notification_label_roles():
    """Verify administrator and support response roles resolve to 'Admin Response'."""
    # Administrative senders must all resolve to 'Admin Response'
    assert resolve_notification_label(sender_role="SUPER_ADMIN") == "Admin Response"
    assert resolve_notification_label(sender_role="ADMIN") == "Admin Response"
    assert resolve_notification_label(sender_role="ORGANIZER") == "Admin Response"
    assert resolve_notification_label(sender_role="SUPPORT_ADMIN") == "Admin Response"
    assert resolve_notification_label(sender_role="ARBITER") == "Admin Response"

    # User replies must resolve to 'User Reply', NEVER 'Admin Response'
    assert resolve_notification_label(sender_role="USER") == "User Reply"
    assert resolve_notification_label(notification_type="USER") == "User Reply"
    assert resolve_notification_label(title="User Reply") == "User Reply"

    # System notifications must resolve to 'System Notification'
    assert resolve_notification_label(sender_role="SYSTEM") == "System Notification"
    assert resolve_notification_label(notification_type="SYSTEM", category="SYSTEM") == "System Notification"

    # Domain events must retain their distinct category labels, not Admin Response
    assert resolve_notification_label(category="WALLET") == "Wallet"
    assert resolve_notification_label(category="TEAM") == "Team"
    assert resolve_notification_label(category="REGISTRATION") == "Registration"
    assert resolve_notification_label(category="BATCHES") == "Batches"
    assert resolve_notification_label(notification_type="WALLET") == "Wallet"
    assert resolve_notification_label(notification_type="TEAM") == "Team"

    # Ambiguous "Response" from administrative source resolves to Admin Response
    assert resolve_notification_label(title="Response") == "Admin Response"
    assert resolve_notification_label(title="Admin Response") == "Admin Response"
    assert resolve_notification_label(title="Support Response") == "Admin Response"


@pytest.mark.asyncio
async def test_admin_response_notification_creation(mock_db):
    """Verify creating a notification with an admin sender role assigns 'Admin Response' label and normalizes Response title."""
    notif = await create_notification(
        db=mock_db,
        user_id="u_contender",
        title="Response",  # ambiguous title
        message="Your ticket has received an update.",
        notification_type="SUPPORT",
        category="SYSTEM",
        sender_role="SUPER_ADMIN",
        sender_name="Lead Arbiter",
    )
    # Ambiguous 'Response' title was normalized to 'Admin Response'
    assert notif.title == "Admin Response"
    assert notif.label == "Admin Response"
    assert notif.sender_role == "SUPER_ADMIN"
    assert notif.sender_name == "Lead Arbiter"

    # Verify persisted document
    doc = await mock_db.notifications.find_one({"_id": notif.id})
    assert doc is not None
    assert doc["title"] == "Admin Response"
    assert doc["label"] == "Admin Response"
    assert doc["sender_role"] == "SUPER_ADMIN"


@pytest.mark.asyncio
async def test_user_and_system_notifications_not_admin_response(mock_db):
    """Verify user messages and system notifications are not mislabeled as Admin Response."""
    # User message
    user_notif = await create_notification(
        db=mock_db,
        user_id="u_admin",
        title="Inbound Message",
        message="I need help with my match",
        notification_type="USER",
        sender_role="USER",
    )
    assert user_notif.label == "User Reply"
    assert user_notif.label != "Admin Response"

    # System confirmation
    sys_notif = await create_notification(
        db=mock_db,
        user_id="u_player",
        title="Support Ticket Submitted [AMT-1001]",
        message="Your support ticket has been received.",
        notification_type="SYSTEM",
        sender_role="SYSTEM",
    )
    assert sys_notif.label == "System Notification"
    assert sys_notif.label != "Admin Response"


@pytest.mark.asyncio
async def test_notification_api_returns_admin_response_label(client, mock_db):
    """Verify the /notifications API endpoint serializes sender_role and label, and normalizes 'Response'."""
    # 1. Admin response notification with legacy ambiguous title 'Response'
    await mock_db.notifications.insert_one({
        "_id": "admin_resp_doc_1",
        "user_id": "user_test_999",
        "type": "SUPPORT",
        "category": "SYSTEM",
        "title": "Response",
        "message": "Your withdrawal has been verified.",
        "sender_role": "SUPER_ADMIN",
        "sender_name": "Admin Zero",
        "is_read": False,
        "is_cleared": False,
        "created_at": "2026-04-01T12:00:00Z",
    })

    # 2. Wallet notification
    await mock_db.notifications.insert_one({
        "_id": "wallet_doc_1",
        "user_id": "user_test_999",
        "type": "WALLET",
        "category": "WALLET",
        "title": "Wallet Topup Success",
        "message": "₹1000 added.",
        "is_read": False,
        "is_cleared": False,
        "created_at": "2026-04-01T11:00:00Z",
    })

    resp = await client.get("/api/v1/notifications")
    assert resp.status_code == 200
    data = resp.json()
    items = data["items"]
    assert len(items) == 2

    admin_item = next(i for i in items if i["id"] == "admin_resp_doc_1")
    assert admin_item["title"] == "Admin Response"
    assert admin_item["label"] == "Admin Response"
    assert admin_item["sender_role"] == "SUPER_ADMIN"
    assert admin_item["sender_name"] == "Admin Zero"

    wallet_item = next(i for i in items if i["id"] == "wallet_doc_1")
    assert wallet_item["label"] == "Wallet"
    assert wallet_item["title"] == "Wallet Topup Success"


@pytest.mark.asyncio
async def test_migration_backfills_admin_response_label(mock_db):
    """Verify migration assigns label and normalizes ambiguous 'Response' titles."""
    await mock_db.notifications.insert_one({
        "_id": "leg_resp_1",
        "user_id": "u1",
        "type": "SUPPORT",
        "title": "Response",
        "message": "We have received your appeal.",
        "sender_role": "ORGANIZER",
    })

    migrated = await migrate_notification_categories(mock_db)
    assert migrated >= 1

    doc = await mock_db.notifications.find_one({"_id": "leg_resp_1"})
    assert doc["title"] == "Admin Response"
    assert doc["label"] == "Admin Response"

