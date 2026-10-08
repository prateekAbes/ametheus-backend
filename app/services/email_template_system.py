"""
Ametheus Central Transactional Email Design System
==================================================
Provides unified, premium, email-client-safe templates for all transactional
and operational emails across the Ametheus Esports Platform.

Visual Language:
- Deep dark palette matching the Ametheus web application:
  - Canvas background: #080A10
  - Surface card: #111624
  - Border: #1E2638
  - Refined accent crimson: #E11D48
  - Refined accent gold: #D97706
  - Refined accent emerald: #10B981
  - Text primary: #FFFFFF
  - Text secondary: #CBD5E1
  - Text muted / metadata: #8A94A6
- Email-safe table layouts with inline CSS (Gmail, Apple Mail, Outlook, iOS, Android).
- Contextual badges and descriptive action buttons (no generic "AMETHEUS ALERT" or "OPEN IN COCKPIT ->").
- Human, direct, professional copy.
- Comprehensive companion plain-text formatting for every email.
"""

from typing import Dict, List, Optional, Tuple
from app.core.config import settings

# Base site domain
BASE_URL = getattr(settings, "FRONTEND_URL", "https://www.ametheus.games").rstrip("/")

# Brand Color Tokens
COLOR_CANVAS = "#080A10"
COLOR_SURFACE = "#111624"
COLOR_SURFACE_CARD = "#0B0F1A"
COLOR_BORDER = "#1E2638"
COLOR_BORDER_SUBTLE = "#161D2B"
COLOR_TEXT_WHITE = "#FFFFFF"
COLOR_TEXT_PRIMARY = "#E2E8F0"
COLOR_TEXT_SECONDARY = "#94A3B8"
COLOR_TEXT_MUTED = "#64748B"

# Accents
ACCENT_CRIMSON = "#E11D48"
ACCENT_CRIMSON_BG = "rgba(225, 29, 72, 0.12)"
ACCENT_GOLD = "#D97706"
ACCENT_GOLD_BG = "rgba(217, 119, 6, 0.12)"
ACCENT_EMERALD = "#10B981"
ACCENT_EMERALD_BG = "rgba(16, 185, 129, 0.12)"
ACCENT_SKY = "#38BDF8"
ACCENT_SKY_BG = "rgba(56, 189, 248, 0.12)"


def _escape_html(text: Optional[str]) -> str:
    """Escapes HTML entities safely."""
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def render_email_shell(
    subject: str,
    badge_text: str,
    title: str,
    content_html: str,
    greeting_name: Optional[str] = None,
    badge_color: str = ACCENT_CRIMSON,
    cta_text: Optional[str] = None,
    cta_url: Optional[str] = None,
    secondary_cta_text: Optional[str] = None,
    secondary_cta_url: Optional[str] = None,
    guidance_note: Optional[str] = None,
    footer_sender_line: Optional[str] = None,
) -> str:
    """
    Renders the unified, responsive Ametheus transactional email wrapper.
    Full compatibility across web, desktop, and mobile email clients.
    """
    # Greeting markup
    greeting_html = ""
    if greeting_name and greeting_name.strip():
        safe_name = _escape_html(greeting_name.strip())
        greeting_html = f"""
        <p style="margin: 0 0 16px 0; font-size: 15px; font-weight: 600; color: {COLOR_TEXT_WHITE}; line-height: 1.5;">
          Hello {safe_name},
        </p>
        """

    # Primary and secondary buttons
    cta_block_html = ""
    if cta_text and cta_url:
        safe_cta_text = _escape_html(cta_text)
        safe_cta_url = _escape_html(cta_url)

        secondary_btn_html = ""
        if secondary_cta_text and secondary_cta_url:
            safe_sec_text = _escape_html(secondary_cta_text)
            safe_sec_url = _escape_html(secondary_cta_url)
            secondary_btn_html = f"""
            <td align="center" style="padding-left: 12px;">
              <a href="{safe_sec_url}" target="_blank" style="display: inline-block; background-color: #1A2234; color: #CBD5E1; padding: 12px 24px; font-size: 13px; font-weight: 600; text-decoration: none; border-radius: 6px; border: 1px solid #28354D; white-space: nowrap;">
                {safe_sec_text}
              </a>
            </td>
            """

        cta_block_html = f"""
        <table border="0" cellspacing="0" cellpadding="0" style="margin: 28px 0 12px 0; width: 100%;">
          <tr>
            <td align="left">
              <table border="0" cellspacing="0" cellpadding="0">
                <tr>
                  <td align="center" style="border-radius: 6px; background-color: {badge_color};">
                    <a href="{safe_cta_url}" target="_blank" style="display: inline-block; padding: 12px 28px; font-size: 13px; font-weight: 700; color: #FFFFFF; text-decoration: none; border-radius: 6px; letter-spacing: 0.3px; white-space: nowrap;">
                      {safe_cta_text} &rarr;
                    </a>
                  </td>
                  {secondary_btn_html}
                </tr>
              </table>
            </td>
          </tr>
        </table>
        """

    # Guidance note (e.g. "Need to respond? Reply directly to this email...")
    guidance_html = ""
    if guidance_note:
        guidance_html = f"""
        <p style="margin: 20px 0 0 0; font-size: 13px; line-height: 1.6; color: {COLOR_TEXT_SECONDARY}; border-top: 1px solid {COLOR_BORDER_SUBTLE}; padding-top: 16px;">
          {guidance_note}
        </p>
        """

    sender_line_html = footer_sender_line or f"Sent by Ametheus &bull; {settings.SUPPORT_EMAIL}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{_escape_html(subject)}</title>
