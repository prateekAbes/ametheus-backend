import asyncio
import logging
import smtplib
import socket
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, Tuple, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.schemas.email_settings import (
    SendTestEmailResponse,
    SmtpStatusResponse,
    EmailSystemStatusResponse,
    EmailServiceStatusItem,
)

logger = logging.getLogger("ametheus.email")


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
) -> Tuple[bool, Optional[str], Optional[str], Optional[str], str]:
    """
    Synchronous SMTP delivery through Brevo SMTP using the selected sender identity.
    Returns:
        (success: bool, error_code: Optional[str], error_details: Optional[str], message_id: Optional[str], sender_used: str)
    """
    to_email_clean = (to_email or "").strip().lower()
    if not to_email_clean or "@" not in to_email_clean:
        logger.warning(f"[Ametheus Email] Invalid recipient email address: '{to_email}'")
        return False, "RECIPIENT_REJECTED", f"Invalid recipient email address: '{to_email}'", None, ""

    cfg = get_identity_config(identity_key)
    sender_used = cfg["email"]

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
    msg["From"] = f"{cfg['name']} <{cfg['email']}>"
    msg["To"] = to_email_clean
    msg["Message-ID"] = f"<{msg_id}>"

    # Only set Reply-To when a meaningful address exists
    effective_reply_to = reply_to or cfg["reply_to"]
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
        envelope_sender = cfg["email"]
        server.sendmail(envelope_sender, [to_email_clean], msg.as_string())
        logger.info(f"[Ametheus Email] Successfully delivered email via {identity_key.upper()} to <{to_email_clean}> from <{cfg['email']}> | Subject: '{subject}'")
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
        return False, "INVALID_SENDER", f"Sender address rejected: {cfg['email']}", None, sender_used

    except Exception as e:
        err_msg = str(e)
        logger.error(f"[Ametheus Email] Failed to deliver email via {identity_key} to <{to_email_clean}>: {err_msg}", exc_info=True)
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


async def send_support_email_threaded(
    to_email: str,
    subject: str,
    text_content: str,
    html_content: str,
    reply_to: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Sends an email using the SUPPORT identity (support@ametheus.games) with email threading headers (In-Reply-To, References).
    Routes strictly via Brevo SMTP with Reply-To set to support@ametheus.games.
    Returns: (success: bool, error_details: Optional[str], message_id: Optional[str])
    """
    ok, err_code, err_det, msg_id, _ = await asyncio.to_thread(
        _send_identity_smtp_sync,
        "support",
        to_email,
        subject,
        text_content,
        html_content,
        reply_to or settings.SUPPORT_EMAIL.strip(),
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
    Utilizes the unified Ametheus email template system.
    """
    norm_service = (service or "support").strip().lower()
    if norm_service not in ("support", "noreply", "official"):
        norm_service = "support"

    cfg = get_identity_config(norm_service)
    from app.services.email_template_system import build_smtp_diagnostic_email
    subject, text_body, html_body = build_smtp_diagnostic_email(
        service_name=norm_service,
        sender_name=cfg["name"],
        sender_email=cfg["email"],
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
    Sends automated confirmation receipt email to the user using the SUPPORT identity (support@ametheus.games).
    Reply-To points strictly to support@ametheus.games.
    Uses the unified Ametheus email template system.
    """
    recipient = (ticket.get("email") or "").strip().lower()
    if not recipient:
        return False, "Recipient email is missing", None

    from app.services.email_template_system import build_support_ticket_confirmation_email
    subject, text_body, html_body = build_support_ticket_confirmation_email(ticket)

    # Strictly sent from support@ametheus.games with Reply-To: support@ametheus.games
    ok, err_code, err_det, msg_id, _ = await asyncio.to_thread(
        _send_identity_smtp_sync,
        "support",
        recipient,
        subject,
        text_body,
        html_body,
        settings.SUPPORT_EMAIL.strip(),
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
    Routes via the SUPPORT email identity using the unified Ametheus template system.
    """
    target_recipient = admin_recipient or getattr(settings, "SUPPORT_ADMIN_EMAIL", None) or "support@tenetadmissions.com"

    from app.services.email_template_system import build_admin_ticket_notification_email
    subject, text_body, html_body = build_admin_ticket_notification_email(ticket)

    return await send_support_email(target_recipient, subject, text_body, html_body)


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
    Routes via the NO-REPLY identity (noreply@ametheus.games) using the unified Ametheus template system.
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

    return await send_noreply_email(to_email_clean, subject, text_body, html_body)


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
    Routes via the NO-REPLY identity (noreply@ametheus.games) with the unified Ametheus template system.
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

    return await send_noreply_email(to_email_clean, subject, text_body, html_body)


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
    Sends an official announcement using official@ametheus.games.
    """
    text_body, html_body = render_official_announcement_email(subject, message, recipient_name)
    return await send_official_email(to_email, subject, text_body, html_body)


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
    Handles both initial assignment ('MATCH SCHEDULED') and updates ('MATCH UPDATED').
    Utilizes the unified Ametheus template system.
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

    return await send_noreply_email(to_email_clean, subject, text_body, html_body)


