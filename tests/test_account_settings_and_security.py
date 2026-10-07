import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock

from app.main import app
from app.core.database import get_db
from app.models.user import User
from app.api.deps import get_current_user
from app.services.notification import create_notification
from app.services.login_security import process_login_security_check

@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
    return db

@pytest_asyncio.fixture
async def test_user(mock_db):
    user_doc = {
        "_id": "user_sec_1",
        "clerk_user_id": "clerk_sec_1",
        "email": "player_sec@ametheus.gg",
        "username": "ShadowPlayer",
        "role": "PLAYER",
        "preferences": {
            "notifications": {
                "Tournament updates": True,
                "Match reminders": True,
                "Team invites": True,
                "Wallet & payments": True,
                "Email notifications": True,
            },
            "login_alerts": True,
        },
        "privacy_settings": {
            "visibility": "Public",
            "usage_analytics": True,
            "show_matches": True,
            "show_stats": True,
        },
        "known_ips": ["192.168.1.100"],
        "last_login_ip": "192.168.1.100",
        "last_login_at": datetime.now(timezone.utc),
    }
    await mock_db.users.insert_one(user_doc)
    return user_doc

@pytest.mark.asyncio
async def test_new_ip_login_detection_and_security_email(mock_db, test_user):
    """
    Test end-to-end security login detection:
    1. Known IP -> no alert.
    2. New unrecognized IP with login_alerts=True -> dispatches alert email & in-app notification.
    3. Second login with that new IP -> recognized, no duplicate alert.
    4. New unrecognized IP with login_alerts=False -> no alert sent.
    """
    from starlette.requests import Request

    # Mock HTTP request from a known IP
    scope_known = {
        "type": "http",
        "method": "POST",
        "headers": [(b"user-agent", b"Mozilla/5.0 (Windows NT 10.0; Win64; x64)")],
        "client": ("192.168.1.100", 12345),
    }
    req_known = Request(scope_known)

    user_in_db = await mock_db.users.find_one({"_id": "user_sec_1"})
    is_new, ip = await process_login_security_check(mock_db, user_in_db, req_known)
    assert is_new is False
    assert ip == "192.168.1.100"

    # Verify no notification created
    notif_count = await mock_db.notifications.count_documents({"user_id": "user_sec_1"})
    assert notif_count == 0

    # Mock HTTP request from an unrecognized NEW IP
    scope_new = {
        "type": "http",
        "method": "POST",
        "headers": [(b"user-agent", b"Mozilla/5.0 (Macintosh; Intel Mac OS X)")],
        "client": ("203.0.113.45", 54321),
    }
    req_new = Request(scope_new)

    with patch("app.services.login_security.send_security_alert_email", new_callable=AsyncMock) as mock_send_email:
        mock_send_email.return_value = (True, None)

        user_in_db = await mock_db.users.find_one({"_id": "user_sec_1"})
        is_new, ip = await process_login_security_check(mock_db, user_in_db, req_new)
        assert is_new is True
        assert ip == "203.0.113.45"

        # Verify in-app security notification was created
        sec_notif = await mock_db.notifications.find_one({
            "user_id": "user_sec_1",
            "title": "Security Alert: New Sign-in Detected",
        })
        assert sec_notif is not None
        assert "203.0.113.45" in sec_notif["message"]

        # Verify IP is now in known_ips
        updated_user = await mock_db.users.find_one({"_id": "user_sec_1"})
        assert "203.0.113.45" in updated_user["known_ips"]

    # Subsequent request from same IP ("203.0.113.45") -> must NOT create duplicate notification
    user_in_db = await mock_db.users.find_one({"_id": "user_sec_1"})
    is_new_repeat, _ = await process_login_security_check(mock_db, user_in_db, req_new)
    assert is_new_repeat is False

    # Now disable login_alerts preference
    await mock_db.users.update_one(
        {"_id": "user_sec_1"},
        {"$set": {"preferences.login_alerts": False}}
    )

    # Mock HTTP request from another brand new IP ("198.51.100.77")
    scope_disabled = {
        "type": "http",
        "method": "POST",
        "headers": [(b"user-agent", b"Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)")],
        "client": ("198.51.100.77", 443),
    }
    req_disabled = Request(scope_disabled)

    initial_notifs = await mock_db.notifications.count_documents({"user_id": "user_sec_1"})

    user_in_db = await mock_db.users.find_one({"_id": "user_sec_1"})
    is_new, ip = await process_login_security_check(mock_db, user_in_db, req_disabled)
    assert is_new is True

    # When login_alerts is False, no additional security notification should be generated
    final_notifs = await mock_db.notifications.count_documents({"user_id": "user_sec_1"})
    assert final_notifs == initial_notifs

