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
from app.schemas.email_settings import SendTestEmailResponse, SmtpStatusResponse
from app.services.email_config_service import (
    get_automated_email,
    get_support_email,
    get_official_email,
    DEFAULT_AUTOMATED_EMAIL,
    DEFAULT_SUPPORT_EMAIL,
)

logger = logging.getLogger("ametheus.email")


def _send_smtp_email(
    to_email: str,
    subject: str,
    text_content: str,
    html_content: str,
    sender_email: Optional[str] = None,
    sender_name: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> Tuple[bool, Optional[str], Optional[str], Optional[str], str]:
    """
    Synchronous SMTP email delivery function executed in a background thread.
    Returns:
        (success: bool, error_code: Optional[str], error_details: Optional[str], message_id: Optional[str], sender_used: str)
    """
    to_email_clean = (to_email or "").strip().lower()
    if not to_email_clean or "@" not in to_email_clean:
        logger.warning(f"[Ametheus Email] Invalid recipient email address: '{to_email}'")
        return False, "RECIPIENT_REJECTED", f"Invalid recipient email address: '{to_email}'", None, sender_email or DEFAULT_AUTOMATED_EMAIL

    if not settings.SMTP_HOST:
        logger.warning(
            f"[Ametheus Email] SMTP not configured in environment (SMTP_HOST missing). Cannot deliver email to <{to_email_clean}> | Subject: '{subject}'"
        )
        return False, "SMTP_NOT_CONFIGURED", "SMTP service not configured in environment (SMTP_HOST is not set)", None, sender_email or DEFAULT_AUTOMATED_EMAIL

    # Determine desired sender address and display name
    from_addr = (sender_email or settings.SMTP_FROM_EMAIL or DEFAULT_AUTOMATED_EMAIL).strip()
    from_name = (sender_name or settings.SMTP_FROM_NAME or "Ametheus").strip()

    msg_id = f"AMT-{uuid.uuid4().hex[:12].upper()}@ametheus.gg"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_addr}>"
    msg["To"] = to_email_clean
    msg["Message-ID"] = f"<{msg_id}>"

    if reply_to:
        msg["Reply-To"] = reply_to
    elif settings.SUPPORT_EMAIL:
        msg["Reply-To"] = settings.SUPPORT_EMAIL

    part1 = MIMEText(text_content, "plain", "utf-8")
    part2 = MIMEText(html_content, "html", "utf-8")
    msg.attach(part1)
    msg.attach(part2)

    port = int(settings.SMTP_PORT or 587)
    host = settings.SMTP_HOST.strip()
    is_ssl = (port == 465) or getattr(settings, "SMTP_SECURE", False)

    server = None
    envelope_sender = from_addr
    sender_used = from_addr

    try:
        if is_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=12)
        else:
            server = smtplib.SMTP(host, port, timeout=12)
            server.ehlo()
            if port in (587, 25) or getattr(settings, "SMTP_SECURE", False):
                server.starttls()
                server.ehlo()

        if settings.SMTP_USER and settings.SMTP_PASSWORD:
            server.login(settings.SMTP_USER.strip(), settings.SMTP_PASSWORD.strip())

        # Attempt delivery with configured sender identity
        try:
            server.sendmail(envelope_sender, [to_email_clean], msg.as_string())
            logger.info(f"[Ametheus Email] Successfully delivered email to <{to_email_clean}> from <{sender_used}> | Subject: '{subject}'")
            return True, None, None, msg_id, sender_used
        except (smtplib.SMTPSenderRefused, smtplib.SMTPResponseException) as sender_err:
            # Check for sender mismatch restriction (e.g. Gmail / strict relays requiring envelope = authenticated user)
            is_sender_refusal = (
                isinstance(sender_err, smtplib.SMTPSenderRefused)
                or (getattr(sender_err, "smtp_code", None) in (530, 550, 553, 554))
            )
            auth_account = (settings.SMTP_USER or "").strip()

            if is_sender_refusal and auth_account and "@" in auth_account and envelope_sender.lower() != auth_account.lower():
                logger.warning(
                    f"[Ametheus Email] SMTP provider rejected envelope sender <{envelope_sender}>: {sender_err}. "
                    f"Retrying with authenticated account <{auth_account}> for envelope delivery while preserving From header."
                )
                envelope_sender = auth_account
                sender_used = f"{from_addr} (via {auth_account})"
                server.sendmail(envelope_sender, [to_email_clean], msg.as_string())
                logger.info(f"[Ametheus Email] Successfully delivered email to <{to_email_clean}> using envelope fallback <{auth_account}> | Subject: '{subject}'")
                return True, None, None, msg_id, sender_used
            else:
                raise sender_err

    except smtplib.SMTPAuthenticationError as e:
        err_msg = f"SMTP authentication failed: {e.smtp_error.decode(errors='replace') if isinstance(e.smtp_error, bytes) else str(e)}"
        logger.error(f"[Ametheus Email] Authentication failed on {host}:{port}: {err_msg}")
        return False, "SMTP_AUTHENTICATION_FAILED", "SMTP authentication failed. Verify username and password / App Password.", None, sender_used

    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, ConnectionRefusedError, socket.gaierror, socket.error, TimeoutError) as e:
        err_msg = f"SMTP connection error: {e}"
        logger.error(f"[Ametheus Email] Connection failed on {host}:{port}: {err_msg}")
        return False, "SMTP_CONNECTION_FAILED", "Failed to connect to SMTP host. Connection timed out or server unreachable.", None, sender_used

    except smtplib.SMTPRecipientsRefused as e:
        err_msg = f"Recipient rejected: {e}"
        logger.error(f"[Ametheus Email] Recipient rejected: {err_msg}")
        return False, "RECIPIENT_REJECTED", f"Recipient address rejected by mail server: {to_email_clean}", None, sender_used

    except smtplib.SMTPSenderRefused as e:
        err_msg = f"Sender rejected: {e}"
        logger.error(f"[Ametheus Email] Sender rejected: {err_msg}")
        return False, "INVALID_SENDER", f"Sender address rejected by mail server: {envelope_sender}", None, sender_used

    except Exception as e:
        err_msg = str(e)
        logger.error(f"[Ametheus Email] Failed to send email to <{to_email_clean}>: {err_msg}", exc_info=True)
        return False, "MESSAGE_SEND_FAILED", f"Failed to deliver email: {err_msg}", None, sender_used

    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass


