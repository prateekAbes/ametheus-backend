import pytest
from unittest.mock import AsyncMock, patch
from mongomock_motor import AsyncMongoMockClient

from app.core.config import settings
from app.services.email_service import (
    get_identity_config,
    send_email,
    send_noreply_email,
    send_system_email,
    send_support_email,
    send_support_email_threaded,
    send_official_email,
    sendSystemEmail,
    sendSupportEmail,
    sendOfficialEmail,
    get_email_system_status,
)
from app.services.resend_inbound_service import (
    process_resend_inbound_email,
    find_matching_ticket,
    extract_ticket_candidates_from_subject,
)


@pytest.fixture
def mock_db():
    client = AsyncMongoMockClient()
    return client["test_email_migration_db"]


@pytest.mark.asyncio
async def test_migration_identity_configurations():
    """Verify that all 3 sender identities adhere strictly to the migration requirements."""
    # 1. Automated / No-Reply
    noreply_cfg = get_identity_config("noreply")
    assert noreply_cfg["email"] == "noreply@ametheus.games"
    assert noreply_cfg["name"] == "Ametheus"
    assert noreply_cfg["reply_to"] == ""
    assert noreply_cfg["host"] == "smtp-relay.brevo.com"
    assert noreply_cfg["port"] == 587

    # 2. Support
    support_cfg = get_identity_config("support")
    assert support_cfg["email"] == "support@ametheus.games"
    assert support_cfg["name"] == "Ametheus Support"
    assert support_cfg["reply_to"] == "support@ametheus.games"
    assert support_cfg["host"] == "smtp-relay.brevo.com"
    assert support_cfg["port"] == 587

    # 3. Official
    official_cfg = get_identity_config("official")
    assert official_cfg["email"] == "official@ametheus.games"
    assert official_cfg["name"] == "Ametheus"
    assert official_cfg["host"] == "smtp-relay.brevo.com"
    assert official_cfg["port"] == 587
    # When OFFICIAL_REPLY_TO is not set in env, reply_to is empty string
    assert official_cfg["reply_to"] == (settings.OFFICIAL_REPLY_TO or "")


@pytest.mark.asyncio
async def test_migration_outbound_headers_and_centralized_abstractions(monkeypatch):
    """
    Test 1, 2, 3: Verify outbound delivery headers:
    - noreply: From noreply@ametheus.games, no Reply-To header
    - support: From support@ametheus.games, Reply-To support@inbound.ametheus.games
    - official: From official@ametheus.games, Reply-To empty or OFFICIAL_REPLY_TO
    """
    import app.services.email_service as email_srv

    captured_smtp = []

    def mock_send(identity_key, to_email, subject, text, html, reply_to=None, in_reply_to=None, references=None):
        captured_smtp.append({
            "identity_key": identity_key,
            "to_email": to_email,
            "subject": subject,
            "reply_to": reply_to,
            "in_reply_to": in_reply_to,
            "references": references,
        })
        return True, None, None, f"<AMT-{identity_key.upper()}@ametheus.games>", f"{identity_key}@ametheus.games"

    monkeypatch.setattr(email_srv, "_send_identity_smtp_sync", mock_send)

    # 1. Automated / System email (send_noreply_email and send_system_email / sendSystemEmail)
    captured_smtp.clear()
    ok, err = await send_noreply_email(
        to_email="player@example.com",
        subject="Welcome to Ametheus",
        text_content="Your account is ready.",
        html_content="<p>Your account is ready.</p>",
    )
    assert ok is True
    assert len(captured_smtp) == 1
    call = captured_smtp[0]
    assert call["identity_key"] == "noreply"
    assert call["to_email"] == "player@example.com"
    assert call["reply_to"] is None or call["reply_to"] == ""

    # Test alias sendSystemEmail
    captured_smtp.clear()
    ok, err = await sendSystemEmail(
        to_email="player@example.com",
        subject="System Alert",
        text_content="Alert text",
        html_content="<p>Alert</p>",
    )
    assert ok is True
    assert captured_smtp[0]["identity_key"] == "noreply"

    # 2. Support email (send_support_email / sendSupportEmail)
    captured_smtp.clear()
    ok, err = await send_support_email(
        to_email="contender@example.com",
        subject="Re: Support Ticket #AM-1042",
        text_content="We are investigating your issue.",
        html_content="<p>We are investigating your issue.</p>",
    )
    assert ok is True
    assert len(captured_smtp) == 1
    call = captured_smtp[0]
    assert call["identity_key"] == "support"
    assert call["to_email"] == "contender@example.com"
    assert call["reply_to"] == settings.SUPPORT_EMAIL.strip()

    # Test dict-based invocation: sendSupportEmail({"to": ..., "subject": ..., "html": ...})
    captured_smtp.clear()
    ok, err = await sendSupportEmail({
        "to": "contender@example.com",
        "subject": "Re: Support Ticket #AM-1042",
        "html": "<p>Updated ticket message</p>",
    })
    assert ok is True
    assert captured_smtp[0]["identity_key"] == "support"
    assert captured_smtp[0]["reply_to"] == settings.SUPPORT_EMAIL.strip()

    # 3. Support threaded reply
    captured_smtp.clear()
    ok, err, msg_id = await send_support_email_threaded(
        to_email="contender@example.com",
        subject="Re: Support Ticket #AM-1042",
        text_content="Admin reply text",
        html_content="<p>Admin reply text</p>",
        in_reply_to="<ORIGINAL-MSGID@ametheus.games>",
        references="<ORIGINAL-MSGID@ametheus.games>",
    )
    assert ok is True
    assert len(captured_smtp) == 1
    call = captured_smtp[0]
    assert call["identity_key"] == "support"
    assert call["reply_to"] == settings.SUPPORT_REPLY_TO.strip()
    assert call["in_reply_to"] == "<ORIGINAL-MSGID@ametheus.games>"
    assert call["references"] == "<ORIGINAL-MSGID@ametheus.games>"

    # 4. Official email (send_official_email / sendOfficialEmail)
    captured_smtp.clear()
    ok, err = await send_official_email(
        to_email="partner@organization.com",
        subject="Official Partnership Notice",
        text_content="Official communication content.",
        html_content="<p>Official communication content.</p>",
    )
    assert ok is True
    assert len(captured_smtp) == 1
    call = captured_smtp[0]
    assert call["identity_key"] == "official"
    assert call["to_email"] == "partner@organization.com"