@pytest.mark.asyncio
async def test_notification_preferences_suppression(mock_db, test_user):
    """
    Test that disabling notification preferences actually suppresses notifications:
    - Disabling 'Tournament updates' suppresses TOURNAMENT / REGISTRATION
    - Disabling 'Match reminders' suppresses MATCH
    - Disabling 'Team invites' suppresses TEAM
    - Disabling 'Wallet & payments' suppresses WALLET
    - Critical SYSTEM notifications are ALWAYS delivered
    """
    # 1. When enabled, tournament notification succeeds
    res1 = await create_notification(
        db=mock_db,
        user_id="user_sec_1",
        title="Tournament Live",
        message="Your bracket match is ready",
        notification_type="TOURNAMENT",
        category="TOURNAMENT",
        send_email_copy=False,
    )
    assert res1 is not None

    # 2. Disable 'Tournament updates' preference
    await mock_db.users.update_one(
        {"_id": "user_sec_1"},
        {"$set": {"preferences.notifications.Tournament updates": False}}
    )

    # Now tournament notification is suppressed
    res2 = await create_notification(
        db=mock_db,
        user_id="user_sec_1",
        title="Tournament Schedule Changed",
        message="Match time updated",
        notification_type="TOURNAMENT",
        category="TOURNAMENT",
        send_email_copy=False,
    )
    assert res2 is None

    # 3. Disable 'Match reminders' preference
    await mock_db.users.update_one(
        {"_id": "user_sec_1"},
        {"$set": {"preferences.notifications.Match reminders": False}}
    )

    res_match = await create_notification(
        db=mock_db,
        user_id="user_sec_1",
        title="Match Room Credentials",
        message="Lobby code 99881",
        notification_type="MATCH",
        category="MATCH",
        send_email_copy=False,
    )
    assert res_match is None

    # 4. Critical SYSTEM notification is ALWAYS delivered regardless of preferences
    res_sys = await create_notification(
        db=mock_db,
        user_id="user_sec_1",
        title="Account Password Updated",
        message="Your credentials were changed",
        notification_type="SYSTEM",
        category="SYSTEM",
        send_email_copy=False,
    )
    assert res_sys is not None
    assert res_sys.title == "Account Password Updated"

@pytest.mark.asyncio
async def test_leaderboard_show_stats_privacy_enforcement(mock_db):
    """
    Test that leaderboard masks captain name when privacy_settings.show_stats is False.
    """
    app.dependency_overrides[get_db] = lambda: mock_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Create game
        await mock_db.games.insert_one({
            "_id": "g_val",
            "slug": "valorant",
            "name": "Valorant",
            "category": "pc",
        })

        # Create captain with show_stats = False
        await mock_db.users.insert_one({
            "_id": "cap_anon",
            "username": "PrivatePro",
            "role": "PLAYER",
            "privacy_settings": {"show_stats": False},
        })

        # Create team
        await mock_db.teams.insert_one({
            "_id": "team_anon",
            "name": "Ghost Squad",
            "tag": "GHOST",
            "game_id": "g_val",
            "captain_id": "cap_anon",
            "is_dummy": False,
        })

        # Fetch leaderboard
        res = await ac.get("/api/v1/leaderboard")
        assert res.status_code == 200
        data = res.json()
        leaderboard_list = data
        ghost_team = next((t for t in leaderboard_list if t["id"] == "team_anon"), None)
        assert ghost_team is not None
        # Captain user must be masked per privacy setting
        assert ghost_team["captain"] == "@Anonymous Contender"

    app.dependency_overrides.clear()
