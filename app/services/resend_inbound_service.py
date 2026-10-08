import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.models.support import SupportTicket, SupportMessage, SupportAttachment, SupportLogEntry, generate_ticket_id
from app.services.email_service import send_ticket_confirmation_email

logger = logging.getLogger("ametheus.resend_inbound")


def verify_resend_webhook_signature(headers: Dict[str, str], body: bytes, secret: Optional[str]) -> bool:
    """
    Verifies Svix signature for Resend webhook events according to Resend security standards.
    Headers checked: svix-id, svix-timestamp, svix-signature.
    """
    if not secret or not secret.strip():
        # Allowed in local test environments where no secret is configured
        logger.warning("[RESEND WEBHOOK] RESEND_WEBHOOK_SECRET is not configured. Allowing webhook in test/dev mode.")
        return True

    # Normalize header keys to lowercase
    lower_headers = {k.lower(): v for k, v in headers.items()}
    msg_id = lower_headers.get("svix-id")
    msg_timestamp = lower_headers.get("svix-timestamp")
    msg_signature = lower_headers.get("svix-signature")

    if not msg_id or not msg_timestamp or not msg_signature:
        logger.warning("[RESEND WEBHOOK] Missing required svix signature headers (svix-id, svix-timestamp, svix-signature).")
        return False

    # Timestamp verification (within 5 minutes tolerance to prevent replay attacks)
    try:
        ts = int(msg_timestamp)
        now = int(time.time())
        if abs(now - ts) > 300:
            logger.warning(f"[RESEND WEBHOOK] Timestamp verification failed: difference {abs(now - ts)}s exceeds 300s window.")
            return False
    except (ValueError, TypeError):
        logger.warning("[RESEND WEBHOOK] Invalid svix-timestamp header.")
        return False

    # Svix secrets start with 'whsec_' and are base64 encoded
    clean_secret = secret.strip()
    try:
        if clean_secret.startswith("whsec_"):
            secret_bytes = base64.b64decode(clean_secret[6:])
        else:
            secret_bytes = clean_secret.encode("utf-8")
    except Exception as e:
        logger.error(f"[RESEND WEBHOOK] Failed to decode webhook secret: {e}")
        return False

    to_sign = f"{msg_id}.{msg_timestamp}.".encode("utf-8") + body
    expected_sig = base64.b64encode(hmac.new(secret_bytes, to_sign, hashlib.sha256).digest()).decode("utf-8")

    # svix-signature can contain multiple versions separated by space, e.g. "v1,abc v1,def"
    passed_signatures = msg_signature.split(" ")
    for sig_part in passed_signatures:
        if "," in sig_part:
            version, sig = sig_part.split(",", 1)
            if version == "v1" and hmac.compare_digest(sig.strip(), expected_sig.strip()):
                return True

    logger.warning("[RESEND WEBHOOK] Svix signature verification failed.")
    return False


def clean_email_address(raw_str: Optional[str]) -> Tuple[str, str]:
    """
    Parses a string like 'John Doe <john@example.com>' or 'john@example.com'.
    Returns (clean_email, display_name).
    """
    if not raw_str:
        return "", ""
    raw = str(raw_str).strip()
    match = re.search(r"<([^>]+)>", raw)
    if match:
        email = match.group(1).strip().lower()
        name = raw[:match.start()].strip().strip("\"'")
        return email, name or email
    clean = raw.strip().strip("<>\"'").lower()
    return clean, clean


def extract_reply_body(text: Optional[str]) -> str:
    """
    Strips quoted reply history from email clients (e.g., 'On ... wrote:', '-----Original Message-----', '> ...').
    Preserves clean user reply text.
    """
    if not text:
        return ""
    cleaned = text.strip()

    # Common email reply delimiters
    patterns = [
        r"(?m)^On\s+.+,\s*.+wrote:\s*$",
        r"(?m)^On\s+.+wrote:\s*$",
        r"(?m)^-{3,}\s*Original Message\s*-{3,}",
        r"(?m)^_{10,}",
        r"(?m)^From:\s*.+@.+",
    ]
    for pat in patterns:
        parts = re.split(pat, cleaned, maxsplit=1)
        if len(parts) > 1 and parts[0].strip():
            cleaned = parts[0]
            break

    # Strip quoted block lines starting with '>'
    lines = cleaned.splitlines()
    trimmed_lines = []
    for line in lines:
        if line.strip().startswith(">"):
            continue
        trimmed_lines.append(line)

    result = "\n".join(trimmed_lines).strip()
    return result if result else (text.strip() if text else "")


