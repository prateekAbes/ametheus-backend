import pytest
from tests.conftest import make_auth_headers
from app.services.notification import create_notification

@pytest.mark.asyncio
async def test_notifications_workflow(client, db_session):
    user_id = "usr_vortex_lead"
    headers = make_auth_headers(user_id)

    # 1. Create 2 notifications
    await create_notification(
        db=db_session,
        user_id=user_id,
        title="Match Ready",
        message="Your match in Quarterfinals is live.",
        notification_type="MATCH",
        link="/matches",
    )
    await create_notification(
        db=db_session,
        user_id=user_id,
        title="Registration Confirmed",
        message="You are registered for Vanguard Cup.",
        notification_type="TOURNAMENT",
    )

    # 2. Fetch notifications
    res = await client.get("/api/v1/notifications", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["unread_count"] >= 2
    assert len(data["items"]) >= 2
    first_notif_id = data["items"][0]["id"]

    # 3. Mark single notification as read
    read_res = await client.post(f"/api/v1/notifications/{first_notif_id}/read", headers=headers)
    assert read_res.status_code == 200
    assert read_res.json()["is_read"] is True

    # 4. Mark all as read
    all_read_res = await client.post("/api/v1/notifications/read-all", headers=headers)
    assert all_read_res.status_code == 200

    # 5. Check unread count is 0
    res_final = await client.get("/api/v1/notifications", headers=headers)
    assert res_final.json()["unread_count"] == 0
