"""
Unit and Integration Tests for Ametheus Central Transactional Email Design System
================================================================================
Verifies that all transactional email builders adhere strictly to the design system:
- High quality email client compatibility (HTML table layout, inline styles, dark mode)
- Wordmark header and contextual badges
- Contextual, bulletproof CTA buttons (no generic "AMETHEUS ALERT" or "OPEN IN COCKPIT ->")
- Natural human tone (no AI/robotic phrasing)
- Clear guidance for support replies (From: support@ametheus.games, Reply-To: support@ametheus.games)
- Plain text companions for all emails
"""

import pytest
from app.services.email_template_system import (
    build_support_ticket_confirmation_email,
    build_support_admin_reply_email,
    build_admin_ticket_notification_email,
    build_user_notification_email,
    build_security_alert_email,
    build_official_announcement_email,
    build_organizer_reply_email,
    build_match_roster_email,
    build_smtp_diagnostic_email,
    render_email_shell,
    BASE_URL,
    ACCENT_CRIMSON,
    ACCENT_GOLD,
    ACCENT_EMERALD,
    COLOR_CANVAS,
    COLOR_SURFACE,
)


def _assert_common_email_standards(subject: str, text: str, html: str):
    """Asserts foundational standards across all emails in the design system."""
    assert subject and len(subject.strip()) > 5
    assert text and len(text.strip()) > 20
    assert html and len(html.strip()) > 50

    # Strict HTML email boilerplate checks
    assert "<!DOCTYPE html>" in html
    assert '<meta name="viewport" content="width=device-width, initial-scale=1.0">' in html
    assert COLOR_CANVAS in html
    assert COLOR_SURFACE in html
    assert "max-width: 600px" in html
    assert "AMETHEUS" in html  # Brand wordmark

    # Prohibited generic/robotic strings check
    assert "AMETHEUS ALERT" not in html
    assert "OPEN IN COCKPIT" not in html
    assert "We are pleased to inform you" not in html
    assert "Your ticket has received a new response" not in html
    assert "#3EF2FF" not in html  # Old cyan removed


def test_support_ticket_confirmation_email():
    ticket = {
        "id": "AMT-789012",
        "ticket_id": "AMT-789012",
        "name": "Sarah Connor",
        "email": "sarah@example.com",
        "category": "PAYMENT",
        "description": "My tournament prize pool deposit did not reflect in my wallet.",
        "status": "WAITING_FOR_ADMIN",
    }
    subject, text, html = build_support_ticket_confirmation_email(ticket)

    _assert_common_email_standards(subject, text, html)
    assert "AMT-789012" in subject
    assert "Support Request Received" in subject
    assert "SUPPORT REQUEST" in html  # Contextual badge
    assert "Sarah Connor" in html
    assert "sarah@example.com" not in html  # Privacy
    assert "My tournament prize pool deposit" in html
    assert "View Support Ticket" in html  # Actionable CTA
    assert "Simply reply directly to this email" in html  # Support guidance
    assert "Simply reply directly to this email" in text


def test_support_admin_reply_email():
    subject, text, html = build_support_admin_reply_email(
        user_name="John Doe",
        ticket_id="AMT-10492",
        category="Tournament Inquiry",
        admin_message="Your match has been rescheduled to 8:00 PM IST due to server maintenance.",
        friendly_status="Waiting for Your Response",
    )

    _assert_common_email_standards(subject, text, html)
    assert "[Ticket AMT-10492]" in subject
    assert "SUPPORT UPDATE" in html
    assert "John Doe" in html
    assert "rescheduled to 8:00 PM IST" in html
    assert "Waiting for Your Response" in html
    assert "View Support Thread" in html
    assert "Simply respond directly to this email" in html


def test_admin_ticket_notification_email():
    ticket = {
        "id": "AMT-990011",
        "ticket_id": "AMT-990011",
        "name": "Alex Mercer",
        "email": "alex@prototype.com",
        "user_id": "usr_proto_99",
        "category": "TECHNICAL",
        "payment_type": "UPI",
        "description": "Unable to connect to matchmaking server in Mumbai region.",
        "created_at": "2026-10-08T18:00:00Z",
    }
    subject, text, html = build_admin_ticket_notification_email(ticket)

    _assert_common_email_standards(subject, text, html)
    assert "[Support Alert]" in subject
    assert "AMT-990011" in subject
    assert "NEW TICKET" in html
    assert "Alex Mercer" in html
    assert "alex@prototype.com" in html
    assert "usr_proto_99" in html
    assert "UPI" in html
    assert "Open in Admin Cockpit" in html