</head>
<body style="margin: 0; padding: 32px 16px; background-color: {COLOR_CANVAS}; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased; color: {COLOR_TEXT_PRIMARY};">
  <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width: 600px; margin: 0 auto; background-color: {COLOR_SURFACE}; border: 1px solid {COLOR_BORDER}; border-radius: 8px; overflow: hidden; box-shadow: 0 8px 30px rgba(0, 0, 0, 0.55);">
    
    <!-- Top Brand Accent Bar -->
    <tr>
      <td height="3" style="background-color: {badge_color}; line-height: 3px; font-size: 3px;">&nbsp;</td>
    </tr>

    <!-- Header: Wordmark + Context Badge -->
    <tr>
      <td style="padding: 24px 28px; border-bottom: 1px solid {COLOR_BORDER}; background-color: #0E1322;">
        <table width="100%" border="0" cellspacing="0" cellpadding="0">
          <tr>
            <td valign="middle" align="left">
              <span style="font-size: 16px; font-weight: 800; letter-spacing: 2.2px; color: {COLOR_TEXT_WHITE}; text-transform: uppercase; display: inline-block;">
                AMETHEUS
              </span>
            </td>
            <td valign="middle" align="right">
              <span style="display: inline-block; background-color: #161D2E; color: {badge_color}; border: 1px solid {badge_color}; font-size: 10px; font-weight: 700; letter-spacing: 1.2px; text-transform: uppercase; padding: 4px 10px; border-radius: 4px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;">
                {_escape_html(badge_text)}
              </span>
            </td>
          </tr>
        </table>
      </td>
    </tr>

    <!-- Body Content Area -->
    <tr>
      <td style="padding: 32px 28px 28px 28px;">
        <!-- Contextual Title -->
        <h1 style="margin: 0 0 18px 0; font-size: 20px; font-weight: 700; color: {COLOR_TEXT_WHITE}; letter-spacing: -0.3px; line-height: 1.35;">
          {_escape_html(title)}
        </h1>

        {greeting_html}

        {content_html}

        {cta_block_html}

        {guidance_html}
      </td>
    </tr>

    <!-- Footer -->
    <tr>
      <td style="background-color: #090C15; padding: 24px 28px; border-top: 1px solid {COLOR_BORDER}; text-align: center;">
        <p style="margin: 0 0 8px 0; font-size: 12px; font-weight: 600; color: #738096; letter-spacing: 0.5px;">
          AMETHEUS ESPORTS PLATFORM &bull; COMPETITIVE GAMING INFRASTRUCTURE
        </p>
        <p style="margin: 0 0 14px 0; font-size: 11px; color: {COLOR_TEXT_MUTED};">
          <a href="{BASE_URL}" style="color: #8C99AE; text-decoration: none; margin: 0 8px;">Website</a> &bull;
          <a href="{BASE_URL}/support" style="color: #8C99AE; text-decoration: none; margin: 0 8px;">Support Desk</a> &bull;
          <a href="{BASE_URL}/terms" style="color: #8C99AE; text-decoration: none; margin: 0 8px;">Terms</a> &bull;
          <a href="{BASE_URL}/privacy" style="color: #8C99AE; text-decoration: none; margin: 0 8px;">Privacy</a>
        </p>
        <p style="margin: 0; font-size: 11px; line-height: 1.6; color: {COLOR_TEXT_MUTED};">
          {sender_line_html}<br/>
          &copy; 2026 Ametheus. All competitive integrity rights reserved.
        </p>
      </td>
    </tr>

  </table>
