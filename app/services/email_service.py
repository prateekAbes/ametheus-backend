import asyncio
import logging
import re
import smtplib
import socket
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from typing import Optional, Tuple, Dict, Any, Union
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.schemas.email_settings import (
    SendTestEmailResponse,
    SmtpStatusResponse,
    EmailSystemStatusResponse,
    EmailServiceStatusItem,
)

logger = logging.getLogger("ametheus.email")


class EmailType(str, Enum):
    SUPPORT_TICKET_CONFIRMATION = "SUPPORT_TICKET_CONFIRMATION"
    SUPPORT_ADMIN_REPLY = "SUPPORT_ADMIN_REPLY"
    ADMIN_TICKET_ALERT = "ADMIN_TICKET_ALERT"
    USER_PLATFORM_NOTIFICATION = "USER_PLATFORM_NOTIFICATION"
    SECURITY_LOGIN_ALERT = "SECURITY_LOGIN_ALERT"
    MATCH_ROSTER_SCHEDULE = "MATCH_ROSTER_SCHEDULE"
    OFFICIAL_PLATFORM_ANNOUNCEMENT = "OFFICIAL_PLATFORM_ANNOUNCEMENT"
    ORGANIZER_COMMUNICATION = "ORGANIZER_COMMUNICATION"
    SMTP_DIAGNOSTIC = "SMTP_DIAGNOSTIC"