def sanitize_html(html_str: Optional[str]) -> str:
    """
    Sanitizes untrusted HTML from inbound emails to prevent XSS.
    Strips dangerous tags (<script>, <iframe>, <object>, etc.) and event handlers (onload, onclick).
    """
    if not html_str:
        return ""
    sanitized = str(html_str)
    # Remove script, iframe, object, embed, applet, style tags and contents
    sanitized = re.sub(r"(?is)<script.*?>.*?</script>", "", sanitized)
    sanitized = re.sub(r"(?is)<iframe.*?>.*?</iframe>", "", sanitized)
    sanitized = re.sub(r"(?is)<object.*?>.*?</object>", "", sanitized)
    sanitized = re.sub(r"(?is)<embed.*?>.*?</embed>", "", sanitized)
    sanitized = re.sub(r"(?is)<style.*?>.*?</style>", "", sanitized)
    # Remove event handler attributes (on*="...")
    sanitized = re.sub(r"(?i)\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", sanitized)
    # Remove javascript: pseudo-protocols
    sanitized = re.sub(r"(?i)javascript:\s*", "", sanitized)
    return sanitized.strip()


def extract_ticket_candidates_from_subject(subject: Optional[str]) -> List[str]:
    """
    Extracts potential ticket identifier candidates from subject line.
    E.g. 'Re: [Ticket AMT-B3F921]', 'Re: Support Ticket #AM-1042', 'Re: Payment [#1024]', 'AMT-B3F921', 'AM-1042'.
    """
    if not subject:
        return []
    candidates = []
    # Pattern 1: AMT-XXXXXX or AM-XXXXXX (e.g. AM-1042, AMT-000102)
    amt_matches = re.findall(r"\b(?:AMT|AM)-[A-Z0-9]{3,10}\b", subject, re.IGNORECASE)
    for m in amt_matches:
        candidates.append(m.upper())
        candidates.append(f"#{m.upper()}")
    # Pattern 2: [#1234] or [Ticket 1234] or [Ticket #AM-1042]
    bracket_matches = re.findall(r"\[(?:Ticket\s*)?#?([A-Za-z0-9_-]{3,20})\]", subject, re.IGNORECASE)
    for m in bracket_matches:
        candidates.append(m)
        if not m.startswith("#"):
            candidates.append(f"#{m}")
    # Pattern 3: #1234 or #AM-1042
    hash_matches = re.findall(r"#([A-Za-z0-9_-]{3,20})", subject)
    for m in hash_matches:
        candidates.append(m)
        candidates.append(f"#{m}")
    return list(dict.fromkeys(candidates))