</body>
</html>"""


def render_data_table(rows: List[Tuple[str, str, Optional[str]]]) -> str:
    """
    Renders an email-safe key-value table.
    Each item is (label, value, optional_color).
    """
    tr_items = []
    for idx, item in enumerate(rows):
        label, value, color = item[0], item[1], item[2] if len(item) > 2 else None
        val_color = color or COLOR_TEXT_WHITE
        border_style = f"border-bottom: 1px solid {COLOR_BORDER_SUBTLE};" if idx < len(rows) - 1 else ""

        tr_items.append(f"""
        <tr>
          <td style="padding: 11px 16px; color: {COLOR_TEXT_SECONDARY}; font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px; font-weight: 600; width: 140px; {border_style}">
            {_escape_html(label)}
          </td>
          <td style="padding: 11px 16px; color: {val_color}; font-size: 13px; font-weight: 600; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; {border_style}">
            {_escape_html(value)}
          </td>
        </tr>
        """)

    rows_html = "".join(tr_items)
    return f"""
    <table width="100%" border="0" cellspacing="0" cellpadding="0" style="background-color: {COLOR_SURFACE_CARD}; border: 1px solid {COLOR_BORDER}; border-radius: 6px; margin: 18px 0; overflow: hidden;">
      {rows_html}
    </table>
    """


def render_message_card(
    message: str,
    header_label: Optional[str] = None,
    accent_color: str = ACCENT_CRIMSON,
) -> str:
    """Renders a clean quote / message block."""
    header_html = ""
    if header_label:
        header_html = f"""
        <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: {accent_color}; font-weight: 700; margin-bottom: 8px;">
          {_escape_html(header_label)}
        </div>
        """

    safe_content = _escape_html(message.strip()).replace("\n\n", "</p><p style='margin: 8px 0 0 0; line-height: 1.6;'>").replace("\n", "<br/>")

    return f"""
    <div style="background-color: {COLOR_SURFACE_CARD}; border: 1px solid {COLOR_BORDER}; border-left: 3px solid {accent_color}; border-radius: 4px; padding: 16px 20px; margin: 18px 0;">
      {header_html}
      <div style="font-size: 13px; line-height: 1.6; color: {COLOR_TEXT_PRIMARY};">
        <p style="margin: 0; line-height: 1.6;">{safe_content}</p>
      </div>
    </div>
    """


# =========================================================================
# SPECIALIZED EMAIL BUILDERS
# =========================================================================

def build_support_ticket_confirmation_email(ticket: dict) -> Tuple[str, str, str]:
    """
    Renders receipt confirmation when a user submits a support ticket.
    Sender: support@ametheus.games
    Reply-To: support@ametheus.games
    """
    ticket_id = ticket.get("ticket_id") or ticket.get("id") or "AMT-000000"
    user_name = ticket.get("name", "Contender")
    category = ticket.get("category", "General Inquiry").replace("_", " ").title()
    description = (ticket.get("description") or "").strip()
    status_raw = ticket.get("status", "WAITING_FOR_ADMIN")

    # Clean display status
    from app.services.email_service import format_friendly_ticket_status
    friendly_status = format_friendly_ticket_status(status_raw)

    subject = f"Support Request Received [#{ticket_id}] — {category}"
    title = f"We've received your support request"

    data_rows = [
        ("Ticket ID", f"#{ticket_id}", ACCENT_SKY),
        ("Status", friendly_status, ACCENT_EMERALD),
        ("Category", category, COLOR_TEXT_WHITE),
    ]
    data_table_html = render_data_table(data_rows)
    description_card = render_message_card(description, header_label="Issue Summary", accent_color=ACCENT_SKY) if description else ""

    content_html = f"""
    <p style="margin: 0 0 16px 0; font-size: 14px; line-height: 1.6; color: {COLOR_TEXT_PRIMARY};">
      Your support ticket has been logged with our team. We are reviewing your inquiry and will respond as quickly as possible.
    </p>

    {data_table_html}

    {description_card}
    """

    guidance = "Need to add details or attach more information? Simply reply directly to this email to update your ticket."
    cta_url = f"{BASE_URL}/support"

    html = render_email_shell(
        subject=subject,
        badge_text="SUPPORT REQUEST",
        badge_color=ACCENT_SKY,
        title=title,
        content_html=content_html,
        greeting_name=user_name,
        cta_text="View Support Ticket",
        cta_url=cta_url,
        guidance_note=guidance,
        footer_sender_line=f"Sent by Ametheus Support &bull; {settings.SUPPORT_EMAIL}",
    )

    text = f"""Hello {user_name},