def test_user_notification_email_contextual_variants():
    # 1. Wallet / Payment notification
    sub_pay, text_pay, html_pay = build_user_notification_email(
        user_name="Viper",
        title="Prize Pool Payout Processed",
        message="₹15,000 has been credited to your verified Ametheus wallet.",
        link="/wallet",
        notification_type="WALLET",
    )
    _assert_common_email_standards(sub_pay, text_pay, html_pay)
    assert "PAYMENT UPDATE" in html_pay
    assert "View Wallet Details" in html_pay
    assert f"{BASE_URL}/wallet" in html_pay

    # 2. Match notification
    sub_m, text_m, html_m = build_user_notification_email(
        user_name="Phoenix",
        title="Quarterfinals Match Ready",
        message="Your match against Team Liquid has been created in the lobby.",
        link="/matches/m_12345",
        notification_type="MATCH",
    )
    _assert_common_email_standards(sub_m, text_m, html_m)
    assert "MATCH UPDATE" in html_m
    assert "View Match Details" in html_m
    assert f"{BASE_URL}/matches/m_12345" in html_m

    # 3. Tournament notification
    sub_t, text_t, html_t = build_user_notification_email(
        user_name="Sova",
        title="Registration Confirmed: BGMI Winter Masters",
        message="Your team has successfully registered for Bracket A.",
        link="/tournaments/bgmi-winter",
        notification_type="TOURNAMENT",
    )
    _assert_common_email_standards(sub_t, text_t, html_t)
    assert "TOURNAMENT UPDATE" in html_t
    assert "View Tournament" in html_t


def test_security_alert_email():
    subject, text, html = build_security_alert_email(
        user_name="Cypher",
        recipient_email="cypher@ametheus.games",
        client_ip="198.51.100.42",
        device_info="Chrome 130 on Linux x86_64",
        timestamp_str="October 8, 2026, 11:30 PM UTC",
    )

    _assert_common_email_standards(subject, text, html)
    assert "198.51.100.42" in subject
    assert "SECURITY ALERT" in html
    assert "198.51.100.42" in html
    assert "Chrome 130 on Linux" in html
    assert "Was this you?" in html
    assert "Review Account Security" in html
    assert f"{BASE_URL}/settings#Security" in html


def test_official_announcement_email():
    subject, text, html = build_official_announcement_email(
        subject="Ametheus National Championship 2026 Announced",
        message="We are excited to unveil the biggest esports championship in South Asia with a prize pool of ₹1,00,00,000.\n\nRegistrations open on November 1st.",
        recipient_name="Player One",
    )

    _assert_common_email_standards(subject, text, html)
    assert "Ametheus National Championship 2026 Announced" in subject
    assert "OFFICIAL ANNOUNCEMENT" in html
    assert "Player One" in html
    assert "₹1,00,00,000" in html
    assert "Visit Ametheus Platform" in html


def test_organizer_reply_email():
    subject, text, html = build_organizer_reply_email(
        organizer_name="Nodwin Gaming",
        inquiry_subject="Custom Lobby Integration Request",
        admin_message="Your custom lobby API key has been provisioned under your Organizer Dashboard.",
        status="RESOLVED",
    )

    _assert_common_email_standards(subject, text, html)
    assert "Custom Lobby Integration Request" in subject
    assert "ORGANIZER INQUIRY" in html
    assert "Nodwin Gaming" in html
    assert "custom lobby API key has been provisioned" in html
    assert "RESOLVED" in html
    assert "Open Organizer Console" in html


def test_match_roster_email_initial_and_update():
    # Initial Match Schedule
    sub_init, text_init, html_init = build_match_roster_email(
        leader_name="Captain Levi",
        tournament_title="Valorant Champions Trophy",
        game_name="Valorant",
        round_name="Semi-Finals",
        match_number=3,
        team_name="Survey Corps",
        opponent_name="Titan Slayers",
        scheduled_date="2026-10-15",
        scheduled_time="19:00 IST",
        match_id="mat_vct_003",
        community_platform="Discord",
        community_link="https://discord.gg/ametheus-vct",
        is_update=False,
    )
    _assert_common_email_standards(sub_init, text_init, html_init)
    assert "MATCH SCHEDULED" in html_init
    assert "Survey Corps" in html_init
    assert "Titan Slayers" in html_init
    assert "View Match Details" in html_init
    assert "Join Discord" in html_init

    # Match Update (Rescheduled)
    sub_upd, text_upd, html_upd = build_match_roster_email(
        leader_name="Captain Levi",
        tournament_title="Valorant Champions Trophy",
        game_name="Valorant",
        round_name="Semi-Finals",
        match_number=3,
        team_name="Survey Corps",
        opponent_name="Titan Slayers",
        scheduled_date="2026-10-15",
        scheduled_time="20:30 IST",
        match_id="mat_vct_003",
        is_update=True,
        previous_details={"opponent": "Titan Slayers", "time": "19:00 IST"},
    )
    _assert_common_email_standards(sub_upd, text_upd, html_upd)
    assert "MATCH UPDATED" in html_upd
    assert "Schedule Update Summary" in html_upd
    assert "19:00 IST" in html_upd
    assert "20:30 IST" in html_upd


def test_smtp_diagnostic_email():
    subject, text, html = build_smtp_diagnostic_email(
        service_name="support",
        sender_name="Ametheus Support",
        sender_email="support@ametheus.games",
        recipient_email="admin@ametheus.gg",
        host="smtp-relay.brevo.com",
        port=587,
    )
    _assert_common_email_standards(subject, text, html)
    assert "SMTP DIAGNOSTIC" in html
    assert "smtp-relay.brevo.com:587" in html
    assert "Delivered Successfully" in html
    assert "Open Admin Cockpit" in html
