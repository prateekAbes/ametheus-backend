import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, Tuple
from app.core.config import settings

logger = logging.getLogger("ametheus.email")


def _send_smtp_email(to_email: str, subject: str, text_content: str, html_content: str) -> Tuple[bool, Optional[str]]:
    """Synchronous SMTP email delivery function executed in a background thread."""
    to_email_clean = (to_email or "").strip().lower()
    if not to_email_clean or "@" not in to_email_clean:
        logger.warning(f"[Ametheus Email] Invalid recipient email address: '{to_email}'")
        return False, f"Invalid recipient email address: '{to_email}'"

    if not settings.SMTP_HOST:
        logger.warning(
            f"[Ametheus Email] SMTP not configured in environment (SMTP_HOST missing). Cannot deliver email to <{to_email_clean}> | Subject: '{subject}'"
        )
        return False, "SMTP service not configured in environment (SMTP_HOST is not set)"

    server = None
    try:
        # Determine sender address
        sender_email = settings.SMTP_FROM_EMAIL or "support@ametheus.gg"
        if settings.SMTP_USER and "@" in settings.SMTP_USER:
            sender_email = settings.SMTP_USER.strip()

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{settings.SMTP_FROM_NAME} <{sender_email}>"
        msg["To"] = to_email_clean

        part1 = MIMEText(text_content, "plain", "utf-8")
        part2 = MIMEText(html_content, "html", "utf-8")
        msg.attach(part1)
        msg.attach(part2)

        port = int(settings.SMTP_PORT or 587)
        host = settings.SMTP_HOST.strip()

        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=12)
        else:
            server = smtplib.SMTP(host, port, timeout=12)
            server.ehlo()
            if port in (587, 25):
                server.starttls()
                server.ehlo()

        if settings.SMTP_USER and settings.SMTP_PASSWORD:
            server.login(settings.SMTP_USER.strip(), settings.SMTP_PASSWORD.strip())

        server.sendmail(sender_email, [to_email_clean], msg.as_string())
        logger.info(f"[Ametheus Email] Successfully delivered email to <{to_email_clean}> | Subject: '{subject}'")
        return True, None
    except Exception as e:
        err_msg = str(e)
        logger.error(f"[Ametheus Email] Failed to send email to <{to_email_clean}>: {err_msg}", exc_info=True)
        return False, err_msg
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass


async def send_ticket_confirmation_email(ticket: dict) -> Tuple[bool, Optional[str]]:
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

What Happens Next:
Our certified arbiters and support engineers are reviewing your ticket. You will receive updates directly via this email address. Please quote your Ticket ID ({ticket_id}) in any future correspondence regarding this matter.

Sincerely,
Ametheus Esports Integrity & Operations Desk
https://ametheus.gg
"""

    html_body = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{subject}</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #F8FAFC; margin: 0; padding: 24px; color: #1E293B;">
  <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 600px; margin: 0 auto; background-color: #FFFFFF; border-radius: 12px; border: 1px solid #E2E8F0; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);">
    <tr>
      <td style="background-color: #0B0E16; padding: 24px; text-align: center; border-bottom: 3px solid #E56A10;">
        <h1 style="color: #FFFFFF; margin: 0; font-size: 22px; font-weight: 800; letter-spacing: 1px;">AMETHEUS <span style="color: #E56A10;">SUPPORT</span></h1>
        <p style="color: #94A3B8; margin: 4px 0 0 0; font-size: 11px; text-transform: uppercase; letter-spacing: 2px;">Official Integrity &amp; Operations Desk</p>
      </td>
    </tr>
    <tr>
      <td style="padding: 32px 24px;">
        <h2 style="margin: 0 0 16px 0; font-size: 18px; color: #0F172A;">Support Request Received</h2>
        <p style="margin: 0 0 20px 0; font-size: 14px; line-height: 1.6; color: #475569;">
          Hello <strong>{user_name}</strong>, thank you for reaching out to Ametheus Support. Your request has been logged and assigned to our live operations queue.
        </p>

        <div style="background-color: #F1F5F9; border-radius: 8px; padding: 16px; margin-bottom: 24px; text-align: center; border: 1px solid #CBD5E1;">
          <div style="font-size: 11px; font-family: monospace; text-transform: uppercase; color: #64748B; letter-spacing: 1px; margin-bottom: 4px;">Official Ticket Reference ID</div>
          <div style="font-size: 24px; font-family: monospace; font-weight: 800; color: #E56A10; letter-spacing: 2px;">{ticket_id}</div>
        </div>

        <table width="100%" border="0" cellspacing="0" cellpadding="0" style="font-size: 13px; margin-bottom: 24px; border-collapse: collapse;">
          <tr style="border-bottom: 1px solid #E2E8F0;">
            <td style="padding: 8px; color: #64748B; width: 140px;">Category:</td>
            <td style="padding: 8px; font-weight: bold; color: #0F172A;">{category}</td>
          </tr>
          {payment_line_html}
          <tr style="border-bottom: 1px solid #E2E8F0;">
            <td style="padding: 8px; color: #64748B;">Date &amp; Time:</td>
            <td style="padding: 8px; font-weight: bold; color: #0F172A;">{created_at}</td>
          </tr>
        </table>

        <div style="margin-bottom: 24px;">
          <div style="font-size: 12px; font-weight: bold; text-transform: uppercase; color: #64748B; margin-bottom: 8px;">Issue Description:</div>
          <div style="background-color: #F8FAFC; border-left: 3px solid #E56A10; padding: 12px 16px; font-size: 13px; line-height: 1.6; color: #334155; white-space: pre-wrap;">{description}</div>
        </div>

        <p style="margin: 0; font-size: 13px; line-height: 1.6; color: #64748B;">
          Please retain this email and reference <strong>{ticket_id}</strong> in any future communication with our support team.
        </p>
      </td>
    </tr>
    <tr>
      <td style="background-color: #F8FAFC; padding: 16px 24px; text-align: center; border-top: 1px solid #E2E8F0; font-size: 11px; color: #94A3B8;">
        &copy; 2026 AMETHEUS Esports Platform. All competitive rights reserved.
      </td>
    </tr>
  </table>
</body>
</html>
"""

    return await asyncio.to_thread(_send_smtp_email, recipient, subject, text_body, html_body)


async def send_admin_ticket_notification_email(ticket: dict) -> Tuple[bool, Optional[str]]:
    """Sends notification to ametheus.gg@gmail.com with complete ticket information."""
    ticket_id = ticket.get("ticket_id") or ticket.get("id") or "AMT-000000"
    user_name = ticket.get("name", "Unknown User")
    user_email = ticket.get("email", "unknown@email.com")
    user_id = ticket.get("user_id") or "Guest (Unauthenticated)"
    category = ticket.get("category", "General Inquiry").replace("_", " ").title()
    payment_type = ticket.get("payment_type", "N/A")
    description = ticket.get("description", "")
    created_at = ticket.get("created_at", "Just now")
    admin_recipient = settings.SUPPORT_ADMIN_EMAIL or "ametheus.gg@gmail.com"

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

    return await asyncio.to_thread(_send_smtp_email, admin_recipient, subject, text_body, html_body)