def resolve_email_identity(
    email_type: Union[EmailType, str],
    diagnostic_identity: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Strict server-side resolver mapping explicit EmailType to sender identity,
    display name, and Reply-To header.
    Never falls back to noreply for Support Admin Reply.
    """
    type_str = str(email_type.value if isinstance(email_type, EmailType) else email_type).strip().upper()

    host = settings.SMTP_HOST.strip() if settings.SMTP_HOST else "smtp-relay.brevo.com"
    port = int(settings.SMTP_PORT or 587)
    username = settings.SMTP_USERNAME.strip() if settings.SMTP_USERNAME else None
    password = settings.SMTP_PASSWORD.strip() if settings.SMTP_PASSWORD else None
    use_tls = bool(settings.SMTP_USE_TLS)

    # 1. PRIMARY DEFENSIVE GUARD: SUPPORT_ADMIN_REPLY
    # A Support Admin Reply MUST always be sent from support@ametheus.games
    # with Reply-To: support@inbound.ametheus.games.
    # It must NEVER fall back to noreply@ametheus.games.
    if type_str == EmailType.SUPPORT_ADMIN_REPLY.value:
        return {
            "email_type": EmailType.SUPPORT_ADMIN_REPLY.value,
            "identity_key": "support",
            "from_email": settings.SUPPORT_EMAIL.strip(),
            "from_name": settings.SUPPORT_EMAIL_NAME.strip(),
            "reply_to": settings.SUPPORT_REPLY_TO.strip(),
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "use_tls": use_tls,
        }

    # 2. Support Ticket Confirmation (automated confirmation to user)
    # From: noreply@ametheus.games, Reply-To: support@ametheus.games
    elif type_str == EmailType.SUPPORT_TICKET_CONFIRMATION.value:
        return {
            "email_type": EmailType.SUPPORT_TICKET_CONFIRMATION.value,
            "identity_key": "noreply",
            "from_email": settings.NOREPLY_EMAIL.strip(),
            "from_name": settings.NOREPLY_EMAIL_NAME.strip(),
            "reply_to": settings.NOREPLY_REPLY_TO.strip(),
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "use_tls": use_tls,
        }

    # 3. Admin Ticket Alert (internal notification of new support ticket)
    # From: official@ametheus.games, Reply-To: support@inbound.ametheus.games
    elif type_str == EmailType.ADMIN_TICKET_ALERT.value:
        return {
            "email_type": EmailType.ADMIN_TICKET_ALERT.value,
            "identity_key": "official",
            "from_email": settings.OFFICIAL_EMAIL.strip(),
            "from_name": settings.OFFICIAL_EMAIL_NAME.strip(),
            "reply_to": settings.SUPPORT_REPLY_TO.strip(),
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "use_tls": use_tls,
        }

    # 4. User Platform Notifications & System alerts
    # From: noreply@ametheus.games, Reply-To: support@ametheus.games
    elif type_str in (
        EmailType.USER_PLATFORM_NOTIFICATION.value,
        EmailType.SECURITY_LOGIN_ALERT.value,
        EmailType.MATCH_ROSTER_SCHEDULE.value,
    ):
        return {
            "email_type": type_str,
            "identity_key": "noreply",
            "from_email": settings.NOREPLY_EMAIL.strip(),
            "from_name": settings.NOREPLY_EMAIL_NAME.strip(),
            "reply_to": settings.NOREPLY_REPLY_TO.strip(),
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "use_tls": use_tls,
        }

    # 5. Official Platform Announcements & Organizer Communications
    # From: official@ametheus.games, Reply-To: official@ametheus.games
    elif type_str in (
        EmailType.OFFICIAL_PLATFORM_ANNOUNCEMENT.value,
        EmailType.ORGANIZER_COMMUNICATION.value,
    ):
        return {
            "email_type": type_str,
            "identity_key": "official",
            "from_email": settings.OFFICIAL_EMAIL.strip(),
            "from_name": settings.OFFICIAL_EMAIL_NAME.strip(),
            "reply_to": (settings.OFFICIAL_REPLY_TO or settings.OFFICIAL_EMAIL).strip(),
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "use_tls": use_tls,
        }

    # 6. SMTP Diagnostic Tester (independent test per identity without altering production routing)
    elif type_str == EmailType.SMTP_DIAGNOSTIC.value:
        norm_diag = (diagnostic_identity or "support").strip().lower()
        if norm_diag in ("noreply", "automated"):
            return {
                "email_type": EmailType.SMTP_DIAGNOSTIC.value,
                "identity_key": "noreply",
                "from_email": settings.NOREPLY_EMAIL.strip(),
                "from_name": settings.NOREPLY_EMAIL_NAME.strip(),
                "reply_to": settings.NOREPLY_REPLY_TO.strip(),
                "host": host,
                "port": port,
                "username": username,
                "password": password,
                "use_tls": use_tls,
            }
        elif norm_diag in ("official", "business"):
            return {
                "email_type": EmailType.SMTP_DIAGNOSTIC.value,
                "identity_key": "official",
                "from_email": settings.OFFICIAL_EMAIL.strip(),
                "from_name": settings.OFFICIAL_EMAIL_NAME.strip(),
                "reply_to": (settings.OFFICIAL_REPLY_TO or settings.OFFICIAL_EMAIL).strip(),
                "host": host,
                "port": port,
                "username": username,
                "password": password,
                "use_tls": use_tls,
            }
        else:
            return {
                "email_type": EmailType.SMTP_DIAGNOSTIC.value,
                "identity_key": "support",
                "from_email": settings.SUPPORT_EMAIL.strip(),
                "from_name": settings.SUPPORT_EMAIL_NAME.strip(),
                "reply_to": settings.SUPPORT_REPLY_TO.strip(),
                "host": host,
                "port": port,
                "username": username,
                "password": password,
                "use_tls": use_tls,
            }

    else:
        raise ValueError(f"Unknown email type: '{email_type}'")


def get_identity_config(identity: str) -> Dict[str, Any]:
    """
    Returns the fixed configuration dictionary for one of the three Ametheus sender identities:
    'official', 'support', or 'noreply'.
    All three identities utilize the shared Brevo SMTP infrastructure.
    """
    norm = (identity or "").strip().lower()

    # Shared Brevo SMTP connection parameters
    host = settings.SMTP_HOST.strip() if settings.SMTP_HOST else "smtp-relay.brevo.com"
    port = int(settings.SMTP_PORT or 587)
    username = settings.SMTP_USERNAME.strip() if settings.SMTP_USERNAME else None
    password = settings.SMTP_PASSWORD.strip() if settings.SMTP_PASSWORD else None
    use_tls = bool(settings.SMTP_USE_TLS)

    if norm in ("support", "help", "contact"):
        return {
            "key": "support",
            "email": settings.SUPPORT_EMAIL.strip(),
            "name": settings.SUPPORT_EMAIL_NAME.strip(),
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "use_tls": use_tls,
            "reply_to": settings.SUPPORT_EMAIL.strip(),
        }
    elif norm in ("noreply", "no-reply", "automated", "system"):
        return {
            "key": "noreply",
            "email": settings.NOREPLY_EMAIL.strip(),
            "name": settings.NOREPLY_EMAIL_NAME.strip(),
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "use_tls": use_tls,
            "reply_to": "",  # noreply identity: no reply-to header
        }
    elif norm in ("official", "business", "general"):
        official_reply_to = getattr(settings, "OFFICIAL_REPLY_TO", None) or ""
        return {
            "key": "official",
            "email": settings.OFFICIAL_EMAIL.strip(),
            "name": settings.OFFICIAL_EMAIL_NAME.strip(),
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "use_tls": use_tls,
            "reply_to": official_reply_to.strip() if official_reply_to else "",
        }
    else:
        raise ValueError(f"Unknown email identity: '{identity}'. Must be 'official', 'support', or 'noreply'.")


def _send_identity_smtp_sync(
    identity_key: str,
    to_email: str,
    subject: str,
    text_content: str,
    html_content: str,
    reply_to: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    *args: Any,
    **kwargs: Any,
) -> Tuple[bool, Optional[str], Optional[str], Optional[str], str]:
    """
    Synchronous SMTP delivery through Brevo SMTP using the selected sender identity.
    Enforces strict server-side EmailType identity rules and defensive override guards.
    Returns:
        (success: bool, error_code: Optional[str], error_details: Optional[str], message_id: Optional[str], sender_used: str)
    """
    email_type = kwargs.get("email_type") or (args[0] if len(args) > 0 else None)
    ticket_id = kwargs.get("ticket_id") or (args[1] if len(args) > 1 else None)
    diagnostic_identity = kwargs.get("diagnostic_identity") or (args[2] if len(args) > 2 else None)

    to_email_clean = (to_email or "").strip().lower()
    if not to_email_clean or "@" not in to_email_clean:
        logger.warning(f"[Ametheus Email] Invalid recipient email address: '{to_email}'")
        return False, "RECIPIENT_REJECTED", f"Invalid recipient email address: '{to_email}'", None, ""

    # Identity Resolution: If explicit email_type is provided, resolve via resolve_email_identity
    if email_type:
        cfg = resolve_email_identity(email_type, diagnostic_identity=diagnostic_identity or identity_key)
        resolved_type = str(getattr(email_type, "value", email_type))
    else:
        norm_key = (identity_key or "").strip().lower()
        if (norm_key in ("support", "help") and (in_reply_to or references)) or (reply_to and reply_to.strip().lower() == settings.SUPPORT_REPLY_TO.strip().lower() and norm_key in ("support", "help")):
            resolved_type = EmailType.SUPPORT_ADMIN_REPLY.value
        elif norm_key in ("support", "help"):
            resolved_type = EmailType.SUPPORT_TICKET_CONFIRMATION.value
        elif norm_key in ("official", "business"):
            if reply_to and reply_to.strip().lower() == settings.SUPPORT_REPLY_TO.strip().lower():
                resolved_type = EmailType.ADMIN_TICKET_ALERT.value
            else:
                resolved_type = EmailType.OFFICIAL_PLATFORM_ANNOUNCEMENT.value
        elif norm_key in ("noreply", "automated"):
            if reply_to and reply_to.strip().lower() == settings.NOREPLY_REPLY_TO.strip().lower():
                resolved_type = EmailType.SUPPORT_TICKET_CONFIRMATION.value
            else:
                resolved_type = EmailType.USER_PLATFORM_NOTIFICATION.value
        else:
            resolved_type = identity_key.upper()
        cfg = get_identity_config(identity_key)

    if not ticket_id and subject:
        m = re.search(r"\[(?:Ticket\s+)?([A-Za-z0-9_\-]+)\]", subject, re.IGNORECASE)
        if m:
            ticket_id = m.group(1)

    # DEFENSIVE SENDER GUARD (Section 9):
    # If this is a Support Admin Reply, hard-enforce From: support@ametheus.games and Reply-To: support@inbound.ametheus.games
    # Even if someone passed identity_key="noreply" or custom from/reply_to
    is_support_admin_reply = (
        (email_type and str(getattr(email_type, "value", email_type)).upper() == EmailType.SUPPORT_ADMIN_REPLY.value)
        or (identity_key.strip().lower() in ("support", "help") and (in_reply_to or references))
        or (reply_to and reply_to.strip().lower() == settings.SUPPORT_REPLY_TO.strip().lower() and identity_key.strip().lower() in ("support", "help"))
    )
    if is_support_admin_reply:
        if identity_key.strip().lower() != "support" or (reply_to and reply_to.strip().lower() != settings.SUPPORT_REPLY_TO.strip().lower()):
            logger.warning(
                f"[Ametheus Email] Defensive override applied: attempted to send SUPPORT_ADMIN_REPLY with invalid sender/reply-to "
                f"(identity={identity_key}, reply_to={reply_to}). Overriding to From: {settings.SUPPORT_EMAIL}, Reply-To: {settings.SUPPORT_REPLY_TO}"
            )
        resolved_type = EmailType.SUPPORT_ADMIN_REPLY.value
        cfg["email"] = settings.SUPPORT_EMAIL.strip()
        cfg["name"] = settings.SUPPORT_EMAIL_NAME.strip()
        cfg["reply_to"] = settings.SUPPORT_REPLY_TO.strip()
        identity_key = "support"

    from_email = cfg.get("from_email") or cfg.get("email")
    from_name = cfg.get("from_name") or cfg.get("name")
    effective_reply_to = cfg.get("reply_to")
    if not effective_reply_to and reply_to:
        effective_reply_to = reply_to.strip()

    sender_used = from_email

    # Section 11: Structured Server-Side Diagnostic Logging (without passwords, keys, or secrets)
    logger.info(
        f"[EMAIL] type={resolved_type} ticketId={ticket_id or 'N/A'} recipient={to_email_clean} "
        f"from={from_email} replyTo={effective_reply_to or 'None'} provider=brevo"
    )

    if not settings.EMAIL_ENABLED:
        logger.info(f"[Ametheus Email] Email delivery disabled (EMAIL_ENABLED=False). Mocking delivery to <{to_email_clean}> | Subject: '{subject}'")
        return True, None, None, f"MOCK-{uuid.uuid4().hex[:8].upper()}@ametheus.games", sender_used

    if not cfg["password"] or not cfg["username"] or not cfg["host"]:
        logger.warning(
            f"[Ametheus Email] Brevo SMTP is not configured in server environment (missing SMTP_USERNAME, SMTP_PASSWORD, or SMTP_HOST). Cannot deliver email to <{to_email_clean}> | Subject: '{subject}'"
        )
        return False, "SMTP_NOT_CONFIGURED", "Brevo SMTP service is not configured in environment.", None, sender_used

    msg_id = f"AMT-{uuid.uuid4().hex[:12].upper()}@ametheus.games"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = to_email_clean
    msg["Message-ID"] = f"<{msg_id}>"

    # Only set Reply-To when a meaningful address exists
    if effective_reply_to and effective_reply_to.strip():
        msg["Reply-To"] = effective_reply_to.strip()

    if in_reply_to:
        clean_in_reply_to = in_reply_to.strip()
        if not clean_in_reply_to.startswith("<"):
            clean_in_reply_to = f"<{clean_in_reply_to}>"
        msg["In-Reply-To"] = clean_in_reply_to

    if references:
        msg["References"] = references.strip()

    part1 = MIMEText(text_content, "plain", "utf-8")
    part2 = MIMEText(html_content, "html", "utf-8")
    msg.attach(part1)
    msg.attach(part2)

    host = cfg["host"]
    port = cfg["port"]
    is_ssl = (port == 465)

    server = None
    try:
        if is_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=12)
        else:
            server = smtplib.SMTP(host, port, timeout=12)
            server.ehlo()
            if cfg["use_tls"] or port in (587, 25):
                server.starttls()
                server.ehlo()

        server.login(cfg["username"], cfg["password"])

        # Envelope sender matches sender email for Brevo relay
        envelope_sender = from_email
        server.sendmail(envelope_sender, [to_email_clean], msg.as_string())
        logger.info(f"[Ametheus Email] Successfully delivered email via {resolved_type} to <{to_email_clean}> from <{from_email}> | Subject: '{subject}'")
        return True, None, None, msg_id, sender_used

    except smtplib.SMTPAuthenticationError as e:
        err_msg = f"SMTP authentication failed: {e.smtp_error.decode(errors='replace') if isinstance(e.smtp_error, bytes) else str(e)}"
        logger.error(f"[Ametheus Email] Brevo authentication failed on {host}:{port}: {err_msg}")
        return False, "SMTP_AUTHENTICATION_FAILED", "SMTP authentication failed. Verify Brevo SMTP credentials.", None, sender_used

    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, ConnectionRefusedError, socket.gaierror, socket.error, TimeoutError) as e:
        err_msg = f"SMTP connection error: {e}"
        logger.error(f"[Ametheus Email] Connection failed on {host}:{port}: {err_msg}")
        return False, "SMTP_CONNECTION_FAILED", "Failed to connect to Brevo SMTP server. Connection timed out or host unreachable.", None, sender_used

    except smtplib.SMTPRecipientsRefused as e:
        err_msg = f"Recipient rejected: {e}"
        logger.error(f"[Ametheus Email] Recipient rejected: {err_msg}")
        return False, "RECIPIENT_REJECTED", f"Recipient address rejected: {to_email_clean}", None, sender_used

    except smtplib.SMTPSenderRefused as e:
        err_msg = f"Sender rejected: {e}"
        logger.error(f"[Ametheus Email] Sender rejected: {err_msg}")
        return False, "INVALID_SENDER", f"Sender address rejected: {from_email}", None, sender_used

    except Exception as e:
        err_msg = str(e)
        logger.error(f"[Ametheus Email] Failed to deliver email via {resolved_type} to <{to_email_clean}>: {err_msg}", exc_info=True)
        return False, "MESSAGE_SEND_FAILED", f"Failed to deliver email: {err_msg}", None, sender_used

    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass


# =========================================================================
# CENTRAL EMAIL SERVICE PUBLIC API
# =========================================================================

async def send_email(
    sender_type: str,
    recipient: str,
    subject: str,
    text_content: str,
    html_content: str,
    reply_to: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Centralized email dispatcher:
    sender_type must be 'official', 'support', or 'noreply'.
    Routes through Brevo SMTP using the chosen identity.
    """
    ok, err_code, err_det, _, _ = await asyncio.to_thread(
        _send_identity_smtp_sync,
        sender_type,
        recipient,
        subject,
        text_content,
        html_content,
        reply_to,
    )
    return ok, err_det


async def send_support_email(
    to_email: Any = None,
    subject: Optional[str] = None,
    text_content: Optional[str] = None,
    html_content: Optional[str] = None,
    reply_to: Optional[str] = None,
    **kwargs: Any,
) -> Tuple[bool, Optional[str]]:
    """
    Sends an email using the SUPPORT identity (support@ametheus.games).
    Reply-To automatically defaults to support@ametheus.games.
    Supports either standard positional arguments or a dict / kwargs payload.
    """
    support_reply_to = settings.SUPPORT_EMAIL.strip()
    if isinstance(to_email, dict):
        d = to_email
        target = d.get("to") or d.get("recipient") or d.get("to_email")
        sub = d.get("subject") or subject or ""
        html = d.get("html") or d.get("html_content") or html_content or ""
        text = d.get("text") or d.get("text_content") or text_content or ""
        rep = d.get("reply_to") or reply_to
        return await send_email(
            sender_type="support",
            recipient=target,
            subject=sub,
            text_content=text or html,
            html_content=html or text,
            reply_to=rep or support_reply_to,
        )

    to_addr = to_email or kwargs.get("to") or kwargs.get("recipient")
    sub = subject or kwargs.get("subject", "")
    html = html_content or kwargs.get("html", "")
    text = text_content or kwargs.get("text", "")
    rep = reply_to or kwargs.get("reply_to")

    return await send_email(
        sender_type="support",
        recipient=to_addr,
        subject=sub,
        text_content=text or html,
        html_content=html or text,
        reply_to=rep or support_reply_to,
    )


async def send_email_by_type(
    email_type: Union[EmailType, str],
    recipient: str,
    subject: str,
    text_content: str,
    html_content: str,
    ticket_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    diagnostic_identity: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Centralized email dispatcher utilizing explicit server-side EmailType.
    Resolves sender and Reply-To strictly server-side according to platform identity rules.
    Returns: (success: bool, error_details: Optional[str], message_id: Optional[str])
    """
    cfg = resolve_email_identity(email_type, diagnostic_identity=diagnostic_identity)
    ok, err_code, err_det, msg_id, _ = await asyncio.to_thread(
        _send_identity_smtp_sync,
        cfg["identity_key"],
        recipient,
        subject,
        text_content,
        html_content,
        cfg["reply_to"],
        in_reply_to,
        references,
    )
    return ok, err_det, msg_id


async def send_support_email_threaded(
    to_email: str,
    subject: str,
    text_content: str,
    html_content: str,
    reply_to: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    ticket_id: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Sends an email using the SUPPORT identity (support@ametheus.games) with email threading headers (In-Reply-To, References).
    Strictly classified as EmailType.SUPPORT_ADMIN_REPLY:
    - From: support@ametheus.games
    - Reply-To: support@inbound.ametheus.games
    - Sender can NEVER fall back to noreply@ametheus.games.
    Returns: (success: bool, error_details: Optional[str], message_id: Optional[str])
    """
    ok, err_code, err_det, msg_id, _ = await asyncio.to_thread(
        _send_identity_smtp_sync,
        "support",
        to_email,
        subject,
        text_content,
        html_content,
        settings.SUPPORT_REPLY_TO.strip(),
        in_reply_to,
        references,
    )
    return ok, err_det, msg_id


async def send_noreply_email(
    to_email: Any = None,
    subject: Optional[str] = None,
    text_content: Optional[str] = None,
    html_content: Optional[str] = None,
    reply_to: Optional[str] = None,
    **kwargs: Any,
) -> Tuple[bool, Optional[str]]:
    """
    Sends an email using the NO-REPLY / SYSTEM identity (noreply@ametheus.games).
    Used for registration confirmations, account verification, password resets,
    automated notifications, tournament/game notifications, match reminders, and system alerts.
    Does NOT include a Reply-To header unless explicitly provided.
    """
    if isinstance(to_email, dict):
        d = to_email
        target = d.get("to") or d.get("recipient") or d.get("to_email")
        sub = d.get("subject") or subject or ""
        html = d.get("html") or d.get("html_content") or html_content or ""
        text = d.get("text") or d.get("text_content") or text_content or ""
        rep = d.get("reply_to") or reply_to
        return await send_email(
            sender_type="noreply",
            recipient=target,
            subject=sub,
            text_content=text or html,
            html_content=html or text,
            reply_to=rep,
        )

    to_addr = to_email or kwargs.get("to") or kwargs.get("recipient")
    sub = subject or kwargs.get("subject", "")
    html = html_content or kwargs.get("html", "")
    text = text_content or kwargs.get("text", "")
    rep = reply_to or kwargs.get("reply_to")

    return await send_email(
        sender_type="noreply",
        recipient=to_addr,
        subject=sub,
        text_content=text or html,
        html_content=html or text,
        reply_to=rep,
    )


async def send_system_email(
    to_email: Any = None,
    subject: Optional[str] = None,
    text_content: Optional[str] = None,
    html_content: Optional[str] = None,
    reply_to: Optional[str] = None,
    **kwargs: Any,
) -> Tuple[bool, Optional[str]]:
    """
    Alias for send_noreply_email.
    Sends automated system transactional messages from noreply@ametheus.games.
    """
    return await send_noreply_email(
        to_email=to_email,
        subject=subject,
        text_content=text_content,
        html_content=html_content,
        reply_to=reply_to,
        **kwargs,
    )


async def send_official_email(
    to_email: Any = None,
    subject: Optional[str] = None,
    text_content: Optional[str] = None,
    html_content: Optional[str] = None,
    reply_to: Optional[str] = None,
    **kwargs: Any,
) -> Tuple[bool, Optional[str]]:
    """
    Sends an email using the OFFICIAL identity (official@ametheus.games).
    Used for official announcements, organizational correspondence, and administrative communications.
    Reply-To defaults to OFFICIAL_REPLY_TO if configured.
    """
    official_rep = getattr(settings, "OFFICIAL_REPLY_TO", None) or ""

    if isinstance(to_email, dict):
        d = to_email
        target = d.get("to") or d.get("recipient") or d.get("to_email")
        sub = d.get("subject") or subject or ""
        html = d.get("html") or d.get("html_content") or html_content or ""
        text = d.get("text") or d.get("text_content") or text_content or ""
        rep = d.get("reply_to") or reply_to or official_rep
        return await send_email(
            sender_type="official",
            recipient=target,
            subject=sub,
            text_content=text or html,
            html_content=html or text,
            reply_to=rep,
        )

    to_addr = to_email or kwargs.get("to") or kwargs.get("recipient")
    sub = subject or kwargs.get("subject", "")
    html = html_content or kwargs.get("html", "")
    text = text_content or kwargs.get("text", "")
    rep = reply_to or kwargs.get("reply_to") or official_rep

    return await send_email(
        sender_type="official",
        recipient=to_addr,
        subject=sub,
        text_content=text or html,
        html_content=html or text,
        reply_to=rep,
    )


# Compatibility aliases matching naming requirements
sendSystemEmail = send_system_email
sendSupportEmail = send_support_email
sendOfficialEmail = send_official_email


async def send_personal_email_with_details(
    sender_type: str,
    recipient: str,
    subject: str,
    text_content: str,
    html_content: str,
) -> Dict[str, Any]:
    """
    Sends an individual personal or administrative email and returns full Brevo relay diagnostic response.
    """
    from datetime import datetime, timezone
    ok, err_code, err_det, msg_id, sender_used = await asyncio.to_thread(
        _send_identity_smtp_sync,
        sender_type,
        recipient,
        subject,
        text_content,
        html_content,
        None,
    )
    return {
        "success": ok,
        "error_code": err_code,
        "error_detail": err_det,
        "message_id": msg_id,
        "sender": sender_used,
        "recipient": recipient,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# =========================================================================
# SYSTEM STATUS & STARTUP VALIDATION
# =========================================================================

def get_email_system_status() -> EmailSystemStatusResponse:
    """
    Returns the non-sensitive configuration status of the three Brevo email identities.
    Never exposes passwords, tokens, or raw secrets.
    """
    services_map: Dict[str, EmailServiceStatusItem] = {}

    for key in ("support", "noreply", "official"):
        cfg = get_identity_config(key)
        is_configured = bool(cfg["password"] and cfg["username"] and cfg["host"])
        services_map[key] = EmailServiceStatusItem(
            service=key,
            email=cfg["email"],
            display_name=cfg["name"],
            smtp_host=cfg["host"],
            smtp_port=cfg["port"],
            use_tls=cfg["use_tls"],
            configured=is_configured,
            status="Configured" if is_configured else "Not Configured",
        )

    return EmailSystemStatusResponse(
        email_enabled=settings.EMAIL_ENABLED,
        services=services_map,
    )


def validate_email_configuration() -> Dict[str, bool]:
    """
    Startup diagnostic validator called during FastAPI application lifespan.
    Logs human-readable configuration status without printing passwords.
    """
    logger.info("[Ametheus Email] Validating Brevo SMTP configuration & sender identities...")
    status = {}
    is_brevo_configured = bool(settings.SMTP_USERNAME and settings.SMTP_PASSWORD and settings.SMTP_HOST)

    for key in ("support", "noreply", "official"):
        cfg = get_identity_config(key)
        status[key] = is_brevo_configured
        if is_brevo_configured:
            logger.info(f"[Ametheus Email] {cfg['name']} ({key} -> {cfg['email']}): Configured via Brevo SMTP ({cfg['host']}:{cfg['port']})")
            print(f"[AMETHEUS EMAIL] {cfg['name']} ({cfg['email']}): Configured (Brevo)")
        else:
            logger.warning(f"[Ametheus Email] {cfg['name']} ({key} -> {cfg['email']}): Not Configured (SMTP_PASSWORD or SMTP_USERNAME missing in environment).")
            print(f"[AMETHEUS EMAIL] {cfg['name']} ({cfg['email']}): Not Configured (Brevo)")
    return status


def _check_smtp_health_sync(identity: str = "support") -> Dict[str, Any]:
    """Synchronous test of Brevo SMTP connectivity and authentication for a specific identity."""
    try:
        cfg = get_identity_config(identity)
    except ValueError as e:
        return {
            "configured": False,
            "connected": False,
            "authenticated": False,
            "sender": "Unknown",
            "service": identity,
            "status_code": "INVALID_SERVICE",
            "message": str(e),
            "host": None,
            "port": 587,
            "secure": True,
        }

    if not cfg["password"] or not cfg["username"] or not cfg["host"]:
        return {
            "configured": False,
            "connected": False,
            "authenticated": False,
            "sender": cfg["email"],
            "service": identity,
            "status_code": "NOT_CONFIGURED",
            "message": "Brevo SMTP credentials (SMTP_USERNAME / SMTP_PASSWORD) not set in server environment.",
            "host": cfg["host"],
            "port": cfg["port"],
            "secure": cfg["use_tls"],
        }

    host = cfg["host"]
    port = cfg["port"]
    is_ssl = (port == 465)

    server = None
    try:
        if is_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=10)
        else:
            server = smtplib.SMTP(host, port, timeout=10)
            server.ehlo()
            if cfg["use_tls"] or port in (587, 25):
                server.starttls()
                server.ehlo()

        server.login(cfg["username"], cfg["password"])

        return {
            "configured": True,
            "connected": True,
            "authenticated": True,
            "sender": cfg["email"],
            "service": identity,
            "status_code": "CONNECTED",
            "message": f"Brevo SMTP handshake and authentication succeeded for {cfg['email']}.",
            "host": host,
            "port": port,
            "secure": cfg["use_tls"],
        }

    except smtplib.SMTPAuthenticationError:
        return {
            "configured": True,
            "connected": True,
            "authenticated": False,
            "sender": cfg["email"],
            "service": identity,
            "status_code": "AUTHENTICATION_FAILED",
            "message": f"Authentication failed with Brevo SMTP ({host}:{port}). Check Brevo SMTP key.",
            "host": host,
            "port": port,
            "secure": cfg["use_tls"],
        }

    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, ConnectionRefusedError, socket.gaierror, socket.error, TimeoutError) as e:
        return {
            "configured": True,
            "connected": False,
            "authenticated": False,
            "sender": cfg["email"],
            "service": identity,
            "status_code": "CONNECTION_FAILED",
            "message": f"Connection failed to Brevo SMTP {host}:{port}. Error: {str(e)}",
            "host": host,
            "port": port,
            "secure": cfg["use_tls"],
        }

    except Exception as e:
        return {
            "configured": True,
            "connected": False,
            "authenticated": False,
            "sender": cfg["email"],
            "service": identity,
            "status_code": "CONNECTION_FAILED",
            "message": f"SMTP test failed: {str(e)}",
            "host": host,
            "port": port,
            "secure": cfg["use_tls"],
        }

    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass


async def check_smtp_health(
    identity: str = "support",
    db: Optional[AsyncIOMotorDatabase] = None,
) -> SmtpStatusResponse:
    """Asynchronous wrapper for live Brevo SMTP health check."""
    status_dict = await asyncio.to_thread(_check_smtp_health_sync, identity)
    return SmtpStatusResponse(**status_dict)


async def send_test_email(
    to_email: str,
    service: str = "support",
    db: Optional[AsyncIOMotorDatabase] = None,
) -> SendTestEmailResponse:
    """
    Sends a test verification email via Brevo SMTP using the chosen identity ('support', 'noreply', or 'official').
    Utilizes EmailType.SMTP_DIAGNOSTIC without altering production email routing.
    """
    norm_service = (service or "support").strip().lower()
    if norm_service not in ("support", "noreply", "official"):
        norm_service = "support"

    cfg = resolve_email_identity(EmailType.SMTP_DIAGNOSTIC, diagnostic_identity=norm_service)
    from app.services.email_template_system import build_smtp_diagnostic_email
    subject, text_body, html_body = build_smtp_diagnostic_email(
        service_name=norm_service,
        sender_name=cfg["from_name"],
        sender_email=cfg["from_email"],
        recipient_email=to_email,
        host=cfg["host"],
        port=cfg["port"],
    )

    ok, err_code, err_det, msg_id, sender_used = await asyncio.to_thread(
        _send_identity_smtp_sync,
        norm_service,
        to_email,
        subject,
        text_body,
        html_body,
        cfg["reply_to"],
        None,
        None,
    )

    return SendTestEmailResponse(
        success=ok,
        service=norm_service,
        message_id=msg_id,
        error_code=err_code,
        error_details=err_det,
        sender_used=sender_used,
        message="Test email sent successfully" if ok else (err_det or "Test email delivery failed"),
    )


# =========================================================================
# EXISTING APPLICATION EMAIL ROUTING IMPLEMENTATIONS (PRESERVED TEMPLATES)
# =========================================================================

def format_friendly_ticket_status(status_raw: Optional[str]) -> str:
    """Returns customer-friendly status for support emails and customer notifications."""
    s = (status_raw or "").strip().lower()
    if s in ("waiting_for_admin", "waiting_for_support"):
        return "Waiting for Support"
    if s in ("waiting_for_user", "waiting_for_player", "waiting_for_contender", "waiting_for_response"):
        return "Waiting for Your Response"
    if s in ("open", "new"):
        return "Open"
    if s in ("in_progress", "investigating", "in-progress"):
        return "In Progress"
    if s in ("resolved", "solved"):
        return "Resolved"
    if s in ("closed", "concluded"):
        return "Closed"
    return s.replace("_", " ").title() if s else "Waiting for Support"


async def send_ticket_confirmation_email(ticket: dict, db: Optional[AsyncIOMotorDatabase] = None) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Sends automated confirmation receipt email to the user using EmailType.SUPPORT_TICKET_CONFIRMATION:
    - From: noreply@ametheus.games
    - Reply-To: support@ametheus.games
    Uses the unified Ametheus email template system.
    """
    recipient = (ticket.get("email") or "").strip().lower()
    if not recipient:
        return False, "Recipient email is missing", None

    tid = ticket.get("ticket_id") or ticket.get("id") or "N/A"
    from app.services.email_template_system import build_support_ticket_confirmation_email
    subject, text_body, html_body = build_support_ticket_confirmation_email(ticket)

    ok, err_code, err_det, msg_id, _ = await asyncio.to_thread(
        _send_identity_smtp_sync,
        "noreply",
        recipient,
        subject,
        text_body,
        html_body,
        settings.NOREPLY_REPLY_TO.strip(),
        None,
        None,
    )
    return ok, err_det, msg_id


async def send_admin_ticket_notification_email(
    ticket: dict,
    admin_recipient: Optional[str] = None,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Sends notification to the configured support desk email with complete ticket information.
    EmailType: ADMIN_TICKET_ALERT
    - From: official@ametheus.games
    - Reply-To: support@inbound.ametheus.games
    Uses the unified Ametheus template system.
    """
    target_recipient = admin_recipient or getattr(settings, "SUPPORT_ADMIN_EMAIL", None) or "support@tenetadmissions.com"
    tid = ticket.get("ticket_id") or ticket.get("id") or "N/A"

    from app.services.email_template_system import build_admin_ticket_notification_email
    subject, text_body, html_body = build_admin_ticket_notification_email(ticket)

    ok, err_code, err_det, msg_id, _ = await asyncio.to_thread(
        _send_identity_smtp_sync,
        "official",
        target_recipient,
        subject,
        text_body,
        html_body,
        settings.SUPPORT_REPLY_TO.strip(),
        None,
        None,
    )
    return ok, err_det


async def send_notification_email(
    recipient: str,
    user_name: str,
    title: str,
    message: str,
    link: Optional[str] = None,
    notification_type: Optional[str] = None,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Sends an automated tournament/match/wallet/platform notification email to the user.
    EmailType: USER_PLATFORM_NOTIFICATION
    - From: noreply@ametheus.games
    - Reply-To: support@ametheus.games
    """
    to_email_clean = (recipient or "").strip().lower()
    if not to_email_clean or "@" not in to_email_clean:
        return False, "Recipient email is invalid or missing"

    from app.services.email_template_system import build_user_notification_email
    subject, text_body, html_body = build_user_notification_email(
        user_name=user_name,
        title=title,
        message=message,
        link=link,
        notification_type=notification_type,
    )

    ok, err_code, err_det, msg_id, _ = await asyncio.to_thread(
        _send_identity_smtp_sync,
        "noreply",
        to_email_clean,
        subject,
        text_body,
        html_body,
        settings.NOREPLY_REPLY_TO.strip(),
        None,
        None,
    )
    return ok, err_det


async def send_security_alert_email(
    to_email: str,
    user_name: str,
    client_ip: str,
    device_info: str,
    timestamp_str: str,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Dispatches a high-priority security notification email when a sign-in is detected from a new/unrecognized IP.
    EmailType: SECURITY_LOGIN_ALERT
    - From: noreply@ametheus.games
    - Reply-To: support@ametheus.games
    """
    to_email_clean = (to_email or "").strip().lower()
    if not to_email_clean or "@" not in to_email_clean:
        return False, "Recipient email is invalid or missing"

    from app.services.email_template_system import build_security_alert_email
    subject, text_body, html_body = build_security_alert_email(
        user_name=user_name,
        recipient_email=to_email_clean,
        client_ip=client_ip,
        device_info=device_info,
        timestamp_str=timestamp_str,
    )

    ok, err_code, err_det, msg_id, _ = await asyncio.to_thread(
        _send_identity_smtp_sync,
        "noreply",
        to_email_clean,
        subject,
        text_body,
        html_body,
        settings.NOREPLY_REPLY_TO.strip(),
        None,
        None,
    )
    return ok, err_det


def render_official_announcement_email(
    subject: str,
    message: str,
    recipient_name: Optional[str] = None,
) -> tuple[str, str]:
    """
    Renders professional plain text and HTML templates for Official Ametheus communications
    using the unified Ametheus template system.
    """
    from app.services.email_template_system import build_official_announcement_email
    _, text_body, html_body = build_official_announcement_email(subject, message, recipient_name)
    return text_body, html_body


async def send_official_announcement_to_recipient(
    to_email: str,
    subject: str,
    message: str,
    recipient_name: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """
    Sends an official announcement using EmailType.OFFICIAL_PLATFORM_ANNOUNCEMENT:
    - From: official@ametheus.games
    - Reply-To: official@ametheus.games
    """
    text_body, html_body = render_official_announcement_email(subject, message, recipient_name)
    ok, err_code, err_det, msg_id, _ = await asyncio.to_thread(
        _send_identity_smtp_sync,
        "official",
        to_email,
        subject,
        text_body,
        html_body,
        (settings.OFFICIAL_REPLY_TO or settings.OFFICIAL_EMAIL).strip(),
        None,
        None,
    )
    return ok, err_det


async def send_match_roster_notification_email(
    to_email: str,
    leader_name: str,
    tournament_title: str,
    game_name: str,
    round_name: str,
    match_number: int,
    team_name: str,
    opponent_name: str,
    scheduled_date: str,
    scheduled_time: str,
    match_id: str,
    community_platform: Optional[str] = None,
    community_link: Optional[str] = None,
    is_update: bool = False,
    previous_details: Optional[Dict[str, str]] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Sends a branded, mobile-responsive match roster notification email to a Team Leader.
    EmailType: MATCH_ROSTER_SCHEDULE
    - From: noreply@ametheus.games
    - Reply-To: support@ametheus.games
    """
    to_email_clean = (to_email or "").strip().lower()
    if not to_email_clean or "@" not in to_email_clean:
        return False, "Recipient email is invalid or missing"

    from app.services.email_template_system import build_match_roster_email
    subject, text_body, html_body = build_match_roster_email(
        leader_name=leader_name,
        tournament_title=tournament_title,
        game_name=game_name,
        round_name=round_name,
        match_number=match_number,
        team_name=team_name,
        opponent_name=opponent_name,
        scheduled_date=scheduled_date,
        scheduled_time=scheduled_time,
        match_id=match_id,
        community_platform=community_platform,
        community_link=community_link,
        is_update=is_update,
        previous_details=previous_details,
    )

    ok, err_code, err_det, msg_id, _ = await asyncio.to_thread(
        _send_identity_smtp_sync,
        "noreply",
        to_email_clean,
        subject,
        text_body,
        html_body,
        settings.NOREPLY_REPLY_TO.strip(),
        None,
        None,
    )
    return ok, err_det


async def send_credentials_release_email(
    to_email: str,
    recipient_name: str,
    tournament_title: str,
    round_or_match_name: str,
    room_id: str,
    room_password: Optional[str] = None,
    custom_instructions: Optional[str] = None,
    tournament_slug: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Sends official match room credentials to an authorized participant when released by an organizer.
    """
    to_email_clean = (to_email or "").strip().lower()
    if not to_email_clean or "@" not in to_email_clean:
        return False, "Recipient email is invalid or missing"

    subject = f"[Ametheus] Match Credentials Released: {tournament_title} — {round_or_match_name}"
    
    pass_text = f"Room Password: {room_password}" if room_password and room_password != "None" else "Room Password: None (No password required)"
    instructions_text = f"\nHost Instructions: {custom_instructions}" if custom_instructions else ""
    link_url = f"https://www.ametheus.games/tournaments/{tournament_slug}" if tournament_slug else "https://www.ametheus.games"

    text_body = f"""Hello {recipient_name},

Official match room credentials for {tournament_title} ({round_or_match_name}) are now LIVE.

Room ID: {room_id}
{pass_text}{instructions_text}

Reporting Window: Join the lobby 15 minutes prior to kickoff.
Access your Competitor Hub and Tournament details: {link_url}

Good luck and play fair!
— The Ametheus Esports Operations Team
"""

    pass_html = f'<p style="margin: 6px 0; font-size: 15px; color: #f59e0b;"><strong>Room Password:</strong> <code style="background: #1e1b4b; padding: 3px 8px; border-radius: 4px; font-weight: bold; color: #fbbf24;">{room_password}</code></p>' if room_password and room_password != "None" else '<p style="margin: 6px 0; font-size: 14px; color: #94a3b8;"><strong>Room Password:</strong> None (No password required)</p>'
    instructions_html = f'<div style="margin-top: 12px; padding: 10px; background: #0f172a; border-left: 3px solid #06b6d4; font-size: 13px; color: #cbd5e1;"><strong>Host Instructions:</strong> {custom_instructions}</div>' if custom_instructions else ""

    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="background: #020617; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 24px; margin: 0;">
  <div style="max-width: 580px; margin: 0 auto; background: #090d16; border: 1px solid rgba(6, 182, 212, 0.3); border-radius: 8px; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,0.5);">
    <div style="background: linear-gradient(135deg, #0e1e38 0%, #030712 100%); padding: 24px; border-bottom: 1px solid rgba(255,255,255,0.1);">
      <h1 style="margin: 0; font-size: 20px; font-weight: 800; color: #06b6d4; text-transform: uppercase; letter-spacing: 0.05em;">AMETHEUS COMPETITIVE</h1>
      <p style="margin: 4px 0 0; font-size: 13px; color: #94a3b8;">Match Room Credentials Release</p>
    </div>
    <div style="padding: 24px;">
      <p style="font-size: 15px; line-height: 1.5; color: #e2e8f0;">Hello <strong>{recipient_name}</strong>,</p>
      <p style="font-size: 14px; line-height: 1.5; color: #94a3b8;">Your match room credentials for <strong>{tournament_title}</strong> (<em>{round_or_match_name}</em>) have been published and are active.</p>
      
      <div style="margin: 20px 0; padding: 18px; background: #040814; border: 1px solid rgba(6, 182, 212, 0.4); border-radius: 6px;">
        <span style="display: block; font-size: 11px; text-transform: uppercase; font-weight: 700; color: #06b6d4; letter-spacing: 0.1em; margin-bottom: 6px;">Official Room Credentials</span>
        <p style="margin: 6px 0; font-size: 18px; color: #06b6d4;"><strong>Room ID:</strong> <code style="background: #0e2a47; padding: 4px 10px; border-radius: 4px; font-weight: 800; color: #38bdf8; letter-spacing: 0.05em;">{room_id}</code></p>
        {pass_html}
        {instructions_html}
      </div>

      <p style="font-size: 13px; color: #94a3b8; line-height: 1.5;">Reporting Window: Please enter the lobby at least <strong>15 minutes prior</strong> to kickoff.</p>
      
      <div style="margin-top: 24px; text-align: center;">
        <a href="{link_url}" style="display: inline-block; background: #06b6d4; color: #020617; font-weight: 800; font-size: 13px; text-transform: uppercase; padding: 12px 24px; text-decoration: none; border-radius: 4px; letter-spacing: 0.05em;">Open Tournament Hub</a>
      </div>
    </div>
    <div style="background: #040711; padding: 14px 24px; border-top: 1px solid rgba(255,255,255,0.06); font-size: 11px; color: #64748b; text-align: center;">
      Ametheus Platform · Automated Match Dispatch · Play Fair
    </div>
  </div>
</body>
</html>
"""

    ok, err_code, err_det, msg_id, _ = await asyncio.to_thread(
        _send_identity_smtp_sync,
        "noreply",
        to_email_clean,
        subject,
        text_body,
        html_body,
        settings.NOREPLY_REPLY_TO.strip(),
        None,
        None,
    )
    return ok, err_det