We've received your support request and assigned it ticket #{ticket_id}.

Ticket ID: #{ticket_id}
Status:    {friendly_status}
Category:  {category}

Our support team is reviewing your inquiry and will follow up shortly.

Summary of your issue:
{description}

Need to add more information? Simply reply directly to this email to update your ticket.

View your ticket online:
{cta_url}

—
Ametheus Support Desk
{settings.SUPPORT_EMAIL}
"""
    return subject, text, html


def build_support_admin_reply_email(
    user_name: str,
    ticket_id: str,
    category: str,
    admin_message: str,
    friendly_status: str,
) -> Tuple[str, str, str]:
    """
    Renders email sent to the user when an admin responds to their support ticket.
    Sender: support@ametheus.games
    Reply-To: support@ametheus.games
    """
    clean_tid = ticket_id.lstrip("#")
    subject = f"Re: [Ticket {clean_tid}] {category} — Ametheus Support"
    title = f"Update on Support Ticket #{clean_tid}"

    data_rows = [
        ("Ticket ID", f"#{clean_tid}", ACCENT_SKY),
        ("Current Status", friendly_status, ACCENT_EMERALD if "Resolved" in friendly_status else ACCENT_SKY),
    ]
    status_table = render_data_table(data_rows)
    reply_card = render_message_card(admin_message, header_label="Support Response", accent_color=ACCENT_CRIMSON)

    content_html = f"""
    <p style="margin: 0 0 16px 0; font-size: 14px; line-height: 1.6; color: {COLOR_TEXT_PRIMARY};">
      Our support team has provided an update on your ticket:
    </p>

    {reply_card}

    {status_table}
    """

    guidance = "Need to reply? Simply respond directly to this email to continue the conversation."
    cta_url = f"{BASE_URL}/support"

    html = render_email_shell(
        subject=subject,
        badge_text="SUPPORT UPDATE",
        badge_color=ACCENT_CRIMSON,
        title=title,
        content_html=content_html,
        greeting_name=user_name,
        cta_text="View Support Thread",
        cta_url=cta_url,
        guidance_note=guidance,
        footer_sender_line=f"Sent by Ametheus Support &bull; {settings.SUPPORT_EMAIL}",
    )

    text = f"""Hello {user_name},

