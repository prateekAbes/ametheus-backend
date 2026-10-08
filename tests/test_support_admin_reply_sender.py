import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from mongomock_motor import AsyncMongoMockClient

from app.main import app
from app.core.config import settings
from app.core.database import get_db
from app.api.deps import get_current_user, get_current_user_optional, require_organizer
from app.models.user import User
from app.services.email_service import (
    EmailType,
    resolve_email_identity,
    get_identity_config,
    send_ticket_confirmation_email,
    send_admin_ticket_notification_email,
    send_support_email_threaded,
    send_notification_email,
    send_security_alert_email,
    send_match_roster_notification_email,
    send_official_announcement_to_recipient,
    send_test_email,
    send_email_by_type,
    _send_identity_smtp_sync,
)
import app.services.email_service as email_srv


@pytest.fixture
def mock_db():
    client = AsyncMongoMockClient()
    return client["test_support_admin_reply_db"]


@pytest_asyncio.fixture
async def client(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db

    admin_user = User(
        id="admin_arbiter_01",
        clerk_user_id="clerk_admin_arbiter",
        email="admin@ametheus.games",
        username="lead_arbiter",
        display_name="Lead Arbiter",
        role="SUPER_ADMIN",
    )
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[get_current_user_optional] = lambda: admin_user
    app.dependency_overrides[require_organizer] = lambda: admin_user

    await mock_db.users.insert_one(admin_user.to_mongo())

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
def captured_emails(monkeypatch):
    """
    Patches _send_identity_smtp_sync to capture all outbound dispatches,
    returning the identity used, recipient, headers, and resolved addresses.
    """
    captured = []

    def mock_send(
        identity_key,
        to_email,
        subject,
        text_content,
        html_content,
        reply_to=None,
        in_reply_to=None,
        references=None,
        *args,
        **kwargs,
    ):
        cfg = get_identity_config(identity_key)
        # Emulate the server's defensive override guard
        from_email = cfg["email"]
        eff_reply_to = reply_to or cfg.get("reply_to")
        
        # If defensive override guard in _send_identity_smtp_sync would trigger:
        email_type = kwargs.get("email_type") or (args[0] if len(args) > 0 else None)
        is_support_admin_reply = (
            (email_type and str(getattr(email_type, "value", email_type)).upper() == EmailType.SUPPORT_ADMIN_REPLY.value)
            or (identity_key.strip().lower() in ("support", "help") and (in_reply_to or references))
            or (reply_to and reply_to.strip().lower() == settings.SUPPORT_REPLY_TO.strip().lower() and identity_key.strip().lower() in ("support", "help"))
        )
        if is_support_admin_reply:
            from_email = settings.SUPPORT_EMAIL.strip()
            eff_reply_to = settings.SUPPORT_REPLY_TO.strip()
            identity_key = "support"

        record = {
            "identity_key": identity_key,
            "from_email": from_email,
            "to_email": to_email,
            "subject": subject,
            "text_content": text_content,
            "html_content": html_content,
            "reply_to": eff_reply_to,
            "in_reply_to": in_reply_to,
            "references": references,
            "email_type": email_type,
        }
        captured.append(record)
        return True, None, None, f"<AMT-{len(captured)}@ametheus.games>", from_email

    monkeypatch.setattr(email_srv, "_send_identity_smtp_sync", mock_send)
    return captured


# =========================================================================
# TEST A: SUPPORT TICKET CONFIRMATION
# =========================================================================

@pytest.mark.asyncio
async def test_a_support_ticket_confirmation(client, mock_db, captured_emails):
    """
    Test A: Support Ticket Confirmation
    - Create a ticket via user path (POST /api/v1/support/tickets)
    - Verify outbound confirmation email:
      - From: noreply@ametheus.games
      - Reply-To: support@ametheus.games
    """
    payload = {
        "name": "Jordan Player",
        "email": "jordan@example.com",
        "category": "PAYMENT",
        "description": "Payment debited but gems not credited.",
    }
    resp = await client.post("/api/v1/support/tickets", json=payload)
    assert resp.status_code == 201

    # Filter confirmation email sent to user
    user_confirmations = [
        c for c in captured_emails
        if c["to_email"] == "jordan@example.com"
    ]
    assert len(user_confirmations) == 1
    conf = user_confirmations[0]

    # Verification:
    assert conf["identity_key"] == "noreply"
    assert conf["from_email"] == "noreply@ametheus.games"
    assert conf["reply_to"] == "support@ametheus.games"
    assert "jordan@example.com" in conf["to_email"]


# =========================================================================
# TEST B: SUPPORT ADMIN REPLY (PRIMARY TEST)
# =========================================================================

@pytest.mark.asyncio
async def test_b_support_admin_reply_primary(client, mock_db, captured_emails):
    """
    Test B: Support Admin Reply (PRIMARY CRITICAL BUG TEST)
    - Administrator replies to user ticket from Admin Cockpit (POST /api/v1/support/tickets/{id}/reply)
    - Outbound email MUST strictly have:
      - From: support@ametheus.games
      - Reply-To: support@inbound.ametheus.games
    - Verify that From is NOT noreply@ametheus.games
    - Verify that From is NOT official@ametheus.games
    """
    # 1. Create existing ticket in DB
    ticket_id = "AMT-TICKET-B01"
    now_iso = "2026-10-09T00:00:00Z"
    await mock_db.support_tickets.insert_one({
        "_id": ticket_id,
        "ticket_id": ticket_id,
        "name": "Sarah Connor",
        "email": "sarah@cyberdyne.io",
        "category": "ACCOUNT",
        "description": "Cannot login to tournament lobby",
        "status": "OPEN",
        "created_at": now_iso,
        "updated_at": now_iso,
        "messages": [],
        "logs": [],
    })
    await mock_db.support_messages.insert_one({
        "ticket_id": ticket_id,
        "message_id": "<AMT-INIT-B01@ametheus.games>",
        "direction": "inbound",
        "sender_email": "sarah@cyberdyne.io",
        "created_at": now_iso,
    })

    captured_emails.clear()

    # 2. Admin submits reply via endpoint
    reply_payload = {
        "message": "We have resolved the lobby session conflict. Please try logging in again.",
        "status": "WAITING_FOR_USER",
    }
    resp = await client.post(f"/api/v1/support/tickets/{ticket_id}/reply", json=reply_payload)
    assert resp.status_code == 200

    # 3. Verify outbound email
    assert len(captured_emails) == 1
    reply = captured_emails[0]

    # PRIMARY ASSERTIONS:
    assert reply["identity_key"] == "support"
    assert reply["from_email"] == "support@ametheus.games"
    assert reply["reply_to"] == "support@inbound.ametheus.games"

    # STRICT EXCLUSION ASSERTIONS:
    assert reply["from_email"] != "noreply@ametheus.games"
    assert reply["from_email"] != "official@ametheus.games"
    assert reply["reply_to"] != "noreply@ametheus.games"
    assert reply["reply_to"] != "support@ametheus.games"


# =========================================================================
# TEST C: SECOND ADMIN REPLY (PERSISTENT HEADERS & THREADING)
# =========================================================================

@pytest.mark.asyncio
async def test_c_second_admin_reply_threading(client, mock_db, captured_emails):
    """
    Test C: Second Admin Reply
    - Submit a second admin reply to the ongoing conversation.
    - Verify headers remain identical:
      - From: support@ametheus.games
      - Reply-To: support@inbound.ametheus.games
    - Verify threading headers (In-Reply-To, References) are preserved.
    """
    ticket_id = "AMT-TICKET-C01"
    now_iso = "2026-10-09T00:00:00Z"
    await mock_db.support_tickets.insert_one({
        "_id": ticket_id,
        "ticket_id": ticket_id,
        "name": "Marcus Wright",
        "email": "marcus@resistance.org",
        "category": "HARDWARE",
        "description": "Display stuttering during tournament match",
        "status": "WAITING_FOR_USER",
        "created_at": now_iso,
        "updated_at": now_iso,
        "messages": [],
        "logs": [],
    })
    # Initial user message
    await mock_db.support_messages.insert_one({
        "ticket_id": ticket_id,
        "message_id": "<AMT-USER-C01@ametheus.games>",
        "direction": "inbound",
        "sender_email": "marcus@resistance.org",
        "created_at": now_iso,
    })

    captured_emails.clear()

    # First admin reply
    resp1 = await client.post(
        f"/api/v1/support/tickets/{ticket_id}/reply",
        json={"message": "Please disable background hardware acceleration.", "status": "WAITING_FOR_USER"},
    )
    assert resp1.status_code == 200
    assert len(captured_emails) == 1
    reply1 = captured_emails[0]
    assert reply1["from_email"] == "support@ametheus.games"
    assert reply1["reply_to"] == "support@inbound.ametheus.games"
    first_reply_msg_id = reply1["in_reply_to"]
    assert first_reply_msg_id == "<AMT-USER-C01@ametheus.games>"

    captured_emails.clear()

    # Second admin reply
    resp2 = await client.post(
        f"/api/v1/support/tickets/{ticket_id}/reply",
        json={"message": "Following up to confirm if disabling acceleration fixed the frame drops.", "status": "WAITING_FOR_USER"},
    )
    assert resp2.status_code == 200
    assert len(captured_emails) == 1
    reply2 = captured_emails[0]

    # Verify identical sender and reply-to on subsequent replies
    assert reply2["from_email"] == "support@ametheus.games"
    assert reply2["reply_to"] == "support@inbound.ametheus.games"
    assert reply2["from_email"] != "noreply@ametheus.games"

    # Threading headers must reference prior messages
    assert reply2["in_reply_to"] is not None
    assert "<AMT-USER-C01@ametheus.games>" in (reply2["references"] or "")


# =========================================================================
# TEST D: USER PLATFORM NOTIFICATIONS, SECURITY & MATCH ALERTS
# =========================================================================

@pytest.mark.asyncio
async def test_d_platform_notifications_and_alerts(captured_emails):
    """
    Test D: Platform Notification Email
    - Dispatch platform notification, security alert, and match roster notification.
    - Verify:
      - From: noreply@ametheus.games
      - Reply-To: support@ametheus.games
    """
    captured_emails.clear()

    # 1. Platform Notification
    ok1, err1 = await send_notification_email(
        recipient="player@ametheus.games",
        user_name="PlayerOne",
        title="Tournament Registration Confirmed",
        message="You are confirmed for the Ametheus Fall Major.",
    )
    assert ok1 is True

    # 2. Security Login Alert
    ok2, err2 = await send_security_alert_email(
        to_email="player@ametheus.games",
        user_name="PlayerOne",
        client_ip="198.51.100.42",
        device_info="Chrome on Linux",
        timestamp_str="2026-10-09 12:00:00 UTC",
    )
    assert ok2 is True

    # 3. Match Roster Schedule Notification
    ok3, err3 = await send_match_roster_notification_email(
        to_email="leader@teamalpha.gg",
        leader_name="Alpha Leader",
        tournament_title="Ametheus Championship",
        game_name="Counter-Strike 2",
        round_name="Quarterfinals",
        match_number=4,
        team_name="Team Alpha",
        opponent_name="Team Bravo",
        scheduled_date="2026-10-15",
        scheduled_time="18:00 UTC",
        match_id="MATCH-CS2-Q4",
    )
    assert ok3 is True

    assert len(captured_emails) == 3
    for call in captured_emails:
        assert call["identity_key"] == "noreply"
        assert call["from_email"] == "noreply@ametheus.games"
        assert call["reply_to"] == "support@ametheus.games"


# =========================================================================
# TEST E: OFFICIAL ANNOUNCEMENTS & ORGANIZER REPLIES
# =========================================================================

@pytest.mark.asyncio
async def test_e_official_announcements(captured_emails):
    """
    Test E: Official Announcement Email
    - Dispatch official announcement and organizer communication.
    - Verify:
      - From: official@ametheus.games
      - Reply-To: official@ametheus.games
    """
    captured_emails.clear()

    # 1. Official Platform Announcement
    ok1, err1 = await send_official_announcement_to_recipient(
        to_email="community@esports.org",
        subject="Important Platform Updates for Fall 2026",
        message="Here are the platform rule and schedule updates.",
    )
    assert ok1 is True
    assert len(captured_emails) == 1
    call1 = captured_emails[0]
    assert call1["identity_key"] == "official"
    assert call1["from_email"] == "official@ametheus.games"
    assert call1["reply_to"] == "official@ametheus.games"

    # 2. Organizer Communication via send_email_by_type
    captured_emails.clear()
    ok2, err2, msg2 = await send_email_by_type(
        email_type=EmailType.ORGANIZER_COMMUNICATION,
        recipient="organizer@apex-league.com",
        subject="Tournament Sanctioning Approved",
        text_content="Your tournament has been officially sanctioned by Ametheus.",
        html_content="<p>Your tournament has been officially sanctioned by Ametheus.</p>",
    )
    assert ok2 is True
    assert len(captured_emails) == 1
    call2 = captured_emails[0]
    assert call2["identity_key"] == "official"
    assert call2["from_email"] == "official@ametheus.games"
    assert call2["reply_to"] == "official@ametheus.games"


# =========================================================================
# TEST F: SMTP DIAGNOSTIC TEST (INDEPENDENT IDENTITY VERIFICATION)
# =========================================================================

@pytest.mark.asyncio
async def test_f_smtp_diagnostic_tester(captured_emails):
    """
    Test F: SMTP Diagnostic Test
    - Test support identity -> verifies support sender.
    - Test noreply identity -> verifies noreply sender.
    - Test official identity -> verifies official sender.
    - Verify diagnostic test does NOT alter production routing.
    """
    captured_emails.clear()

    # 1. Test Support diagnostic
    resp_supp = await send_test_email(to_email="dev@ametheus.games", service="support")
    assert resp_supp.success is True
    assert resp_supp.service == "support"
    assert resp_supp.sender_used == "support@ametheus.games"

    # 2. Test Noreply diagnostic
    resp_no = await send_test_email(to_email="dev@ametheus.games", service="noreply")
    assert resp_no.success is True
    assert resp_no.service == "noreply"
    assert resp_no.sender_used == "noreply@ametheus.games"

    # 3. Test Official diagnostic
    resp_off = await send_test_email(to_email="dev@ametheus.games", service="official")
    assert resp_off.success is True
    assert resp_off.service == "official"
    assert resp_off.sender_used == "official@ametheus.games"

    # Verify diagnostic calls did NOT mutate standard configs or production identity resolvers
    prod_supp = resolve_email_identity(EmailType.SUPPORT_ADMIN_REPLY)
    assert prod_supp["from_email"] == "support@ametheus.games"
    assert prod_supp["reply_to"] == "support@inbound.ametheus.games"

    prod_conf = resolve_email_identity(EmailType.SUPPORT_TICKET_CONFIRMATION)
    assert prod_conf["from_email"] == "noreply@ametheus.games"
    assert prod_conf["reply_to"] == "support@ametheus.games"


# =========================================================================
# TEST G: DEFENSIVE SENDER GUARD HARD-OVERRIDE
# =========================================================================

@pytest.mark.asyncio
async def test_g_defensive_guard_tampered_headers():
    """
    Test G: Defensive Guard Test
    - Call email dispatcher with corrupted/tampered headers attempting to send a
      Support Admin Reply with From: noreply@ametheus.games or spoofed Reply-To.
    - Verify the server hard-overrides it:
      - From: support@ametheus.games
      - Reply-To: support@inbound.ametheus.games
    """
    # 1. Attempt resolve_email_identity with SUPPORT_ADMIN_REPLY:
    # Guaranteed to return support@ametheus.games and support@inbound.ametheus.games
    identity_res = resolve_email_identity(EmailType.SUPPORT_ADMIN_REPLY)
    assert identity_res["from_email"] == "support@ametheus.games"
    assert identity_res["reply_to"] == "support@inbound.ametheus.games"
    assert identity_res["from_email"] != "noreply@ametheus.games"

    # 2. Directly invoke _send_identity_smtp_sync with spoofed identity_key="noreply" and corrupted reply_to
    # while specifying email_type=EmailType.SUPPORT_ADMIN_REPLY (with EMAIL_ENABLED=False mock)
    original_enabled = settings.EMAIL_ENABLED
    settings.EMAIL_ENABLED = False
    try:
        ok, err_code, err_det, msg_id, sender_used = _send_identity_smtp_sync(
            identity_key="noreply",  # Spoofed / wrong sender attempt!
            to_email="victim@user.com",
            subject="Re: Tampered Admin Reply",
            text_content="Body text",
            html_content="<p>Body text</p>",
            reply_to="noreply@ametheus.games",  # Spoofed / wrong reply-to attempt!
            email_type=EmailType.SUPPORT_ADMIN_REPLY,
        )
        assert ok is True
        # Hard-override MUST have forced sender to support@ametheus.games!
        assert sender_used == "support@ametheus.games"
        assert sender_used != "noreply@ametheus.games"

        # 3. Direct send_email_by_type with SUPPORT_ADMIN_REPLY
        ok_t, err_t, msg_t = await send_email_by_type(
            email_type=EmailType.SUPPORT_ADMIN_REPLY,
            recipient="user@domain.com",
            subject="Re: Ticket Inbound Thread",
            text_content="Admin text",
            html_content="<p>Admin text</p>",
        )
        assert ok_t is True
    finally:
        settings.EMAIL_ENABLED = original_enabled
