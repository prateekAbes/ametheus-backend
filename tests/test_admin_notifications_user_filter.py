import pytest
from httpx import AsyncClient, ASGITransport
from mongomock_motor import AsyncMongoMockClient

from app.main import app
from app.core.database import get_db
from app.api.deps import require_organizer
from app.models.user import User
from app.models.notification import Notification

@pytest.fixture
def mock_db():
    client = AsyncMongoMockClient()
    return client["test_admin_notifs_filter_db"]

@pytest.mark.asyncio
async def test_admin_notifications_user_filter(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db
    admin_user = User(
        id="admin_notif_01",
        email="admin@ametheus.gg",
        username="super_admin",
        role="SUPER_ADMIN",
    )
    app.dependency_overrides[require_organizer] = lambda: admin_user

    # Create two test users
    user_a = User(id="user_a_id", clerk_user_id="clerk_user_a", username="AlphaUser", email="alpha@example.com")
    user_b = User(id="user_b_id", clerk_user_id="clerk_user_b", username="BetaUser", email="beta@example.com")
    await mock_db.users.insert_one(user_a.to_mongo())
    await mock_db.users.insert_one(user_b.to_mongo())

    # Insert notifications
    notif_a = Notification(user_id="clerk_user_a", title="Alpha Notification", message="Msg for Alpha")
    notif_b = Notification(user_id="clerk_user_b", title="Beta Notification", message="Msg for Beta")
    await mock_db.notifications.insert_one(notif_a.to_mongo())
    await mock_db.notifications.insert_one(notif_b.to_mongo())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # 1. Unfiltered query returns both
        res_all = await ac.get("/api/v1/admin/notifications")
        assert res_all.status_code == 200
        all_data = res_all.json()
        assert all_data["total"] == 2

        # 2. Filter by user_a clerk_id
        res_a = await ac.get("/api/v1/admin/notifications?user_id=clerk_user_a")
        assert res_a.status_code == 200
        a_data = res_a.json()
        assert a_data["total"] == 1
        assert a_data["items"][0]["title"] == "Alpha Notification"

        # 3. Filter by user_a username
        res_a_user = await ac.get("/api/v1/admin/notifications?user_id=AlphaUser")
        assert res_a_user.status_code == 200
        a_user_data = res_a_user.json()
        assert a_user_data["total"] == 1
        assert a_user_data["items"][0]["title"] == "Alpha Notification"

        # 4. Filter by user_b clerk_id
        res_b = await ac.get("/api/v1/admin/notifications?user_id=clerk_user_b")
        assert res_b.status_code == 200
        b_data = res_b.json()
        assert b_data["total"] == 1
        assert b_data["items"][0]["title"] == "Beta Notification"

    app.dependency_overrides.clear()