async def find_matching_ticket(
    db: AsyncIOMotorDatabase,
    message_id: Optional[str],
    in_reply_to: Optional[str],
    references: List[str],
    subject: Optional[str],
    sender_email: str,
    thread_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Identifies the existing support ticket according to strict matching priority (Section 14):
    1. Existing thread/message identifier.
    2. In-Reply-To matching a stored Message-ID.
    3. References matching a stored Message-ID.
    4. Stored message ID.
    5. Ticket ID in subject (validated against sender email/user).
    6. Sender + appropriate open ticket fallback.
    """
    sender_clean = sender_email.strip().lower()

    # Priority 1: Existing thread/message identifier
    if thread_id:
        clean_th = str(thread_id).strip()
        ticket_by_thread = await db.support_tickets.find_one(
            {"$or": [{"_id": clean_th}, {"ticket_id": clean_th}, {"id": clean_th}, {"thread_id": clean_th}]}
        )
        if ticket_by_thread:
            logger.info(f"[RESEND INBOUND] Priority 1 Match: Matched ticket '{ticket_by_thread.get('ticket_id')}' via thread identifier '{clean_th}'")
            return ticket_by_thread

    # Priority 2: In-Reply-To
    if in_reply_to:
        clean_in_reply = in_reply_to.strip().strip("<>")
        query = {
            "$or": [
                {"message_id": clean_in_reply},
                {"message_id": f"<{clean_in_reply}>"},
                {"message_id": in_reply_to.strip()},
            ]
        }
        msg_doc = await db.support_messages.find_one(query)
        if msg_doc and msg_doc.get("ticket_id"):
            tid = msg_doc["ticket_id"]
            ticket = await db.support_tickets.find_one({"$or": [{"_id": tid}, {"ticket_id": tid}, {"id": tid}]})
            if ticket:
                logger.info(f"[RESEND INBOUND] Priority 2 Match: Matched ticket '{ticket.get('ticket_id')}' via In-Reply-To '{in_reply_to}'")
                return ticket

    # Priority 3: References
    if references:
        clean_refs = []
        for r in references:
            c = r.strip().strip("<>")
            clean_refs.extend([c, f"<{c}>", r.strip()])
        if clean_refs:
            msg_doc = await db.support_messages.find_one({"message_id": {"$in": clean_refs}})
            if msg_doc and msg_doc.get("ticket_id"):
                tid = msg_doc["ticket_id"]
                ticket = await db.support_tickets.find_one({"$or": [{"_id": tid}, {"ticket_id": tid}, {"id": tid}]})
                if ticket:
                    logger.info(f"[RESEND INBOUND] Priority 3 Match: Matched ticket '{ticket.get('ticket_id')}' via References list")
                    return ticket

    # Priority 4: Stored message ID
    if message_id:
        clean_msg_id = message_id.strip().strip("<>")
        query = {
            "$or": [
                {"message_id": clean_msg_id},
                {"message_id": f"<{clean_msg_id}>"},
                {"message_id": message_id.strip()},
            ]
        }
        msg_doc = await db.support_messages.find_one(query)
        if msg_doc and msg_doc.get("ticket_id"):
            tid = msg_doc["ticket_id"]
            ticket = await db.support_tickets.find_one({"$or": [{"_id": tid}, {"ticket_id": tid}, {"id": tid}]})
            if ticket:
                logger.info(f"[RESEND INBOUND] Priority 4 Match: Matched ticket '{ticket.get('ticket_id')}' via Stored message ID")
                return ticket

    # Priority 5: Ticket identifier in subject (with strict sender ownership validation)
    candidates = extract_ticket_candidates_from_subject(subject)
    for cand in candidates:
        ticket = await db.support_tickets.find_one(
            {"$or": [{"_id": cand}, {"ticket_id": cand}, {"id": cand}]}
        )
        if ticket:
            ticket_owner_email = (ticket.get("email") or "").strip().lower()
            # Validate sender relationship: sender email must match ticket email
            if ticket_owner_email == sender_clean:
                logger.info(f"[RESEND INBOUND] Priority 5 Match: Matched ticket '{ticket.get('ticket_id')}' via subject identifier '{cand}' and validated sender '{sender_clean}'")
                return ticket
            else:
                logger.warning(
                    f"[RESEND INBOUND] Priority 5 Rejection: Candidate ticket '{cand}' found but sender '{sender_clean}' does not match ticket owner '{ticket_owner_email}'. Rejecting relationship."
                )

    # Priority 6: Existing open ticket for sender email fallback
    if sender_clean:
        open_ticket = await db.support_tickets.find_one(
            {
                "email": sender_clean,
                "status": {"$in": ["WAITING_FOR_ADMIN", "WAITING_FOR_USER", "OPEN", "IN_PROGRESS", "NEW"]},
                "is_archived": {"$ne": True},
            },
            sort=[("updated_at", -1)],
        )
        if open_ticket:
            logger.info(f"[RESEND INBOUND] Priority 6 Fallback: Matched latest open ticket '{open_ticket.get('ticket_id')}' for sender '{sender_clean}'")
            return open_ticket

    return None


async def process_resend_inbound_email(db: AsyncIOMotorDatabase, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Core inbound processor for Resend email events:
    - Parses sender, recipient, subject, bodies, headers, and attachments
    - Deduplicates using Message-ID
    - Identifies conversation thread
    - Preserves SYSTEM vs ADMIN vs USER status distinction
    - Updates ticket state and appends to conversation thread
    """
    logger.info("[RESEND INBOUND] Processing received inbound email event")

    # Extract sender
    from_raw = data.get("from") or ""
    sender_email, sender_name = clean_email_address(from_raw)

    # Extract recipient
    to_raw = data.get("to") or []
    if isinstance(to_raw, list):
        to_email = to_raw[0] if to_raw else "support@ametheus.games"
    else:
        to_email = str(to_raw)
    recipient_email, _ = clean_email_address(to_email)

    subject = (data.get("subject") or "").strip()
    text_content = data.get("text") or ""
    html_content = data.get("html") or ""

    # Message-ID extraction
    raw_message_id = data.get("message_id")
    headers = data.get("headers") or {}
    lower_headers = {str(k).lower(): v for k, v in headers.items()} if isinstance(headers, dict) else {}

    if not raw_message_id and "message-id" in lower_headers:
        raw_message_id = lower_headers["message-id"]
    if not raw_message_id:
        raw_message_id = f"<resend_{data.get('email_id') or secrets.token_hex(8)}@inbound.ametheus.games>"

    message_id = str(raw_message_id).strip()

    # In-Reply-To extraction
    in_reply_to = data.get("in_reply_to") or lower_headers.get("in-reply-to")
    if in_reply_to:
        in_reply_to = str(in_reply_to).strip()

    # References extraction
    references_raw = data.get("references") or lower_headers.get("references")
    references: List[str] = []
    if isinstance(references_raw, list):
        references = [str(r).strip() for r in references_raw if r]
    elif isinstance(references_raw, str):
        references = [r.strip() for r in references_raw.split() if r.strip()]

    # Attachments extraction
    attachments_raw = data.get("attachments") or []
    processed_attachments: List[SupportAttachment] = []
    for att in attachments_raw:
        if isinstance(att, dict):
            processed_attachments.append(
                SupportAttachment(
                    id=att.get("id") or f"att_{secrets.token_hex(6)}",
                    filename=att.get("filename") or "attachment",
                    content_type=att.get("content_type") or att.get("contentType") or "application/octet-stream",
                    size=int(att.get("size") or 0),
                    url=att.get("download_url") or att.get("url") or None,
                )
            )

    logger.info(f"[RESEND INBOUND] Sender: {sender_email} | Message-ID: {message_id} | In-Reply-To: {in_reply_to} | Subject: '{subject}'")

    # 1. Deduplication / Idempotency Check
    existing_message = await db.support_messages.find_one({"message_id": message_id})
    if existing_message:
        logger.info(f"[RESEND INBOUND] Duplicate webhook ignored for Message-ID: {message_id}")
        return {
            "status": "duplicate",
            "message": "Message already processed",
            "message_id": existing_message.get("id"),
            "ticket_id": existing_message.get("ticket_id"),
        }

    now_iso = datetime.now(timezone.utc).isoformat()
    clean_body = extract_reply_body(text_content) or (text_content.strip() if text_content else "No message text.")
    sanitized_html_content = sanitize_html(html_content) if html_content else None
    thread_id = data.get("thread_id") or lower_headers.get("x-thread-id") or lower_headers.get("x-ticket-id") or data.get("ticket_id")

    # 2. Find matching ticket using 6-tier matching sequence
    ticket = await find_matching_ticket(
        db=db,
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        subject=subject,
        sender_email=sender_email,
        thread_id=str(thread_id).strip() if thread_id else None,
    )

    if ticket:
        ticket_id = ticket.get("ticket_id") or str(ticket.get("_id"))

        # Check whether an admin has ever responded to this ticket
        # Rule: Automated confirmation is SYSTEM, not ADMIN.
        # Only genuine ADMIN messages transition the conversation state.
        admin_messages_count = await db.support_messages.count_documents(
            {
                "ticket_id": ticket_id,
                "message_type": "ADMIN",
                "direction": "outbound",
            }
        )
        has_admin_responded = admin_messages_count > 0

        # Status Transition Logic:
        # If admin has already responded once: user reply sets status to WAITING_FOR_ADMIN.
        # If user is replying to confirmation before any admin response: status remains WAITING_FOR_ADMIN.
        current_status = ticket.get("status", "OPEN")
        if current_status in ("RESOLVED", "CLOSED"):
            # Reopen resolved/closed ticket on customer reply
            new_status = "WAITING_FOR_ADMIN"
        elif has_admin_responded:
            new_status = "WAITING_FOR_ADMIN"
        else:
            # Remains in current state (WAITING_FOR_ADMIN or OPEN) without becoming WAITING_FOR_USER
            new_status = "WAITING_FOR_ADMIN" if current_status == "WAITING_FOR_ADMIN" else current_status

        # Create inbound USER message
        new_msg = SupportMessage(
            ticket_id=ticket_id,
            thread_id=ticket_id,
            sender_id=ticket.get("user_id"),
            sender_email=sender_email,
            sender_name=sender_name or ticket.get("name", "Contender"),
            recipient_email=recipient_email,
            subject=subject,
            body_text=text_content,
            body_html=sanitized_html_content,
            message=clean_body,
            message_id=message_id,
            in_reply_to=in_reply_to,
            references=references,
            direction="inbound",
            message_type="USER",
            sender_role="USER",
            attachments=processed_attachments,
            created_at=now_iso,
        )

        msg_mongo = new_msg.to_mongo()
        await db.support_messages.insert_one(msg_mongo)

        # Audit / Support Log
        inbound_log = SupportLogEntry(
            ticket_id=ticket_id,
            action="USER_REPLY_EMAIL",
            timestamp=now_iso,
            actor=sender_name or sender_email,
            actor_role="USER",
            details=f"Inbound email reply received ({message_id}). Status is {new_status}.",
        )
        await db.support_logs.insert_one(inbound_log.to_mongo())

        # Update Ticket Document
        update_fields = {
            "status": new_status,
            "has_new_reply": True,
            "last_message_at": now_iso,
            "last_message_by": sender_name or sender_email,
            "updated_at": now_iso,
        }
        await db.support_tickets.update_one(
            {"_id": ticket["_id"]},
            {
                "$set": update_fields,
                "$push": {
                    "messages": msg_mongo,
                    "logs": inbound_log.to_mongo(),
                },
            },
        )

        logger.info(f"[RESEND INBOUND] Message saved to Ticket #{ticket_id}. Status: {new_status}. New reply flag: True")
        return {
            "status": "success",
            "action": "reply_appended",
            "ticket_id": ticket_id,
            "message_id": new_msg.id,
            "ticket_status": new_status,
        }

    else:
        # No matching ticket found: create a brand new Support Ticket
        new_tid = generate_ticket_id()
        logger.info(f"[RESEND INBOUND] No matching ticket found. Creating new Ticket #{new_tid} for {sender_email}")

        initial_user_msg = SupportMessage(
            ticket_id=new_tid,
            thread_id=new_tid,
            sender_email=sender_email,
            sender_name=sender_name or "Contender",
            recipient_email=recipient_email,
            subject=subject or f"Support Inquiry [{new_tid}]",
            body_text=text_content,
            body_html=sanitized_html_content,
            message=clean_body,
            message_id=message_id,
            in_reply_to=in_reply_to,
            references=references,
            direction="inbound",
            message_type="USER",
            sender_role="USER",
            attachments=processed_attachments,
            created_at=now_iso,
        )
        initial_msg_doc = initial_user_msg.to_mongo()
        await db.support_messages.insert_one(initial_msg_doc)

        init_log = SupportLogEntry(
            ticket_id=new_tid,
            action="TICKET_CREATED_VIA_EMAIL",
            timestamp=now_iso,
            actor=sender_name or sender_email,
            actor_role="USER",
            details="Ticket initiated through direct inbound email inquiry.",
        )
        await db.support_logs.insert_one(init_log.to_mongo())

        new_ticket = SupportTicket(
            id=new_tid,
            ticket_id=new_tid,
            name=sender_name or "Contender",
            email=sender_email,
            category="OTHER",
            payment_type="NOT_APPLICABLE",
            description=clean_body,
            status="WAITING_FOR_ADMIN",
            has_new_reply=True,
            last_message_at=now_iso,
            last_message_by=sender_name or sender_email,
            messages=[initial_user_msg],
            logs=[init_log],
            created_at=now_iso,
            updated_at=now_iso,
        )
        await db.support_tickets.insert_one(new_ticket.to_mongo())

        # Send automated confirmation via Brevo SMTP and save as SYSTEM message
        ticket_dict = {
            "id": new_tid,
            "ticket_id": new_tid,
            "name": new_ticket.name,
            "email": new_ticket.email,
            "category": new_ticket.category,
            "payment_type": new_ticket.payment_type,
            "description": new_ticket.description,
            "created_at": now_iso,
        }
        confirm_ok, confirm_err, confirm_msg_id = await send_ticket_confirmation_email(ticket_dict, db)

        system_msg = SupportMessage(
            ticket_id=new_tid,
            thread_id=new_tid,
            sender_email=settings.SUPPORT_EMAIL,
            sender_name="Ametheus System",
            recipient_email=sender_email,
            subject=f"Ametheus Support Ticket Created — {new_tid}",
            body_text=f"Automated confirmation receipt dispatched to {sender_email}.",
            message=f"Automated confirmation receipt dispatched to {sender_email}.",
            message_id=confirm_msg_id or f"<AMT-CONFIRM-{new_tid}@ametheus.games>",
            direction="outbound",
            message_type="SYSTEM",
            sender_role="SYSTEM",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        sys_doc = system_msg.to_mongo()
        await db.support_messages.insert_one(sys_doc)
        await db.support_tickets.update_one(
            {"_id": new_tid},
            {"$push": {"messages": sys_doc}},
        )

        logger.info(f"[RESEND INBOUND] Created new Ticket #{new_tid} with initial USER message and SYSTEM confirmation.")
        return {
            "status": "ticket_created",
            "ticket_id": new_tid,
            "message_id": initial_user_msg.id,
            "ticket_status": "WAITING_FOR_ADMIN",
        }