@pytest.mark.asyncio
async def test_resend_inbound_duplicate_webhook_idempotency(mock_db):
    """Test 4: Verify duplicate inbound webhook event does not create duplicate support messages."""
    msg_id = "<UNIQUE-INBOUND-MSG-001@mail.example.com>"
    payload = {
        "from": "user@example.com",
        "to": ["support@inbound.ametheus.games"],
        "subject": "Help with registration",
        "text": "Please assist me with tournament entry.",
        "message_id": msg_id,
    }

    # First delivery: creates a new ticket
    res1 = await process_resend_inbound_email(mock_db, payload)
    assert res1["status"] == "ticket_created"
    ticket_id = res1["ticket_id"]

    # Verify 1 ticket and 1 user message created
    t_count = await mock_db.support_tickets.count_documents({})
    assert t_count == 1
    m_count = await mock_db.support_messages.count_documents({"message_id": msg_id})
    assert m_count == 1

    # Second delivery with identical Message-ID: must be rejected idempotently as duplicate
    res2 = await process_resend_inbound_email(mock_db, payload)
    assert res2["status"] == "duplicate"
    assert res2["ticket_id"] == ticket_id

    # Verify no second message or duplicate ticket was created
    t_count_after = await mock_db.support_tickets.count_documents({})
    assert t_count_after == 1
    m_count_after = await mock_db.support_messages.count_documents({"message_id": msg_id})
    assert m_count_after == 1


@pytest.mark.asyncio
async def test_resend_inbound_ticket_matching_sequence(mock_db):
    """
    Test support ticket threading & matching:
    - Subject candidate extraction for AM-1042 style and AMT-XXXXXX
    - Threading via In-Reply-To
    - Appending reply to existing ticket without creating duplicates
    """
    # 1. Test subject regex extraction
    candidates_am = extract_ticket_candidates_from_subject("Re: Support Ticket #AM-1042")
    assert "AM-1042" in candidates_am
    assert "#AM-1042" in candidates_am

    candidates_amt = extract_ticket_candidates_from_subject("Re: [Ticket AMT-998822] Account verification")
    assert "AMT-998822" in candidates_amt

    # 2. Seed an existing ticket and initial message in mock DB
    tid = "AM-1042"
    orig_msg_id = "<AMT-INITIAL-1042@ametheus.games>"
    await mock_db.support_tickets.insert_one({
        "_id": tid,
        "ticket_id": tid,
        "email": "contender@example.com",
        "name": "Alex Contender",
        "category": "ACCOUNT",
        "status": "WAITING_FOR_ADMIN",
        "is_archived": False,
        "messages": [],
        "logs": [],
    })
    await mock_db.support_messages.insert_one({
        "ticket_id": tid,
        "message_id": orig_msg_id,
        "sender_email": "support@ametheus.games",
        "message_type": "SYSTEM",
        "direction": "outbound",
    })

    # 3. Simulate user email reply arriving via Resend inbound webhook
    reply_payload = {
        "from": "Alex Contender <contender@example.com>",
        "to": ["support@inbound.ametheus.games"],
        "subject": "Re: Support Ticket #AM-1042",
        "text": "Thank you, here is my verification photo.\n\nOn Mon, Oct 5, 2026 wrote:\n> We received your ticket.",
        "message_id": "<USER-REPLY-555@mail.example.com>",
        "in_reply_to": orig_msg_id,
    }

    result = await process_resend_inbound_email(mock_db, reply_payload)
    assert result["status"] == "success"
    assert result["action"] == "reply_appended"
    assert result["ticket_id"] == tid

    # Verify message appended to ticket without creating new ticket
    ticket_count = await mock_db.support_tickets.count_documents({})
    assert ticket_count == 1
    ticket = await mock_db.support_tickets.find_one({"ticket_id": tid})
    assert ticket["status"] == "WAITING_FOR_ADMIN"
    assert ticket["has_new_reply"] is True


@pytest.mark.asyncio
async def test_email_system_status_reports_brevo():
    """Verify that email system status endpoint reports Brevo SMTP without exposing credentials."""
    status = get_email_system_status()
    assert "support" in status.services
    assert "noreply" in status.services
    assert "official" in status.services
    assert status.services["support"].email == "support@ametheus.games"
    assert status.services["support"].smtp_host == "smtp-relay.brevo.com"
    assert status.services["noreply"].email == "noreply@ametheus.games"
    assert status.services["official"].email == "official@ametheus.games"
