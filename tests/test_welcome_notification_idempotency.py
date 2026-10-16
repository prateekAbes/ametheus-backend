import pytest
import asyncio
from mongomock_motor import AsyncMongoMockClient
from app.services.notification import ensure_welcome_notification, WELCOME_NOTIFICATION_TITLE
from app.models.user import User

@pytest.fixture
def mock_db():
    client = AsyncMongoMockClient()
    return client["test_welcome_idempotency_db"]

@pytest.mark.asyncio
async def test_ensure_welcome_notification_concurrent_execution(mock_db, monkeypatch):
    """
    Simulates multiple concurrent API requests on user signup/login
    (e.g., auth sync, get me, fetch notifications, wallet check)
    and verifies that exactly ONE welcome notification and ONE email dispatch are created.
    """
    user_id = "user_concurrent_test_123"
    test_user = User(
        id=user_id,
        clerk_user_id=user_id,
        email="testplayer@example.com",
        username="TestPlayer",
        display_name="Test Player",
        role="PLAYER",
    )
    await mock_db.users.insert_one(test_user.to_mongo())

    email_calls = []
    async def mock_send_email(*args, **kwargs):
        email_calls.append(kwargs)
        return True, None

    monkeypatch.setattr("app.services.email_service.send_notification_email", mock_send_email)

    # Launch 5 concurrent calls to ensure_welcome_notification simultaneously
    results = await asyncio.gather(
        ensure_welcome_notification(db=mock_db, user_id=user_id),
        ensure_welcome_notification(db=mock_db, user_id=user_id),
        ensure_welcome_notification(db=mock_db, user_id=user_id),
        ensure_welcome_notification(db=mock_db, user_id=user_id),
        ensure_welcome_notification(db=mock_db, user_id=user_id),
    )

    # Allow any background tasks to finish
    await asyncio.sleep(0.1)

    # 1. Total notifications in DB must be EXACTLY 1
    notifs = await mock_db.notifications.find({"user_id": user_id}).to_list(10)
    assert len(notifs) == 1, f"Expected exactly 1 notification, found {len(notifs)}"
    assert notifs[0]["title"] == WELCOME_NOTIFICATION_TITLE

    # 2. Sent email records in DB must be EXACTLY 1
    email_records = await mock_db.sent_email_notifications.find({}).to_list(10)
    assert len(email_records) == 1, f"Expected exactly 1 email record, found {len(email_records)}"

    # 3. User document must have welcome_notification_sent = True
    user_doc = await mock_db.users.find_one({"_id": user_id})
    assert user_doc.get("welcome_notification_sent") is True

    # 4. Subsequent calls should return the existing notification without creating new ones
    subsequent_res = await ensure_welcome_notification(db=mock_db, user_id=user_id)
    assert subsequent_res is not None
    notifs_after = await mock_db.notifications.find({"user_id": user_id}).to_list(10)
    assert len(notifs_after) == 1
