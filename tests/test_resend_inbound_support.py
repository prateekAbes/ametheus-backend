import pytest
import pytest_asyncio
from mongomock_motor import AsyncMongoMockClient
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.database import get_db
from app.core.config import settings
from app.models.user import User
from app.api.deps import get_current_user, get_current_user_optional, require_organizer
from app.services.resend_inbound_service import (
    extract_reply_body,
    sanitize_html,
    process_resend_inbound_email,
)


@pytest_asyncio.fixture
async def mock_db():
    client = AsyncMongoMockClient()
    db = client["ametheus_test"]
    return db


@pytest_asyncio.fixture
async def client(mock_db):
    app.dependency_overrides[get_db] = lambda: mock_db

    admin_user = User(
        id="admin_123",
        clerk_user_id="user_admin_clerk",
        email="admin@ametheus.games",
        username="lead_admin",
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


@pytest.mark.asyncio
async def test_ticket_creation_with_user_and_system_messages(client, mock_db):
    """
    Test 1: When a user creates a support ticket:
    - Initial USER message is created in db.support_messages
    - SYSTEM confirmation receipt is created in db.support_messages
    - Initial status in DB is WAITING_FOR_ADMIN
    """
    create_payload = {
        "name": "Sarah Connor",
        "email": "sarah@cyberdyne.io",
        "category": "PAYMENT",
        "description": "My payment for the tournament failed at checkout.",
    }
    resp = await client.post("/api/v1/support/tickets", json=create_payload)
    assert resp.status_code == 201
    data = resp.json()
    ticket_id = data["ticket_id"]
    assert ticket_id.startswith("AMT-")

    # Verify ticket in DB
    ticket_doc = await mock_db.support_tickets.find_one({"ticket_id": ticket_id})
    assert ticket_doc is not None
    assert ticket_doc["status"] == "WAITING_FOR_ADMIN"
    assert ticket_doc["has_new_reply"] is True

    # Verify messages in db.support_messages
    msgs = await mock_db.support_messages.find({"ticket_id": ticket_id}).sort("created_at", 1).to_list(10)
    assert len(msgs) >= 2

    # First is USER
    user_msg = msgs[0]
    assert user_msg["message_type"] == "USER"
    assert user_msg["sender_role"] == "USER"
    assert user_msg["sender_email"] == "sarah@cyberdyne.io"
    assert "payment for the tournament" in user_msg["message"]

    # Second is SYSTEM
    system_msg = msgs[1]
    assert system_msg["message_type"] == "SYSTEM"
    assert system_msg["sender_role"] == "SYSTEM"
    assert system_msg["recipient_email"] == "sarah@cyberdyne.io"
    assert system_msg["message_id"] is not None


@pytest.mark.asyncio
async def test_user_replies_before_admin_response_status_remains_waiting_for_admin(mock_db):
    """
    Test 2: If user replies to confirmation receipt before an admin has ever responded,
    status remains WAITING_FOR_ADMIN (does NOT change to WAITING_FOR_USER).
    """
    # Create ticket manually in DB
    ticket_id = "AMT-TST001"
    now = "2026-10-06T10:00:00Z"
    await mock_db.support_tickets.insert_one({
        "_id": ticket_id,
        "ticket_id": ticket_id,
        "name": "John Doe",
        "email": "john@example.com",
        "category": "ACCOUNT",
        "description": "Forgot my credentials",
        "status": "WAITING_FOR_ADMIN",
        "has_new_reply": True,
        "messages": [],
        "created_at": now,
        "updated_at": now,
    })

    # Inbound email reply from user before admin has responded
    inbound_data = {
        "from": "John Doe <john@example.com>",
        "to": "support@ametheus.games",
        "subject": f"Re: Ametheus Support Ticket Created — {ticket_id}",
        "text": "Also I wanted to add that my phone number is 9876543210.",
        "message_id": "<user-reply-1@mail.example.com>",
    }

    result = await process_resend_inbound_email(mock_db, inbound_data)
    assert result["status"] == "success"
    assert result["ticket_id"] == ticket_id
    assert result["ticket_status"] == "WAITING_FOR_ADMIN"

    # Verify ticket status in DB
    ticket_doc = await mock_db.support_tickets.find_one({"ticket_id": ticket_id})
    assert ticket_doc["status"] == "WAITING_FOR_ADMIN"
    assert ticket_doc["has_new_reply"] is True


@pytest.mark.asyncio
async def test_admin_first_reply_transitions_to_waiting_for_user(client, mock_db):
    """
    Test 3: When admin replies to a ticket:
    - Threading headers (In-Reply-To, References) are set
    - ADMIN message is saved in db.support_messages
    - Status transitions to WAITING_FOR_USER
    - has_new_reply is reset to False
    """
    ticket_id = "AMT-TST002"
    now = "2026-10-06T10:00:00Z"

    # Pre-populate initial user message and system confirmation
    user_msg_id = "<initial-user-msg-002@mail.example.com>"
    await mock_db.support_messages.insert_one({
        "id": "msg_001",
        "ticket_id": ticket_id,
        "message_id": user_msg_id,
        "message_type": "USER",
        "sender_role": "USER",
        "sender_email": "jane@example.com",
        "message": "Need help with team invite",
        "created_at": now,
    })

    await mock_db.support_tickets.insert_one({
        "_id": ticket_id,
        "ticket_id": ticket_id,
        "name": "Jane",
        "email": "jane@example.com",
        "category": "TEAM",
        "description": "Need help with team invite",
        "status": "WAITING_FOR_ADMIN",
        "has_new_reply": True,
        "messages": [],
        "created_at": now,
        "updated_at": now,
    })

    # Admin sends reply via endpoint
    reply_resp = await client.post(
        f"/api/v1/support/tickets/{ticket_id}/reply",
        json={"message": "Hello Jane, we have sent the invite code to your registered team leader."},
    )
    assert reply_resp.status_code == 200
    ticket_data = reply_resp.json()
    assert ticket_data["status"] == "WAITING_FOR_USER"
    assert ticket_data["has_new_reply"] is False

    # Check ADMIN message in db.support_messages
    admin_msg = await mock_db.support_messages.find_one({
        "ticket_id": ticket_id,
        "message_type": "ADMIN",
    })
    assert admin_msg is not None
    assert admin_msg["sender_role"] == "ADMIN"
    assert admin_msg["in_reply_to"] == user_msg_id
    assert user_msg_id in admin_msg["references"]


@pytest.mark.asyncio
async def test_user_replies_after_admin_reply_transitions_to_waiting_for_admin(mock_db):
    """
    Test 4: When user replies after an admin has responded:
    - Status transitions from WAITING_FOR_USER to WAITING_FOR_ADMIN
    - has_new_reply is set to True
    """
    ticket_id = "AMT-TST003"
    now = "2026-10-06T10:00:00Z"
    admin_msg_id = "<admin-reply-003@ametheus.games>"

    await mock_db.support_messages.insert_one({
        "id": "msg_admin_003",
        "ticket_id": ticket_id,
        "message_id": admin_msg_id,
        "message_type": "ADMIN",
        "sender_role": "ADMIN",
        "direction": "outbound",
        "message": "We have reviewed your request.",
        "created_at": now,
    })

    await mock_db.support_tickets.insert_one({
        "_id": ticket_id,
        "ticket_id": ticket_id,
        "name": "Alex",
        "email": "alex@example.com",
        "category": "WALLET",
        "description": "Wallet balance check",
        "status": "WAITING_FOR_USER",
        "has_new_reply": False,
        "created_at": now,
        "updated_at": now,
    })

    # User replies to admin response
    inbound_data = {
        "from": "Alex <alex@example.com>",
        "to": "support@ametheus.games",
        "subject": f"Re: [Ticket {ticket_id}] WALLET — Ametheus Support",
        "text": "Thank you, that solved my issue!",
        "message_id": "<alex-reply-003@mail.example.com>",
        "in_reply_to": admin_msg_id,
    }

    result = await process_resend_inbound_email(mock_db, inbound_data)
    assert result["status"] == "success"
    assert result["ticket_status"] == "WAITING_FOR_ADMIN"

    # Verify DB
    ticket_doc = await mock_db.support_tickets.find_one({"ticket_id": ticket_id})
    assert ticket_doc["status"] == "WAITING_FOR_ADMIN"
    assert ticket_doc["has_new_reply"] is True


@pytest.mark.asyncio
async def test_multi_turn_reply_chain_in_single_thread(client, mock_db):
    """
    Test 5 & 6: Multi-turn reply chain:
    USER -> ADMIN -> USER -> ADMIN -> all in single conversation thread.
    """
    ticket_id = "AMT-CHAIN01"
    now = "2026-10-06T11:00:00Z"

    # Initial state
    await mock_db.support_tickets.insert_one({
        "_id": ticket_id,
        "ticket_id": ticket_id,
        "name": "Dave",
        "email": "dave@example.com",
        "category": "OTHER",
        "description": "Initial question",
        "status": "WAITING_FOR_ADMIN",
        "has_new_reply": True,
        "created_at": now,
        "updated_at": now,
    })
    init_msg_id = "<dave-msg-1@mail.example.com>"
    await mock_db.support_messages.insert_one({
        "id": "msg_dave_1",
        "ticket_id": ticket_id,
        "message_id": init_msg_id,
        "message_type": "USER",
        "sender_role": "USER",
        "message": "Initial question",
        "created_at": now,
    })

    # Turn 1: Admin replies
    resp1 = await client.post(
        f"/api/v1/support/tickets/{ticket_id}/reply",
        json={"message": "Admin answer 1"},
    )
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "WAITING_FOR_USER"

    admin_msg1 = await mock_db.support_messages.find_one({"ticket_id": ticket_id, "message": "Admin answer 1"})
    assert admin_msg1 is not None
    admin_msg1_id = admin_msg1["message_id"]

    # Turn 2: User replies
    inbound_dave2 = {
        "from": "Dave <dave@example.com>",
        "to": "support@ametheus.games",
        "subject": f"Re: [Ticket {ticket_id}] OTHER — Ametheus Support",
        "text": "User follow up 2",
        "message_id": "<dave-msg-2@mail.example.com>",
        "in_reply_to": admin_msg1_id,
    }
    res2 = await process_resend_inbound_email(mock_db, inbound_dave2)
    assert res2["ticket_status"] == "WAITING_FOR_ADMIN"

    # Turn 3: Admin replies again
    resp3 = await client.post(
        f"/api/v1/support/tickets/{ticket_id}/reply",
        json={"message": "Admin resolution 3", "status": "RESOLVED"},
    )
    assert resp3.status_code == 200
    assert resp3.json()["status"] == "RESOLVED"

    # Verify conversation thread details via GET endpoint
    details_resp = await client.get(f"/api/v1/support/tickets/{ticket_id}")
    assert details_resp.status_code == 200
    details = details_resp.json()
    assert len(details["messages"]) == 4  # Initial user + Admin 1 + User 2 + Admin 3
    assert details["messages"][0]["message_type"] == "USER"
    assert details["messages"][1]["message_type"] == "ADMIN"
    assert details["messages"][2]["message_type"] == "USER"
    assert details["messages"][3]["message_type"] == "ADMIN"


@pytest.mark.asyncio
async def test_duplicate_webhook_idempotency(mock_db):
    """
    Test 7: Duplicate inbound webhook with same Message-ID is ignored.
    """
    ticket_id = "AMT-DUP001"
    now = "2026-10-06T12:00:00Z"
    dup_msg_id = "<duplicate-unique-id-999@mail.example.com>"

    await mock_db.support_tickets.insert_one({
        "_id": ticket_id,
        "ticket_id": ticket_id,
        "name": "Eve",
        "email": "eve@example.com",
        "category": "OTHER",
        "status": "WAITING_FOR_ADMIN",
        "created_at": now,
        "updated_at": now,
    })

    inbound_payload = {
        "from": "Eve <eve@example.com>",
        "to": "support@ametheus.games",
        "subject": f"Inquiry [{ticket_id}]",
        "text": "Hello support",
        "message_id": dup_msg_id,
    }

    # First delivery
    res1 = await process_resend_inbound_email(mock_db, inbound_payload)
    assert res1["status"] == "success"

    # Second delivery with identical Message-ID
    res2 = await process_resend_inbound_email(mock_db, inbound_payload)
    assert res2["status"] == "duplicate"

    # Ensure only 1 message exists in DB
    count = await mock_db.support_messages.count_documents({"message_id": dup_msg_id})
    assert count == 1


@pytest.mark.asyncio
async def test_subject_ticket_id_mismatched_sender_isolation(mock_db):
    """
    Test 8: If an email subject contains a ticket ID belonging to someone else,
    it must NOT attach to that ticket. Instead, a new ticket should be created or fallback.
    """
    victim_ticket_id = "AMT-VICTIM1"
    now = "2026-10-06T12:00:00Z"

    await mock_db.support_tickets.insert_one({
        "_id": victim_ticket_id,
        "ticket_id": victim_ticket_id,
        "name": "Victim User",
        "email": "victim@example.com",
        "category": "PAYMENT",
        "status": "WAITING_FOR_ADMIN",
        "created_at": now,
        "updated_at": now,
    })

    # Attacker tries to hijack victim's ticket by spoofing the subject line
    attacker_payload = {
        "from": "Attacker <attacker@evil.org>",
        "to": "support@ametheus.games",
        "subject": f"Re: [Ticket {victim_ticket_id}] PAYMENT",
        "text": "Give me their account details",
        "message_id": "<attacker-msg-1@evil.org>",
    }

    res = await process_resend_inbound_email(mock_db, attacker_payload)
    # The message must NOT have attached to victim_ticket_id
    assert res["ticket_id"] != victim_ticket_id
    assert res["status"] == "ticket_created"

    # Victim's ticket must have 0 messages from attacker
    victim_msgs = await mock_db.support_messages.find({"ticket_id": victim_ticket_id}).to_list(10)
    assert len(victim_msgs) == 0


@pytest.mark.asyncio
async def test_body_quote_stripping_and_html_sanitization():
    """
    Test 9: Test email reply history stripping and XSS HTML sanitization.
    """
    quoted_text = """Thank you for your help, it worked!

On Mon, Oct 6, 2026 at 2:30 PM Ametheus Support <support@ametheus.games> wrote:
> Hello Contender,
> Please check your dashboard.
"""
    cleaned = extract_reply_body(quoted_text)
    assert cleaned == "Thank you for your help, it worked!"
    assert "On Mon, Oct 6" not in cleaned
    assert "> Hello Contender" not in cleaned

    dirty_html = """
    <div>
        <p>Hello Support!</p>
        <script>alert('xss');</script>
        <iframe src="http://malicious.com"></iframe>
        <a href="javascript:steal()">Click me</a>
        <img src="pic.jpg" onload="alert(1)" />
    </div>
    """
    clean_html = sanitize_html(dirty_html)
    assert "<script>" not in clean_html
    assert "alert('xss')" not in clean_html
    assert "<iframe>" not in clean_html
    assert "javascript:" not in clean_html
    assert "onload=" not in clean_html
    assert "Hello Support!" in clean_html


@pytest.mark.asyncio
async def test_inbound_email_with_attachments(mock_db):
    """
    Test 10: Inbound email attachments are extracted and persisted in SupportMessage.
    """
    ticket_id = "AMT-ATT001"
    now = "2026-10-06T13:00:00Z"

    await mock_db.support_tickets.insert_one({
        "_id": ticket_id,
        "ticket_id": ticket_id,
        "name": "Gamer",
        "email": "gamer@example.com",
        "category": "GAME_ISSUE",
        "status": "WAITING_FOR_ADMIN",
        "created_at": now,
        "updated_at": now,
    })

    inbound_payload = {
        "from": "Gamer <gamer@example.com>",
        "to": "support@ametheus.games",
        "subject": f"Bug report [{ticket_id}]",
        "text": "Here is the screenshot of the glitch.",
        "message_id": "<gamer-att-msg@mail.example.com>",
        "attachments": [
            {
                "id": "att_123",
                "filename": "screenshot.png",
                "content_type": "image/png",
                "size": 102400,
                "download_url": "https://resend.com/files/screenshot.png",
            }
        ],
    }

    res = await process_resend_inbound_email(mock_db, inbound_payload)
    assert res["status"] == "success"

    msg = await mock_db.support_messages.find_one({"ticket_id": ticket_id})
    assert msg is not None
    assert len(msg["attachments"]) == 1
    att = msg["attachments"][0]
    assert att["filename"] == "screenshot.png"
    assert att["content_type"] == "image/png"
    assert att["size"] == 102400
    assert att["url"] == "https://resend.com/files/screenshot.png"


@pytest.mark.asyncio
async def test_outbound_support_emails_have_correct_from_and_reply_to_headers(monkeypatch):
    """
    Test 11: Verify that all outbound support emails sent via Brevo SMTP configure:
    From: support@ametheus.games
    Reply-To: support@inbound.ametheus.games
    while preserving threading headers.
    """
    from app.services.email_service import (
        get_identity_config,
        send_support_email_threaded,
        send_ticket_confirmation_email,
    )
    import app.services.email_service as email_srv

    # 1. Verify identity config for support identity
    support_cfg = get_identity_config("support")
    assert support_cfg["email"] == settings.SUPPORT_EMAIL.strip()
    assert support_cfg["reply_to"] == settings.SUPPORT_EMAIL.strip()

    captured_calls = []

    def mock_send_identity_smtp_sync(identity_key, to_email, subject, text_content, html_content, reply_to=None, in_reply_to=None, references=None):
        captured_calls.append({
            "identity_key": identity_key,
            "to_email": to_email,
            "subject": subject,
            "reply_to": reply_to,
            "in_reply_to": in_reply_to,
            "references": references,
        })
        return True, None, None, "<AMT-TEST-MSGID@ametheus.games>", "support@ametheus.games"

    monkeypatch.setattr(email_srv, "_send_identity_smtp_sync", mock_send_identity_smtp_sync)

    # 2. Test automated ticket confirmation email
    ticket = {
        "id": "AMT-CONF01",
        "ticket_id": "AMT-CONF01",
        "name": "Alex",
        "email": "alex@example.com",
        "category": "PAYMENT",
        "description": "Test payment issue",
    }
    ok, err, msg_id = await send_ticket_confirmation_email(ticket)
    assert len(captured_calls) == 1
    conf_call = captured_calls[0]
    # Automated ticket confirmation uses support identity (From: support@ametheus.games)
    assert conf_call["identity_key"] == "support"
    assert conf_call["to_email"] == "alex@example.com"
    # Reply-To must be support@ametheus.games
    assert conf_call["reply_to"] == settings.SUPPORT_EMAIL.strip()

    # 3. Test admin support reply email with threading
    captured_calls.clear()
    ok, err, msg_id = await send_support_email_threaded(
        to_email="alex@example.com",
        subject="Re: [Ticket AMT-CONF01] PAYMENT — Ametheus Support",
        text_content="Here is the admin answer",
        html_content="<p>Here is the admin answer</p>",
        in_reply_to="<AMT-CONF01-USER@mail.com>",
        references="<AMT-CONF01-USER@mail.com>",
    )
    assert ok is True
    assert len(captured_calls) == 1
    reply_call = captured_calls[0]
    assert reply_call["identity_key"] == "support"
    assert reply_call["to_email"] == "alex@example.com"
    # Reply-To must be support@ametheus.games
    assert reply_call["reply_to"] == settings.SUPPORT_EMAIL.strip()
    # Threading headers preserved
    assert reply_call["in_reply_to"] == "<AMT-CONF01-USER@mail.com>"
    assert reply_call["references"] == "<AMT-CONF01-USER@mail.com>"


@pytest.mark.asyncio
async def test_user_replies_to_inbound_email_address_routes_to_existing_ticket(client, mock_db):
    """
    Test 12: End-to-end Reply-To routing verification:
    1. Ticket created -> confirmation sent with Reply-To: support@inbound.ametheus.games
    2. User hits reply in email client -> email addressed to support@inbound.ametheus.games
    3. Resend Inbound webhook receives email -> matches existing ticket, status remains WAITING_FOR_ADMIN
    4. Admin sends first reply -> status becomes WAITING_FOR_USER
    5. User replies again to support@inbound.ametheus.games -> status becomes WAITING_FOR_ADMIN
    """
    ticket_id = "AMT-INB777"
    user_email = "challenger@example.com"
    now = "2026-10-06T14:00:00Z"
    confirm_msg_id = "<AMT-CONF-AMT-INB777@ametheus.games>"

    # Create initial ticket and confirmation message
    await mock_db.support_tickets.insert_one({
        "_id": ticket_id,
        "ticket_id": ticket_id,
        "name": "Challenger",
        "email": user_email,
        "category": "TOURNAMENT",
        "description": "Cannot see my match schedule",
        "status": "WAITING_FOR_ADMIN",
        "has_new_reply": True,
        "messages": [],
        "created_at": now,
        "updated_at": now,
    })
    await mock_db.support_messages.insert_one({
        "id": "msg_sys_confirm",
        "ticket_id": ticket_id,
        "message_id": confirm_msg_id,
        "message_type": "SYSTEM",
        "sender_role": "SYSTEM",
        "recipient_email": user_email,
        "message": "Automated confirmation receipt dispatched",
        "created_at": now,
    })

    # Step 2 & 3: User replies to confirmation email (destination: support@inbound.ametheus.games)
    user_reply_1 = {
        "from": f"Challenger <{user_email}>",
        "to": "support@inbound.ametheus.games",  # Routed via Reply-To!
        "subject": f"Re: Ametheus Support Ticket Created — {ticket_id}",
        "text": "Please hurry, tournament begins in 1 hour!",
        "message_id": "<challenger-msg-1@inbound.mail.com>",
        "in_reply_to": confirm_msg_id,
    }

    res1 = await process_resend_inbound_email(mock_db, user_reply_1)
    assert res1["status"] == "success"
    assert res1["ticket_id"] == ticket_id
    # Must NOT create a new ticket, status remains WAITING_FOR_ADMIN because admin hasn't replied yet
    assert res1["ticket_status"] == "WAITING_FOR_ADMIN"

    t_doc = await mock_db.support_tickets.find_one({"ticket_id": ticket_id})
    assert t_doc["status"] == "WAITING_FOR_ADMIN"

    # Step 4: Admin sends first genuine response via admin endpoint
    reply_resp = await client.post(
        f"/api/v1/support/tickets/{ticket_id}/reply",
        json={"message": "We have checked your bracket. Match 14 is scheduled for 4 PM."},
    )
    assert reply_resp.status_code == 200
    assert reply_resp.json()["status"] == "WAITING_FOR_USER"

    admin_msg = await mock_db.support_messages.find_one({
        "ticket_id": ticket_id,
        "message_type": "ADMIN",
    })
    assert admin_msg is not None
    admin_msg_id = admin_msg["message_id"]

    # Step 5: User replies to admin response (destination: support@inbound.ametheus.games)
    user_reply_2 = {
        "from": f"Challenger <{user_email}>",
        "to": ["support@inbound.ametheus.games"],  # Routed via Reply-To!
        "subject": f"Re: [Ticket {ticket_id}] TOURNAMENT — Ametheus Support",
        "text": "Got it, thank you for the quick help!",
        "message_id": "<challenger-msg-2@inbound.mail.com>",
        "in_reply_to": admin_msg_id,
    }

    res2 = await process_resend_inbound_email(mock_db, user_reply_2)
    assert res2["status"] == "success"
    assert res2["ticket_id"] == ticket_id
    # Now that admin had responded once, user reply transitions ticket back to WAITING_FOR_ADMIN
    assert res2["ticket_status"] == "WAITING_FOR_ADMIN"

    # Verify conversation thread via GET endpoint
    details_resp = await client.get(f"/api/v1/support/tickets/{ticket_id}")
    assert details_resp.status_code == 200
    msgs = details_resp.json()["messages"]
    assert len(msgs) == 4  # System Confirm + User Reply 1 + Admin Reply + User Reply 2
    assert msgs[0]["message_type"] == "SYSTEM"
    assert msgs[1]["message_type"] == "USER"
    assert msgs[2]["message_type"] == "ADMIN"
    assert msgs[3]["message_type"] == "USER"