def _check_smtp_health_sync() -> Dict[str, Any]:
    """Synchronous test of SMTP connectivity and authentication."""
    if not settings.SMTP_HOST:
        return {
            "configured": False,
            "connected": False,
            "authenticated": False,
            "status_code": "SMTP_NOT_CONFIGURED",
            "message": "SMTP host is not configured in environment variables.",
            "host": None,
            "port": int(settings.SMTP_PORT or 587),
            "secure": bool(getattr(settings, "SMTP_SECURE", False)),
        }

    host = settings.SMTP_HOST.strip()
    port = int(settings.SMTP_PORT or 587)
    is_ssl = (port == 465) or getattr(settings, "SMTP_SECURE", False)

    server = None
    try:
        if is_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=10)
        else:
            server = smtplib.SMTP(host, port, timeout=10)
            server.ehlo()
            if port in (587, 25) or getattr(settings, "SMTP_SECURE", False):
                server.starttls()
                server.ehlo()

        authenticated = False
        if settings.SMTP_USER and settings.SMTP_PASSWORD:
            server.login(settings.SMTP_USER.strip(), settings.SMTP_PASSWORD.strip())
            authenticated = True

        return {
            "configured": True,
            "connected": True,
            "authenticated": authenticated,
            "status_code": "CONNECTED",
            "message": "SMTP server is connected and authenticated successfully.",
            "host": host,
            "port": port,
            "secure": is_ssl,
        }

    except smtplib.SMTPAuthenticationError:
        return {
            "configured": True,
            "connected": True,
            "authenticated": False,
            "status_code": "SMTP_AUTHENTICATION_FAILED",
            "message": "SMTP authentication failed. Verify SMTP username and password / App Password.",
            "host": host,
            "port": port,
            "secure": is_ssl,
        }

    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, ConnectionRefusedError, socket.gaierror, socket.error, TimeoutError) as e:
        return {
            "configured": True,
            "connected": False,
            "authenticated": False,
            "status_code": "SMTP_CONNECTION_FAILED",
            "message": f"Connection failed to {host}:{port}. Verify host address, port, and firewall rules.",
            "host": host,
            "port": port,
            "secure": is_ssl,
        }

    except Exception as e:
        return {
            "configured": True,
            "connected": False,
            "authenticated": False,
            "status_code": "CONNECTION_FAILED",
            "message": f"SMTP health check failed: {str(e)}",
            "host": host,
            "port": port,
            "secure": is_ssl,
        }

    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass


async def check_smtp_health(db: Optional[AsyncIOMotorDatabase] = None) -> SmtpStatusResponse:
    """Asynchronous wrapper for live SMTP health check."""
    status_dict = await asyncio.to_thread(_check_smtp_health_sync)
    sender = await get_automated_email(db)

    return SmtpStatusResponse(
        configured=status_dict["configured"],
        connected=status_dict["connected"],
        authenticated=status_dict["authenticated"],
        sender=sender,
        host=status_dict["host"],
        port=status_dict["port"],
        secure=status_dict["secure"],
        status_code=status_dict["status_code"],
        message=status_dict["message"],
    )


async def send_test_email(to_email: str, db: Optional[AsyncIOMotorDatabase] = None) -> SendTestEmailResponse:
    """Sends a verification test email from the configured Automated Email to the specified destination."""
    sender_addr = await get_automated_email(db)
    subject = "Ametheus SMTP Test Email"

    text_body = f"""Ametheus Esports Platform — SMTP Diagnostic Test
===================================================

This is an automated test message sent from the Ametheus Admin Cockpit.
If you are reading this email, your SMTP configuration and sender identity are working correctly.

Configured Automated Sender: {sender_addr}
Destination Recipient: {to_email}
Environment Host: {settings.SMTP_HOST or 'Not Configured'}
Port: {settings.SMTP_PORT}

Competitive Integrity & Operations:
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
        <h1 style="color: #FFFFFF; margin: 0; font-size: 20px; font-weight: 800; letter-spacing: 1px;">AMETHEUS <span style="color: #E56A10;">SMTP TEST</span></h1>
        <p style="color: #94A3B8; margin: 4px 0 0 0; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Email Subsystem Verification</p>
      </td>
    </tr>
    <tr>
      <td style="padding: 28px 24px;">
        <div style="background-color: #064E3B; border: 1px solid #059669; border-radius: 8px; padding: 14px 16px; margin-bottom: 20px; color: #A7F3D0; font-family: monospace; font-size: 12px; font-weight: bold;">
          &#10004; SUCCESS: SMTP relay connection &amp; email delivery confirmed!
        </div>

        <p style="margin: 0 0 16px 0; font-size: 13px; color: #CBD5E1; line-height: 1.6;">
          This is an official test email generated by the Super Admin from the <strong>Ametheus Email Settings Cockpit</strong>.
        </p>

        <table width="100%" border="0" cellspacing="0" cellpadding="8" style="font-size: 12px; font-family: monospace; background-color: #0B0E16; border-radius: 8px; border: 1px solid #242C3D; margin-bottom: 20px;">
          <tr style="border-bottom: 1px solid #242C3D;">
            <td style="color: #94A3B8; width: 150px;">Automated Sender:</td>
            <td style="color: #E56A10; font-weight: bold;">{sender_addr}</td>
          </tr>
          <tr style="border-bottom: 1px solid #242C3D;">
            <td style="color: #94A3B8;">Destination:</td>
            <td style="color: #FFFFFF;">{to_email}</td>
          </tr>
          <tr style="border-bottom: 1px solid #242C3D;">
            <td style="color: #94A3B8;">SMTP Host:</td>
            <td style="color: #94A3B8;">{settings.SMTP_HOST or 'N/A'}</td>
          </tr>
          <tr>
            <td style="color: #94A3B8;">SMTP Port:</td>
            <td style="color: #94A3B8;">{settings.SMTP_PORT}</td>
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
        _send_smtp_email,
        to_email,
        subject,
        text_body,
        html_body,
        sender_addr,
        "Ametheus System Test",
    )

    return SendTestEmailResponse(
        success=ok,
        message_id=msg_id,
        error_code=err_code,
        error_details=err_det,
        sender_used=sender_used,
    )


async def send_ticket_confirmation_email(ticket: dict, db: Optional[AsyncIOMotorDatabase] = None) -> Tuple[bool, Optional[str]]:
    """Sends confirmation email to the user with their Ticket ID and issue summary."""
    ticket_id = ticket.get("ticket_id") or ticket.get("id") or "AMT-000000"
    user_name = ticket.get("name", "Contender")
    category = ticket.get("category", "General Inquiry").replace("_", " ").title()
    payment_type = ticket.get("payment_type")
    description = ticket.get("description", "")
    created_at = ticket.get("created_at", "Just now")
    recipient = (ticket.get("email") or "").strip().lower()

    if not recipient:
        return False, "Recipient email is missing"

    sender_addr = await get_automated_email(db)
    support_addr = await get_support_email(db)

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

    ok, err_code, err_det, msg_id, _ = await asyncio.to_thread(
        _send_smtp_email,
        recipient,
        subject,
        text_body,
        html_body,
        sender_addr,
        "Ametheus Support Operations",
        support_addr,
    )
    return ok, err_det


async def send_admin_ticket_notification_email(
    ticket: dict,
    admin_recipient: Optional[str] = None,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> Tuple[bool, Optional[str]]:
    """Sends notification to the configured support email with complete ticket information."""
    ticket_id = ticket.get("ticket_id") or ticket.get("id") or "AMT-000000"
    user_name = ticket.get("name", "Unknown User")
    user_email = ticket.get("email", "unknown@email.com")
    user_id = ticket.get("user_id") or "Guest (Unauthenticated)"
    category = ticket.get("category", "General Inquiry").replace("_", " ").title()
    payment_type = ticket.get("payment_type", "N/A")
    description = ticket.get("description", "")
    created_at = ticket.get("created_at", "Just now")

    target_recipient = admin_recipient or await get_support_email(db)
    sender_addr = await get_automated_email(db)

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
            <td style="color: #94A3B8;">Category:</td>
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

    ok, err_code, err_det, _, _ = await asyncio.to_thread(
        _send_smtp_email,
        target_recipient,
        subject,
        text_body,
        html_body,
        sender_addr,
        "Ametheus Support Operations",
    )
    return ok, err_det


async def send_notification_email(
    recipient: str,
    user_name: str,
    title: str,
    message: str,
    link: Optional[str] = None,
    db: Optional[AsyncIOMotorDatabase] = None,
) -> Tuple[bool, Optional[str]]:
    """Sends an important website notification email to the user's registered email address."""
    to_email_clean = (recipient or "").strip().lower()
    if not to_email_clean or "@" not in to_email_clean:
        return False, "Recipient email is invalid or missing"

    sender_addr = await get_automated_email(db)
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

    ok, err_code, err_det, _, _ = await asyncio.to_thread(
        _send_smtp_email,
        to_email_clean,
        subject,
        text_body,
        html_body,
        sender_addr,
        "Ametheus Alert",
    )
    return ok, err_det
