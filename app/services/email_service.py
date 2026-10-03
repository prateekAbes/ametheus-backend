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
            "reply_to": settings.EMAIL_REPLY_TO.strip(),
        }
    elif norm in ("official", "business", "general"):
        return {
            "key": "official",
            "email": settings.OFFICIAL_EMAIL.strip(),
            "name": settings.OFFICIAL_EMAIL_NAME.strip(),
            "host": host,
            "port": port,
            "username": username,
            "password": password,
            "use_tls": use_tls,
            "reply_to": settings.SUPPORT_EMAIL.strip(),
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
        return True, None, None, f"MOCK-{uuid.uuid4().hex[:8].upper()}", sender_used

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
    msg["Reply-To"] = reply_to or cfg["reply_to"]

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
    to_email: str,
    subject: str,
    text_content: str,
    html_content: str,
    reply_to: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Sends an email using the SUPPORT identity (support@ametheus.games).
    Used for contact/support messages, user support requests, help inquiries, and support ticket communication.
    """
    return await send_email(
        sender_type="support",
        recipient=to_email,
        subject=subject,
        text_content=text_content,
        html_content=html_content,
        reply_to=reply_to or settings.SUPPORT_EMAIL,
    )


async def send_noreply_email(
    to_email: str,
    subject: str,
    text_content: str,
    html_content: str,
    reply_to: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Sends an email using the NO-REPLY / AUTOMATED identity (noreply@ametheus.games).
    Used for registration confirmations, account verification, password resets,
    automated notifications, tournament/game notifications, match reminders, and system alerts.
    """
    return await send_email(
        sender_type="noreply",
        recipient=to_email,
        subject=subject,
        text_content=text_content,
        html_content=html_content,
        reply_to=reply_to or settings.EMAIL_REPLY_TO,
    )


async def send_official_email(
    to_email: str,
    subject: str,
    text_content: str,
    html_content: str,
    reply_to: Optional[str] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Sends an email using the OFFICIAL identity (official@ametheus.games).
    Used for official announcements, organizational correspondence, and administrative communications.
    """
    return await send_email(
        sender_type="official",
        recipient=to_email,
        subject=subject,
        text_content=text_content,
        html_content=html_content,
        reply_to=reply_to or settings.SUPPORT_EMAIL,
    )


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
    """
    norm_service = (service or "support").strip().lower()
    if norm_service not in ("support", "noreply", "official"):
        norm_service = "support"

    cfg = get_identity_config(norm_service)
    subject = f"Ametheus SMTP Test — {cfg['name']} ({cfg['email']})"

    text_body = f"""Ametheus Esports Platform — Brevo SMTP Service Test
===================================================

This is an administrative test message dispatched from the Ametheus Operations Cockpit.
If you received this message, Brevo SMTP delivery for {cfg['name']} ({norm_service.upper()}) is functioning properly.

Identity: {cfg['name']}
Sender Email: {cfg['email']}
Destination: {to_email}
Relay Host: {cfg['host']}
Relay Port: {cfg['port']}

Competitive Integrity:
https://ametheus.gg
"""

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{subject}</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0B0E16; margin: 0; padding: 24px; color: #F8FAFC;">
  <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 600px; margin: 0 auto; background-color: #111622; border-radius: 12px; border: 1px solid #242C3D; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.6);">
    <tr>
      <td style="background-color: #1A2234; padding: 20px 24px; border-bottom: 2px solid #E56A10;">
        <h1 style="color: #FFFFFF; margin: 0; font-size: 20px; font-weight: 800; letter-spacing: 1px;">AMETHEUS <span style="color: #E56A10;">EMAIL TEST</span></h1>
        <p style="color: #94A3B8; margin: 4px 0 0 0; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Identity: {cfg['name']} ({norm_service.upper()})</p>
      </td>
    </tr>
    <tr>
      <td style="padding: 28px 24px;">
        <div style="background-color: #064E3B; border: 1px solid #059669; border-radius: 8px; padding: 14px 16px; margin-bottom: 20px; color: #A7F3D0; font-family: monospace; font-size: 12px; font-weight: bold;">
          &#10004; SUCCESS: Brevo SMTP relay connection and delivery confirmed!
        </div>

        <p style="margin: 0 0 16px 0; font-size: 13px; color: #CBD5E1; line-height: 1.6;">
          This verification email was triggered from the Super Admin diagnostic panel via Brevo SMTP relay.
        </p>

        <table width="100%" border="0" cellspacing="0" cellpadding="8" style="font-size: 12px; font-family: monospace; background-color: #0B0E16; border-radius: 8px; border: 1px solid #242C3D; margin-bottom: 20px;">
          <tr style="border-bottom: 1px solid #242C3D;">
            <td style="color: #94A3B8; width: 140px;">Sender Identity:</td>
            <td style="color: #E56A10; font-weight: bold;">{cfg['name']} &lt;{cfg['email']}&gt;</td>
          </tr>
          <tr style="border-bottom: 1px solid #242C3D;">
            <td style="color: #94A3B8;">Recipient:</td>
            <td style="color: #FFFFFF;">{to_email}</td>
          </tr>
          <tr style="border-bottom: 1px solid #242C3D;">
            <td style="color: #94A3B8;">Relay Server:</td>
            <td style="color: #94A3B8;">{cfg['host']}:{cfg['port']}</td>
          </tr>
        </table>
      </td>
    </tr>
    <tr>
      <td style="background-color: #0B0E16; padding: 16px 24px; text-align: center; border-top: 1px solid #242C3D; font-size: 11px; color: #64748B;">
        &copy; 2026 AMETHEUS Esports Platform. All competitive integrity rights reserved.
      </td>
    </tr>
  </table>
</body>
</html>
"""

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

async def send_ticket_confirmation_email(ticket: dict, db: Optional[AsyncIOMotorDatabase] = None) -> Tuple[bool, Optional[str]]:
    """
    Sends confirmation receipt email to the user using the SUPPORT identity (support@ametheus.games).
    Reply-To points directly to support@ametheus.games.
    """
    ticket_id = ticket.get("ticket_id") or ticket.get("id") or "AMT-000000"
    user_name = ticket.get("name", "Contender")
    category = ticket.get("category", "General Inquiry").replace("_", " ").title()
    payment_type = ticket.get("payment_type")
    description = ticket.get("description", "")
    created_at = ticket.get("created_at", "Just now")
    recipient = (ticket.get("email") or "").strip().lower()

    if not recipient:
        return False, "Recipient email is missing"

    support_cfg = get_identity_config("support")
    support_addr = support_cfg["email"]
    subject = f"Ametheus Support Ticket Created — {ticket_id}"

    payment_line_text = f"Payment Method / Type: {payment_type}\n" if payment_type and payment_type != "NOT_APPLICABLE" else ""
    payment_line_html = f"<tr><td style='padding: 8px; color: #64748B;'>Payment Type:</td><td style='padding: 8px; font-weight: bold; color: #0F172A;'>{payment_type}</td></tr>" if payment_type and payment_type != "NOT_APPLICABLE" else ""

    text_body = f"""Hello {user_name},

Thank you for contacting Ametheus Operations Support.
We have received your support request and assigned it the following official Ticket ID:

Ticket ID: {ticket_id}
Category: {category}
{payment_line_text}Submitted At: {created_at}

Issue Summary:
{description}

Our competition administration team investigates payment and tournament requests with priority.
You can reply to this email directly or contact support at {support_addr} quoting your Ticket ID: {ticket_id}.

—
Ametheus Support Operations
https://ametheus.gg
"""

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{subject}</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0B0E16; margin: 0; padding: 24px; color: #F8FAFC;">
  <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 600px; margin: 0 auto; background-color: #111622; border-radius: 12px; border: 1px solid #242C3D; overflow: hidden; box-shadow: 0 4px 10px rgba(0,0,0,0.5);">
    <tr>
      <td style="background-color: #1A2234; padding: 20px 24px; border-bottom: 2px solid #E56A10;">
        <h1 style="color: #FFFFFF; margin: 0; font-size: 20px; font-weight: 800; letter-spacing: 1px;">AMETHEUS <span style="color: #E56A10;">SUPPORT</span></h1>
        <p style="color: #94A3B8; margin: 4px 0 0 0; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Official Ticket Receipt</p>
      </td>
    </tr>
    <tr>
      <td style="padding: 28px 24px;">
        <p style="margin: 0 0 16px 0; font-size: 14px; color: #94A3B8;">
          Hello <strong style="color: #FFFFFF;">{user_name}</strong>,
        </p>
        <p style="margin: 0 0 20px 0; font-size: 13px; line-height: 1.6; color: #CBD5E1;">
          We have received your support inquiry. An operations specialist has been notified and will review your case.
        </p>

        <div style="background-color: #0B0E16; border: 1px solid #E56A10; border-radius: 8px; padding: 18px; margin-bottom: 24px; text-align: center;">
          <span style="font-size: 11px; font-family: monospace; text-transform: uppercase; letter-spacing: 1.5px; color: #94A3B8;">Official Ticket ID</span>
          <div style="font-size: 26px; font-family: monospace; font-weight: 800; color: #E56A10; margin-top: 4px; letter-spacing: 2px;">
            {ticket_id}
          </div>
        </div>

        <table width="100%" border="0" cellspacing="0" cellpadding="8" style="font-size: 13px; font-family: monospace; margin-bottom: 20px;">
          <tr style="border-bottom: 1px solid #242C3D;">
            <td style="color: #94A3B8; width: 140px;">Category:</td>
            <td style="color: #FFFFFF; font-weight: bold;">{category}</td>
          </tr>
          {payment_line_html}
          <tr style="border-bottom: 1px solid #242C3D;">
            <td style="color: #94A3B8;">Submitted At:</td>
            <td style="color: #94A3B8;">{created_at}</td>
          </tr>
        </table>

        <div style="background-color: #0B0E16; border: 1px solid #242C3D; border-radius: 8px; padding: 16px; margin-bottom: 24px;">
          <div style="font-size: 11px; font-family: monospace; text-transform: uppercase; color: #E56A10; margin-bottom: 8px; font-weight: bold;">Issue Summary:</div>
          <div style="font-size: 13px; line-height: 1.6; color: #E2E8F0; white-space: pre-wrap;">{description}</div>
        </div>

        <p style="margin: 0; font-size: 12px; color: #94A3B8; line-height: 1.6;">
          For expedited resolution, please quote <strong style="color: #E56A10;">{ticket_id}</strong> in any correspondence or email us at <a href="mailto:{support_addr}" style="color: #E56A10;">{support_addr}</a>.
        </p>
      </td>
    </tr>
    <tr>
      <td style="background-color: #0B0E16; padding: 16px 24px; text-align: center; border-top: 1px solid #242C3D; font-size: 11px; color: #64748B;">
        &copy; 2026 AMETHEUS Esports Platform. All competitive integrity rights reserved.
      </td>
    </tr>
  </table>
</body>
</html>
"""

    return await send_support_email(recipient, subject, text_body, html_body)


async def send_admin_ticket_notification_email(
    ticket: dict,
    admin_recipient: Optional[str] = None,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Sends notification to the configured support desk email with complete ticket information.
    Routes via the SUPPORT email identity.
    """
    ticket_id = ticket.get("ticket_id") or ticket.get("id") or "AMT-000000"
    user_name = ticket.get("name", "Unknown User")
    user_email = ticket.get("email", "unknown@email.com")
    user_id = ticket.get("user_id") or "Guest (Unauthenticated)"
    category = ticket.get("category", "General Inquiry").replace("_", " ").title()
    payment_type = ticket.get("payment_type", "N/A")
    description = ticket.get("description", "")
    created_at = ticket.get("created_at", "Just now")

    target_recipient = admin_recipient or settings.SUPPORT_EMAIL

    subject = f"New Ametheus Support Ticket — {ticket_id} — {category}"

    text_body = f"""New Support Ticket Logged in Ametheus Cockpit

Ticket ID: {ticket_id}
User Name: {user_name}
User Email: {user_email}
User/Account ID: {user_id}
Category: {category}
Payment Type: {payment_type}
Created At: {created_at}

Description:
{description}

View and manage this ticket in the Ametheus Admin Cockpit:
https://ametheus.gg/admin
"""

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{subject}</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0B0E16; margin: 0; padding: 24px; color: #F8FAFC;">
  <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 650px; margin: 0 auto; background-color: #111622; border-radius: 12px; border: 1px solid #242C3D; overflow: hidden;">
    <tr>
      <td style="background-color: #1A2234; padding: 20px 24px; border-bottom: 2px solid #E56A10;">
        <h2 style="margin: 0; color: #FFFFFF; font-size: 18px;">[ADMIN ALERT] New Support Ticket</h2>
        <span style="font-family: monospace; font-size: 13px; color: #E56A10; font-weight: bold;">{ticket_id}</span>
      </td>
    </tr>
    <tr>
      <td style="padding: 24px;">
        <table width="100%" border="0" cellspacing="0" cellpadding="8" style="font-size: 13px; font-family: monospace; margin-bottom: 20px;">
          <tr style="border-bottom: 1px solid #242C3D;">
            <td style="color: #94A3B8; width: 140px;">Submitting User:</td>
            <td style="color: #FFFFFF; font-weight: bold;">{user_name} &lt;{user_email}&gt;</td>
          </tr>
          <tr style="border-bottom: 1px solid #242C3D;">
            <td style="color: #94A3B8;">User Account ID:</td>
            <td style="color: #94A3B8;">{user_id}</td>
          </tr>
          <tr style="border-bottom: 1px solid #242C3D;">
            <td style="color: #E56A10; font-weight: bold;">{category}</td>
          </tr>
          <tr style="border-bottom: 1px solid #242C3D;">
            <td style="color: #94A3B8;">Payment Type:</td>
            <td style="color: #FFFFFF;">{payment_type}</td>
          </tr>
          <tr style="border-bottom: 1px solid #242C3D;">
            <td style="color: #94A3B8;">Timestamp:</td>
            <td style="color: #94A3B8;">{created_at}</td>
          </tr>
        </table>

        <div style="background-color: #0B0E16; border: 1px solid #242C3D; border-radius: 8px; padding: 16px; margin-bottom: 24px;">
          <div style="font-size: 11px; font-family: monospace; text-transform: uppercase; color: #E56A10; margin-bottom: 8px; font-weight: bold;">User Description:</div>
          <div style="font-size: 13px; line-height: 1.6; color: #E2E8F0; white-space: pre-wrap;">{description}</div>
        </div>

        <a href="https://ametheus.gg/admin" style="display: inline-block; background-color: #E56A10; color: #FFFFFF; padding: 10px 20px; font-family: monospace; font-size: 12px; font-weight: bold; text-transform: uppercase; text-decoration: none; border-radius: 6px;">
          Open in Admin Cockpit &rarr;
        </a>
      </td>
    </tr>
  </table>
</body>
</html>
"""

    return await send_support_email(target_recipient, subject, text_body, html_body)


async def send_notification_email(
    recipient: str,
    user_name: str,
    title: str,
    message: str,
    link: Optional[str] = None,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Sends an automated tournament/match notification email to the user.
    Routes via the NO-REPLY identity (noreply@ametheus.games).
    Reply-To points to support@ametheus.games.
    """
    to_email_clean = (recipient or "").strip().lower()
    if not to_email_clean or "@" not in to_email_clean:
        return False, "Recipient email is invalid or missing"

    display_name = user_name or "Contender"
    subject = f"AMETHEUS Alert: {title}"

    destination_url = "https://ametheus.gg/dashboard"
    if link:
        if link.startswith("http"):
            destination_url = link
        else:
            destination_url = f"https://ametheus.gg{link if link.startswith('/') else '/' + link}"

    text_body = f"""Hello {display_name},

{title}

{message}

View details on your dashboard:
{destination_url}

—
AMETHEUS Esports Operations
https://ametheus.gg
"""

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{subject}</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0B0E16; margin: 0; padding: 24px; color: #F8FAFC;">
  <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 600px; margin: 0 auto; background-color: #111622; border-radius: 12px; border: 1px solid #242C3D; overflow: hidden; box-shadow: 0 4px 10px rgba(0,0,0,0.5);">
    <tr>
      <td style="background-color: #1A2234; padding: 20px 24px; border-bottom: 2px solid #E56A10;">
        <h1 style="color: #FFFFFF; margin: 0; font-size: 20px; font-weight: 800; letter-spacing: 1px;">AMETHEUS <span style="color: #E56A10;">ALERT</span></h1>
        <p style="color: #94A3B8; margin: 4px 0 0 0; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Official Competition Notification</p>
      </td>
    </tr>
    <tr>
      <td style="padding: 28px 24px;">
        <p style="margin: 0 0 16px 0; font-size: 14px; color: #94A3B8;">
          Hello <strong style="color: #FFFFFF;">{display_name}</strong>,
        </p>

        <h2 style="margin: 0 0 16px 0; font-size: 18px; color: #E56A10;">{title}</h2>

        <div style="background-color: #0B0E16; border-left: 3px solid #E56A10; padding: 16px; border-radius: 4px; margin-bottom: 24px;">
          <p style="margin: 0; font-size: 14px; line-height: 1.6; color: #E2E8F0; white-space: pre-wrap;">{message}</p>
        </div>

        <div style="text-align: center; margin: 28px 0 12px 0;">
          <a href="{destination_url}" style="display: inline-block; background-color: #E56A10; color: #FFFFFF; padding: 12px 28px; font-family: monospace; font-size: 13px; font-weight: bold; text-transform: uppercase; text-decoration: none; border-radius: 8px; letter-spacing: 0.5px;">
            Open in Cockpit &rarr;
          </a>
        </div>
      </td>
    </tr>
    <tr>
      <td style="background-color: #0B0E16; padding: 16px 24px; text-align: center; border-top: 1px solid #242C3D; font-size: 11px; color: #64748B;">
        &copy; 2026 AMETHEUS Esports Platform. All competitive integrity rights reserved.
      </td>
    </tr>
  </table>
</body>
</html>
"""

    return await send_noreply_email(to_email_clean, subject, text_body, html_body)


def render_official_announcement_email(
    subject: str,
    message: str,
    recipient_name: Optional[str] = None,
) -> tuple[str, str]:
    """
    Renders professional plain text and HTML templates for Official Ametheus communications.
    """
    name = (recipient_name or "Ametheus Member").strip()
    clean_msg = message.strip()

    text_body = f"""Ametheus Official Communication
===================================================

Dear {name},

{clean_msg}

--
AMETHEUS Esports Platform
Official Operations
https://ametheus.gg
"""

    html_message = clean_msg.replace("\n\n", "</p><p style='margin: 0 0 16px 0; font-size: 14px; line-height: 1.7; color: #E2E8F0;'>").replace("\n", "<br/>")

    html_body = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{subject}</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0B0E16; margin: 0; padding: 24px; color: #F8FAFC;">
  <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 620px; margin: 0 auto; background-color: #111622; border-radius: 12px; border: 1px solid #242C3D; overflow: hidden; box-shadow: 0 4px 14px rgba(0,0,0,0.6);">
    <tr>
      <td style="background-color: #1A2234; padding: 24px 28px; border-bottom: 2px solid #E56A10;">
        <h1 style="color: #FFFFFF; margin: 0; font-size: 20px; font-weight: 800; letter-spacing: 1px;">AMETHEUS <span style="color: #E56A10;">OFFICIAL</span></h1>
        <p style="color: #94A3B8; margin: 4px 0 0 0; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Official Platform Announcement</p>
      </td>
    </tr>
    <tr>
      <td style="padding: 32px 28px;">
        <p style="margin: 0 0 18px 0; font-size: 14px; color: #94A3B8;">
          Dear <strong style="color: #FFFFFF;">{name}</strong>,
        </p>

        <h2 style="margin: 0 0 20px 0; font-size: 18px; color: #E56A10; font-weight: 700;">{subject}</h2>

        <div style="background-color: #0B0E16; border-left: 3px solid #E56A10; padding: 20px; border-radius: 6px; margin-bottom: 28px;">
          <p style="margin: 0 0 16px 0; font-size: 14px; line-height: 1.7; color: #E2E8F0;">
            {html_message}
          </p>
        </div>

        <div style="text-align: center; margin: 28px 0 12px 0;">
          <a href="https://ametheus.gg" style="display: inline-block; background-color: #E56A10; color: #FFFFFF; padding: 12px 28px; font-family: monospace; font-size: 13px; font-weight: bold; text-transform: uppercase; text-decoration: none; border-radius: 8px; letter-spacing: 0.5px;">
            Visit Ametheus Platform &rarr;
          </a>
        </div>
      </td>
    </tr>
    <tr>
      <td style="background-color: #0B0E16; padding: 18px 28px; text-align: center; border-top: 1px solid #242C3D; font-size: 11px; color: #64748B;">
        Sent by Ametheus Official Communications &bull; official@ametheus.games<br/>
        &copy; 2026 AMETHEUS Esports Platform. All competitive integrity rights reserved.
      </td>
    </tr>
  </table>
</body>
</html>
"""
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
    Complies with Sections 34, 35, 36, and 37 of the Match Engine specification.
    """
    to_email_clean = (to_email or "").strip().lower()
    if not to_email_clean or "@" not in to_email_clean:
        return False, "Recipient email is invalid or missing"

    display_leader = leader_name or "Team Leader"
    action_title = "MATCH UPDATED" if is_update else "MATCH SCHEDULED"
    subject = f"AMETHEUS: {action_title} — {tournament_title} ({round_name})"

    destination_url = f"https://ametheus.gg/matches/{match_id}"

    # Build plain text body
    text_body = f"""Ametheus
----------------------------------------
{action_title}

Your team has been assigned a match in {tournament_title}.

Tournament: {tournament_title}
Game:       {game_name}
Round:      {round_name}
Match:      Match {match_number}
Your Team:  {team_name}
Opponent:   {opponent_name}
Date:       {scheduled_date}
Time:       {scheduled_time}
"""

    if is_update and previous_details:
        text_body += f"""
--- UPDATE DETAILS ---
Previous: {previous_details.get('opponent', 'TBD')} @ {previous_details.get('time', 'TBD')}
Updated:  {opponent_name} @ {scheduled_time}
"""

    text_body += f"""
View Match Details:
{destination_url}
"""

    if community_link:
        text_body += f"""
Tournament {community_platform or 'Community'} Link:
{community_link}
"""

    text_body += """
----------------------------------------
AMETHEUS Esports Operations
Competitive Integrity & Fair Play
https://ametheus.gg
"""

    # Build HTML body
    community_btn_html = ""
    if community_link:
        plat_label = (community_platform or "WhatsApp").upper()
        community_btn_html = f"""
        <div style="margin-top: 14px; text-align: center;">
          <a href="{community_link}" target="_blank" style="display: inline-block; background-color: #10B981; color: #FFFFFF; padding: 10px 22px; font-family: monospace; font-size: 12px; font-weight: bold; text-transform: uppercase; text-decoration: none; border-radius: 6px; letter-spacing: 0.5px;">
            💬 Join Tournament {plat_label} &rarr;
          </a>
        </div>
        """

    update_box_html = ""
    if is_update and previous_details:
        prev_opp = previous_details.get('opponent', 'TBD')
        prev_time = previous_details.get('time', 'TBD')
        update_box_html = f"""
        <div style="background-color: #1A1A24; border: 1px solid #F59E0B; border-radius: 6px; padding: 14px 18px; margin-bottom: 20px;">
          <p style="margin: 0 0 6px 0; font-size: 11px; text-transform: uppercase; font-weight: bold; color: #F59E0B; letter-spacing: 1px;">
            ⚠️ What Changed:
          </p>
          <p style="margin: 0; font-size: 13px; line-height: 1.5; color: #E2E8F0;">
            <strong style="color: #94A3B8;">Previous:</strong> {prev_opp} &bull; {prev_time}<br/>
            <strong style="color: #10B981;">Updated:</strong> {opponent_name} &bull; {scheduled_time}
          </p>
        </div>
        """

    badge_color = "#F59E0B" if is_update else "#E56A10"

    html_body = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{subject}</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #07090E; margin: 0; padding: 24px 12px; color: #F8FAFC;">
  <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 600px; margin: 0 auto; background-color: #111622; border-radius: 12px; border: 1px solid #242C3D; overflow: hidden; box-shadow: 0 8px 24px rgba(0,0,0,0.6);">
    <!-- Header -->
    <tr>
      <td style="background-color: #161D2C; padding: 22px 28px; border-bottom: 2px solid {badge_color};">
        <table width="100%" border="0" cellspacing="0" cellpadding="0">
          <tr>
            <td>
              <h1 style="color: #FFFFFF; margin: 0; font-size: 20px; font-weight: 900; letter-spacing: 1.5px; text-transform: uppercase;">
                AMETHEUS <span style="color: {badge_color};">MATCH ENGINE</span>
              </h1>
              <p style="color: #94A3B8; margin: 4px 0 0 0; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">
                Official Roster Notification
              </p>
            </td>
            <td align="right">
              <span style="display: inline-block; background-color: {badge_color}; color: #07090E; padding: 4px 10px; font-family: monospace; font-size: 11px; font-weight: 900; text-transform: uppercase; border-radius: 4px; letter-spacing: 1px;">
                {action_title}
              </span>
            </td>
          </tr>
        </table>
      </td>
    </tr>

    <!-- Body -->
    <tr>
      <td style="padding: 28px 28px 20px 28px;">
        <p style="margin: 0 0 16px 0; font-size: 14px; color: #94A3B8;">
          Hello <strong style="color: #FFFFFF;">{display_leader}</strong>,
        </p>

        <p style="margin: 0 0 20px 0; font-size: 15px; color: #E2E8F0; line-height: 1.5;">
          Your team <strong style="color: #FFFFFF;">{team_name}</strong> has a match confirmed in <strong style="color: {badge_color};">{tournament_title}</strong>.
        </p>

        {update_box_html}

        <!-- Match Specs Table -->
        <table width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color: #0B0E16; border: 1px solid #1E2738; border-radius: 8px; margin-bottom: 24px; overflow: hidden;">
          <tr>
            <td style="padding: 12px 16px; border-bottom: 1px solid #1E2738; color: #64748B; font-size: 12px; font-family: monospace; text-transform: uppercase; width: 35%;">Tournament</td>
            <td style="padding: 12px 16px; border-bottom: 1px solid #1E2738; color: #F8FAFC; font-size: 13px; font-weight: 700;">{tournament_title}</td>
          </tr>
          <tr>
            <td style="padding: 12px 16px; border-bottom: 1px solid #1E2738; color: #64748B; font-size: 12px; font-family: monospace; text-transform: uppercase;">Game</td>
            <td style="padding: 12px 16px; border-bottom: 1px solid #1E2738; color: #06B6D4; font-size: 13px; font-weight: 700; text-transform: uppercase;">{game_name}</td>
          </tr>
          <tr>
            <td style="padding: 12px 16px; border-bottom: 1px solid #1E2738; color: #64748B; font-size: 12px; font-family: monospace; text-transform: uppercase;">Round &amp; Match</td>
            <td style="padding: 12px 16px; border-bottom: 1px solid #1E2738; color: #F8FAFC; font-size: 13px; font-weight: 700;">{round_name} &bull; Match {match_number}</td>
          </tr>
          <tr>
            <td style="padding: 12px 16px; border-bottom: 1px solid #1E2738; color: #64748B; font-size: 12px; font-family: monospace; text-transform: uppercase;">Your Team</td>
            <td style="padding: 12px 16px; border-bottom: 1px solid #1E2738; color: #10B981; font-size: 13px; font-weight: 800;">{team_name}</td>
          </tr>
          <tr>
            <td style="padding: 12px 16px; border-bottom: 1px solid #1E2738; color: #64748B; font-size: 12px; font-family: monospace; text-transform: uppercase;">Opponent</td>
            <td style="padding: 12px 16px; border-bottom: 1px solid #1E2738; color: #EF4444; font-size: 13px; font-weight: 800;">{opponent_name}</td>
          </tr>
          <tr>
            <td style="padding: 12px 16px; color: #64748B; font-size: 12px; font-family: monospace; text-transform: uppercase;">Date &amp; Time</td>
            <td style="padding: 12px 16px; color: #F59E0B; font-size: 13px; font-weight: 700;">{scheduled_date} &bull; {scheduled_time}</td>
          </tr>
        </table>

        <!-- Primary CTA Button -->
        <div style="text-align: center; margin: 24px 0 8px 0;">
          <a href="{destination_url}" style="display: inline-block; background-color: {badge_color}; color: #07090E; padding: 13px 32px; font-family: monospace; font-size: 13px; font-weight: 900; text-transform: uppercase; text-decoration: none; border-radius: 6px; letter-spacing: 1px; box-shadow: 0 4px 12px rgba(229,106,16,0.3);">
            [ VIEW MATCH DETAILS &rarr; ]
          </a>
        </div>

        {community_btn_html}
      </td>
    </tr>

    <!-- Footer -->
    <tr>
      <td style="background-color: #0B0E16; padding: 20px 28px; text-align: center; border-top: 1px solid #1E2738; font-size: 11px; color: #64748B; line-height: 1.6;">
        Competitive integrity &bull; Real-time tournament telemetry<br/>
        &copy; 2026 AMETHEUS Esports Platform. All rights reserved.
      </td>
    </tr>
  </table>
</body>
</html>
"""

    return await send_noreply_email(to_email_clean, subject, text_body, html_body)