Our support team has updated your ticket [#{clean_tid}]:

--------------------------------------------------
{admin_message.strip()}
--------------------------------------------------

Ticket ID: #{clean_tid}
Status:    {friendly_status}

Need to reply? Simply respond directly to this email to continue the conversation.

View the support thread:
{cta_url}

—
Ametheus Support Desk
{settings.SUPPORT_EMAIL}
"""
    return subject, text, html


def build_admin_ticket_notification_email(ticket: dict) -> Tuple[str, str, str]:
    """
    Internal notification email dispatched to the admin team when a new ticket is submitted.
    """
    ticket_id = ticket.get("ticket_id") or ticket.get("id") or "AMT-000000"
    user_name = ticket.get("name", "Unknown Contender")
    user_email = ticket.get("email", "unknown@ametheus.games")
    user_id = ticket.get("user_id") or "Guest"
    category = ticket.get("category", "General Inquiry").replace("_", " ").title()
    payment_type = ticket.get("payment_type", "N/A")
    description = ticket.get("description", "")
    created_at = ticket.get("created_at", "Just now")

    subject = f"[Support Alert] #{ticket_id} — {category} ({user_name})"
    title = f"New Support Ticket Submitted"

    data_rows = [
        ("Ticket ID", f"#{ticket_id}", ACCENT_CRIMSON),
        ("User", f"{user_name} ({user_email})", COLOR_TEXT_WHITE),
        ("Account ID", str(user_id), COLOR_TEXT_SECONDARY),
        ("Category", category, ACCENT_GOLD),
        ("Payment Type", payment_type, COLOR_TEXT_SECONDARY),
        ("Created At", str(created_at), COLOR_TEXT_SECONDARY),
    ]
    data_table = render_data_table(data_rows)
    desc_card = render_message_card(description, header_label="User Description", accent_color=ACCENT_GOLD)

    content_html = f"""
    <p style="margin: 0 0 16px 0; font-size: 14px; line-height: 1.6; color: {COLOR_TEXT_PRIMARY};">
      A user has submitted a new support inquiry requiring review:
    </p>

    {data_table}

    {desc_card}
    """

    admin_url = f"{BASE_URL}/admin/support"

    html = render_email_shell(
        subject=subject,
        badge_text="NEW TICKET",
        badge_color=ACCENT_CRIMSON,
        title=title,
        content_html=content_html,
        greeting_name=None,
        cta_text="Open in Admin Cockpit",
        cta_url=admin_url,
        footer_sender_line="Ametheus Internal Support Telemetry",
    )

    text = f"""[New Support Ticket]

Ticket ID:    #{ticket_id}
User:         {user_name} <{user_email}>
Account ID:   {user_id}
Category:     {category}
Payment Type: {payment_type}
Timestamp:    {created_at}

Description:
{description}

Manage this ticket in the Admin Cockpit:
{admin_url}
"""
    return subject, text, html


def build_user_notification_email(
    user_name: str,
    title: str,
    message: str,
    link: Optional[str] = None,
    notification_type: Optional[str] = None,
) -> Tuple[str, str, str]:
    """
    Renders a branded notification email for platform events:
    Match, Tournament, Wallet, Payment, Team, or Account events.
    """
    display_name = user_name or "Contender"
    norm_type = (notification_type or "").upper()

    # Determine badge, accent, and button text based on event type
    if norm_type in ("PAYMENT", "WALLET") or "payment" in title.lower() or "wallet" in title.lower() or "deposit" in title.lower() or "payout" in title.lower():
        badge_text = "PAYMENT UPDATE"
        badge_color = ACCENT_EMERALD
        cta_label = "View Wallet Details"
    elif norm_type in ("MATCH",) or "match" in title.lower() or "vs" in title.lower():
        badge_text = "MATCH UPDATE"
        badge_color = ACCENT_CRIMSON
        cta_label = "View Match Details"
    elif norm_type in ("TOURNAMENT", "REGISTRATION", "BATCHES") or "tournament" in title.lower():
        badge_text = "TOURNAMENT UPDATE"
        badge_color = ACCENT_GOLD
        cta_label = "View Tournament"
    elif norm_type in ("TEAM",) or "team" in title.lower():
        badge_text = "TEAM UPDATE"
        badge_color = ACCENT_SKY
        cta_label = "View Team"
    elif norm_type in ("SECURITY", "ACCOUNT") or "security" in title.lower():
        badge_text = "SECURITY NOTICE"
        badge_color = ACCENT_CRIMSON
        cta_label = "Review Security"
    else:
        badge_text = "PLATFORM NOTICE"
        badge_color = ACCENT_CRIMSON
        cta_label = "Open Dashboard"

    destination_url = f"{BASE_URL}/dashboard"
    if link:
        if link.startswith("http"):
            destination_url = link
        else:
            destination_url = f"{BASE_URL}{link if link.startswith('/') else '/' + link}"

    subject = f"{title} — Ametheus"

    message_card = render_message_card(message, accent_color=badge_color)

    content_html = f"""
    {message_card}
    """

    html = render_email_shell(
        subject=subject,
        badge_text=badge_text,
        badge_color=badge_color,
        title=title,
        content_html=content_html,
        greeting_name=display_name,
        cta_text=cta_label,
        cta_url=destination_url,
        footer_sender_line=f"Sent by Ametheus &bull; {settings.NOREPLY_EMAIL}",
    )

    text = f"""Hello {display_name},

{title}

{message}

View details on your dashboard:
{destination_url}

—
Ametheus Esports Platform
{BASE_URL}
"""
    return subject, text, html


def build_security_alert_email(
    user_name: str,
    recipient_email: str,
    client_ip: str,
    device_info: str,
    timestamp_str: str,
) -> Tuple[str, str, str]:
    """
    Renders security notification email for unrecognized IP logins.
    """
    display_name = user_name or "Contender"
    subject = f"Security Alert: New sign-in detected from {client_ip}"
    title = "New sign-in from an unrecognized device"

    data_rows = [
        ("Account", f"{display_name} ({recipient_email})", COLOR_TEXT_WHITE),
        ("IP Address", client_ip, ACCENT_CRIMSON),
        ("Device / Browser", device_info, ACCENT_SKY),
        ("Date & Time", timestamp_str, COLOR_TEXT_SECONDARY),
    ]
    data_table = render_data_table(data_rows)

    settings_url = f"{BASE_URL}/settings#Security"

    content_html = f"""
    <p style="margin: 0 0 16px 0; font-size: 14px; line-height: 1.6; color: {COLOR_TEXT_PRIMARY};">
      A sign-in to your Ametheus account was detected from an IP address not previously recognized:
    </p>

    {data_table}

    <div style="background-color: {COLOR_SURFACE_CARD}; border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 18px 20px; margin: 20px 0;">
      <p style="margin: 0 0 10px 0; font-size: 13px; font-weight: 700; color: {COLOR_TEXT_WHITE};">
        Was this you?
      </p>
      <p style="margin: 0 0 12px 0; font-size: 13px; line-height: 1.6; color: {COLOR_TEXT_SECONDARY};">
        &bull; <strong>If you just signed in:</strong> You can safely disregard this email. This device will be remembered for future sessions.
      </p>
      <p style="margin: 0; font-size: 13px; line-height: 1.6; color: #FCA5A5;">
        &bull; <strong>If you didn't sign in:</strong> Someone else may have your login credentials. We strongly advise changing your password and terminating other active sessions immediately.
      </p>
    </div>
    """

    html = render_email_shell(
        subject=subject,
        badge_text="SECURITY ALERT",
        badge_color=ACCENT_CRIMSON,
        title=title,
        content_html=content_html,
        greeting_name=display_name,
        cta_text="Review Account Security",
        cta_url=settings_url,
        footer_sender_line=f"Sent by Ametheus Security &bull; {settings.NOREPLY_EMAIL}",
    )

    text = f"""Hello {display_name},

SECURITY ALERT: New sign-in detected from {client_ip}

We detected a sign-in to your Ametheus account from an unrecognized device:

• Account:         {display_name} ({recipient_email})
• IP Address:      {client_ip}
• Device:          {device_info}
• Date & Time:     {timestamp_str}

WAS THIS YOU?
• If this was you: You can safely ignore this notification.
• If you do not recognize this activity:
  1. Change your password immediately: {settings_url}
  2. Terminate all other active sessions under Settings > Security.
  3. Contact support at {settings.SUPPORT_EMAIL}.

—
Ametheus Security Operations
{BASE_URL}
"""
    return subject, text, html


def build_official_announcement_email(
    subject: str,
    message: str,
    recipient_name: Optional[str] = None,
) -> Tuple[str, str, str]:
    """
    Renders official broadcast announcements from Ametheus leadership.
    """
    name = (recipient_name or "Ametheus Contender").strip()
    title = subject

    # Format message paragraphs
    clean_msg = message.strip()
    html_paragraphs = "".join([
        f"<p style='margin: 0 0 16px 0; font-size: 14px; line-height: 1.7; color: {COLOR_TEXT_PRIMARY};'>{_escape_html(p.strip())}</p>"
        for p in clean_msg.split("\n\n") if p.strip()
    ])

    content_html = f"""
    <div style="background-color: {COLOR_SURFACE_CARD}; border: 1px solid {COLOR_BORDER}; border-radius: 6px; padding: 22px 24px; margin: 18px 0;">
      {html_paragraphs}
    </div>
    """

    portal_url = BASE_URL

    html = render_email_shell(
        subject=subject,
        badge_text="OFFICIAL ANNOUNCEMENT",
        badge_color=ACCENT_GOLD,
        title=title,
        content_html=content_html,
        greeting_name=name,
        cta_text="Visit Ametheus Platform",
        cta_url=portal_url,
        footer_sender_line=f"Sent by Ametheus Official Communications &bull; {settings.OFFICIAL_EMAIL}",
    )

    text = f"""Hello {name},

{subject}
==================================================

{clean_msg}

—
Ametheus Platform Leadership
{BASE_URL}
"""
    return subject, text, html


def build_organizer_reply_email(
    organizer_name: str,
    inquiry_subject: str,
    admin_message: str,
    status: str,
) -> Tuple[str, str, str]:
    """
    Renders email sent to tournament organizers when administration replies to their communication.
    """
    display_name = organizer_name or "Tournament Organizer"
    subject = f"Re: [Organizer Inquiry] {inquiry_subject} — Ametheus Admin"
    title = f"Response to: {inquiry_subject}"

    data_rows = [
        ("Inquiry Subject", inquiry_subject, COLOR_TEXT_WHITE),
        ("Status", status, ACCENT_EMERALD if status == "RESOLVED" else ACCENT_GOLD),
    ]
    data_table = render_data_table(data_rows)
    msg_card = render_message_card(admin_message, header_label="Administration Response", accent_color=ACCENT_GOLD)

    content_html = f"""
    <p style="margin: 0 0 16px 0; font-size: 14px; line-height: 1.6; color: {COLOR_TEXT_PRIMARY};">
      Platform administration has responded to your organizer communication:
    </p>

    {msg_card}

    {data_table}
    """

    organizer_url = f"{BASE_URL}/organizer"
    guidance = "Visit your Organizer Console to view tournament metrics or continue this conversation."

    html = render_email_shell(
        subject=subject,
        badge_text="ORGANIZER INQUIRY",
        badge_color=ACCENT_GOLD,
        title=title,
        content_html=content_html,
        greeting_name=display_name,
        cta_text="Open Organizer Console",
        cta_url=organizer_url,
        guidance_note=guidance,
        footer_sender_line=f"Sent by Ametheus Administration &bull; {settings.OFFICIAL_EMAIL}",
    )

    text = f"""Hello {display_name},

Platform administration has responded to your communication regarding: {inquiry_subject}

--------------------------------------------------
{admin_message.strip()}
--------------------------------------------------

Status: {status}

Visit your Organizer Console to manage your tournaments:
{organizer_url}

—
Ametheus Administration
{settings.OFFICIAL_EMAIL}
"""
    return subject, text, html


def build_match_roster_email(
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
) -> Tuple[str, str, str]:
    """
    Renders match assignment / update notification for team captains and players.
    """
    display_leader = leader_name or "Team Leader"
    action_label = "MATCH UPDATED" if is_update else "MATCH SCHEDULED"
    badge_color = ACCENT_GOLD if is_update else ACCENT_CRIMSON

    subject = f"{action_label.title()}: {tournament_title} — {round_name} (Match {match_number})"
    title = f"Your match has been {'updated' if is_update else 'scheduled'} in {tournament_title}"

    # Change diff card if update
    update_box_html = ""
    if is_update and previous_details:
        prev_opp = previous_details.get("opponent", "TBD")
        prev_time = previous_details.get("time", "TBD")
        update_box_html = f"""
        <div style="background-color: {COLOR_SURFACE_CARD}; border: 1px solid {ACCENT_GOLD}; border-radius: 6px; padding: 14px 18px; margin-bottom: 20px;">
          <p style="margin: 0 0 6px 0; font-size: 11px; text-transform: uppercase; font-weight: 700; color: {ACCENT_GOLD}; letter-spacing: 0.8px;">
            Schedule Update Summary
          </p>
          <p style="margin: 0; font-size: 13px; line-height: 1.5; color: {COLOR_TEXT_PRIMARY};">
            <span style="color: {COLOR_TEXT_SECONDARY};">Previous:</span> {prev_opp} &bull; {prev_time}<br/>
            <span style="color: {ACCENT_EMERALD}; font-weight: 600;">Updated:</span> {opponent_name} &bull; {scheduled_time}
          </p>
        </div>
        """

    data_rows = [
        ("Tournament", tournament_title, COLOR_TEXT_WHITE),
        ("Game", game_name, ACCENT_SKY),
        ("Round & Match", f"{round_name} &bull; Match {match_number}", COLOR_TEXT_WHITE),
        ("Your Team", team_name, ACCENT_EMERALD),
        ("Opponent", opponent_name, ACCENT_CRIMSON),
        ("Date & Time", f"{scheduled_date} &bull; {scheduled_time}", ACCENT_GOLD),
    ]
    data_table = render_data_table(data_rows)

    content_html = f"""
    <p style="margin: 0 0 16px 0; font-size: 14px; line-height: 1.6; color: {COLOR_TEXT_PRIMARY};">
      Your team <strong style="color: {COLOR_TEXT_WHITE};">{_escape_html(team_name)}</strong> has a match confirmed in <strong style="color: {badge_color};">{_escape_html(tournament_title)}</strong>:
    </p>

    {update_box_html}

    {data_table}
    """

    destination_url = f"{BASE_URL}/matches/{match_id}"

    sec_btn_text = None
    sec_btn_url = None
    if community_link:
        plat_label = (community_platform or "Community").capitalize()
        sec_btn_text = f"Join {plat_label}"
        sec_btn_url = community_link

    guidance = "Be online and check-in at least 15 minutes before match start time. Ensure all team members meet competitive eligibility criteria."

    html = render_email_shell(
        subject=subject,
        badge_text=action_label,
        badge_color=badge_color,
        title=title,
        content_html=content_html,
        greeting_name=display_leader,
        cta_text="View Match Details",
        cta_url=destination_url,
        secondary_cta_text=sec_btn_text,
        secondary_cta_url=sec_btn_url,
        guidance_note=guidance,
        footer_sender_line=f"Sent by Ametheus Match Engine &bull; {settings.NOREPLY_EMAIL}",
    )

    text = f"""Hello {display_leader},

{action_label}: {tournament_title}

Your team {team_name} has a match scheduled in {tournament_title}.

Tournament: {tournament_title}
Game:       {game_name}
Round:      {round_name} (Match {match_number})
Your Team:  {team_name}
Opponent:   {opponent_name}
Date & Time:{scheduled_date} @ {scheduled_time}
"""

    if is_update and previous_details:
        text += f"""
Schedule Update:
Previous: {previous_details.get('opponent', 'TBD')} @ {previous_details.get('time', 'TBD')}
Updated:  {opponent_name} @ {scheduled_time}
"""

    text += f"""
View Match Details:
{destination_url}
"""

    if community_link:
        text += f"""
Tournament Community ({community_platform or 'Discord/WhatsApp'}):
{community_link}
"""

    text += f"""
—
Ametheus Match Operations
{BASE_URL}
"""
    return subject, text, html


def build_smtp_diagnostic_email(
    service_name: str,
    sender_name: str,
    sender_email: str,
    recipient_email: str,
    host: str,
    port: int,
) -> Tuple[str, str, str]:
    """
    Renders diagnostic verification email for admin SMTP testing.
    """
    subject = f"Ametheus SMTP Verification — {sender_name} ({sender_email})"
    title = "Brevo SMTP Delivery Verified"

    data_rows = [
        ("Identity Key", service_name.upper(), ACCENT_GOLD),
        ("Sender", f"{sender_name} <{sender_email}>", COLOR_TEXT_WHITE),
        ("Recipient", recipient_email, COLOR_TEXT_WHITE),
        ("Relay Server", f"{host}:{port}", COLOR_TEXT_SECONDARY),
        ("Status", "Delivered Successfully", ACCENT_EMERALD),
    ]
    data_table = render_data_table(data_rows)

    content_html = f"""
    <div style="background-color: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.35); border-radius: 6px; padding: 14px 18px; margin-bottom: 20px; color: #A7F3D0; font-size: 13px; font-weight: 600;">
      &#10004; Success: Brevo SMTP relay connection and delivery confirmed.
    </div>

    <p style="margin: 0 0 16px 0; font-size: 14px; line-height: 1.6; color: {COLOR_TEXT_PRIMARY};">
      This test message was dispatched from the Ametheus Operations Cockpit to confirm that outgoing email infrastructure is operating smoothly.
    </p>

    {data_table}
    """

    cockpit_url = f"{BASE_URL}/admin"

    html = render_email_shell(
        subject=subject,
        badge_text="SMTP DIAGNOSTIC",
        badge_color=ACCENT_EMERALD,
        title=title,
        content_html=content_html,
        greeting_name=None,
        cta_text="Open Admin Cockpit",
        cta_url=cockpit_url,
        footer_sender_line=f"Sent by Ametheus Diagnostic Telemetry &bull; {sender_email}",
    )

    text = f"""Ametheus Esports Platform — Brevo SMTP Service Test
===================================================

This is an administrative test message dispatched from the Ametheus Operations Cockpit.
Brevo SMTP delivery for {sender_name} ({service_name.upper()}) is functioning properly.

Identity:     {sender_name}
Sender Email: {sender_email}
Destination:  {recipient_email}
Relay Host:   {host}:{port}
Status:       Delivered Successfully

Manage platform operations:
{cockpit_url}

—
Ametheus System Operations
{BASE_URL}
"""
    return subject, text, html
