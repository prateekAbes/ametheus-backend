"""
Canonical Notification Categories and Mapping Service for Ametheus.

This module provides the canonical single-source-of-truth mapping
between events, notification types, database categories, API query filters,
and frontend category tabs:
- SYSTEM
- TEAM
- WALLET
- REGISTRATION
- BATCHES
(with backward-compatible support for TOURNAMENT and MATCH)
"""

from typing import Dict, Any, Optional

CANONICAL_CATEGORIES = (
    "SYSTEM",
    "TEAM",
    "WALLET",
    "REGISTRATION",
    "BATCHES",
)

# Extended canonical set including legacy/granular types
ALL_VALID_CATEGORIES = (
    "SYSTEM",
    "TEAM",
    "WALLET",
    "REGISTRATION",
    "BATCHES",
    "TOURNAMENT",
    "MATCH",
)


def resolve_canonical_category(
    notification_type: Optional[str],
    title: Optional[str] = None,
) -> str:
    """
    Normalizes any event or legacy notification type string into its canonical category.
    Uses title heuristics where appropriate (e.g. 'Tournament Registered' -> 'REGISTRATION').
    """
    raw_type = (notification_type or "").strip().upper()
    title_upper = (title or "").strip().upper()

    # Exact canonical category takes absolute highest precedence
    if raw_type in CANONICAL_CATEGORIES:
        return raw_type
    if raw_type in ("BATCH", "BATCH_PUBLISH", "BATCH_NOTIFICATION", "BATCH_BROADCAST"):
        return "BATCHES"
    if raw_type in ("PAYMENT", "PAYOUT", "WITHDRAWAL", "DEPOSIT", "REFUND", "WALLET_TOPUP"):
        return "WALLET"
    if raw_type in ("TOURNAMENT_REGISTRATION", "EVENT_REGISTRATION"):
        return "REGISTRATION"

    # 1. BATCHES events (must precede TEAM/ROSTER heuristics so batch roster actions are categorized as BATCHES)
    if "BATCH" in raw_type or "BATCH" in title_upper:
        return "BATCHES"

    # 2. WALLET / PAYMENT events
    if (
        "WALLET" in raw_type
        or "PAYMENT" in raw_type
        or "PAYOUT" in raw_type
        or "WITHDRAW" in raw_type
        or "DEPOSIT" in raw_type
        or "REFUND" in raw_type
        or "TRANSACTION" in raw_type
        or "WALLET" in title_upper
        or "PAYMENT" in title_upper
        or "WITHDRAW" in title_upper
        or "DEPOSIT" in title_upper
    ):
        return "WALLET"

    # 3. REGISTRATION events
    if (
        "REGISTER" in raw_type
        or "REGISTERED" in title_upper
        or "REGISTRATION" in title_upper
    ):
        return "REGISTRATION"

    # 4. TEAM events
    if (
        raw_type in ("TEAM", "TEAM_INVITE", "ROSTER", "SQUAD", "CAPTAIN", "TEAM_JOIN", "TEAM_KICK", "TEAM_LEADERSHIP")
        or "TEAM" in raw_type
        or "TEAM" in title_upper
        or "ROSTER" in title_upper
        or "SQUAD" in title_upper
    ):
        return "TEAM"

    # 5. MATCH events
    if raw_type in ("MATCH", "SCHEDULE", "FIXTURE") or "MATCH" in raw_type or "MATCH" in title_upper:
        return "MATCH"

    # 6. TOURNAMENT events (non-registration tournament alerts)
    if raw_type in ("TOURNAMENT", "TOURNEY") or "TOURNAMENT" in raw_type:
        return "TOURNAMENT"

    # 7. SYSTEM events (default fallback)
    return "SYSTEM"


def get_category_query_filter(category: str) -> Dict[str, Any]:
    """
    Generates a robust MongoDB query clause matching both modern 'category' field
    and legacy 'type' / 'title' fields to ensure historical notifications are never lost.
    """
    cat_upper = category.strip().upper()

    if cat_upper in ("ALL", ""):
        return {}

    if cat_upper == "WALLET":
        return {
            "$or": [
                {"category": {"$in": ["WALLET", "wallet"]}},
                {
                    "type": {
                        "$in": [
                            "WALLET",
                            "PAYMENT",
                            "WALLET_TOPUP",
                            "PAYOUT",
                            "WITHDRAWAL",
                            "DEPOSIT",
                            "REFUND",
                            "wallet",
                            "payment",
                        ]
                    }
                },
                {"title": {"$regex": "wallet|payment|deposit|payout|withdrawal|refund|utr", "$options": "i"}},
            ]
        }

    if cat_upper == "REGISTRATION":
        return {
            "$or": [
                {"category": {"$in": ["REGISTRATION", "registration"]}},
                {"type": {"$in": ["REGISTRATION", "TOURNAMENT_REGISTRATION", "registration"]}},
                {"title": {"$regex": "registered|registration", "$options": "i"}},
            ]
        }

    if cat_upper == "TEAM":
        return {
            "$or": [
                {"category": {"$in": ["TEAM", "team"]}},
                {"type": {"$in": ["TEAM", "TEAM_JOIN", "TEAM_KICK", "ROSTER", "team"]}},
                {"title": {"$regex": "team|squad|captain|roster", "$options": "i"}},
            ]
        }

    if cat_upper in ("BATCHES", "BATCH"):
        return {
            "$or": [
                {"category": {"$in": ["BATCHES", "BATCH", "batches", "batch"]}},
                {"type": {"$in": ["BATCHES", "BATCH", "BATCH_PUBLISH", "batches", "batch"]}},
                {"title": {"$regex": "batch", "$options": "i"}},
            ]
        }

    if cat_upper == "SYSTEM":
        return {
            "$or": [
                {"category": {"$in": ["SYSTEM", "system"]}},
                {"type": {"$in": ["SYSTEM", "ADMIN", "GENERAL", "WELCOME", "system", "admin"]}},
            ]
        }

    if cat_upper == "TOURNAMENT":
        return {
            "$or": [
                {"category": {"$in": ["TOURNAMENT", "tournament"]}},
                {"type": {"$in": ["TOURNAMENT", "tournament"]}},
            ]
        }

    if cat_upper == "MATCH":
        return {
            "$or": [
                {"category": {"$in": ["MATCH", "match"]}},
                {"type": {"$in": ["MATCH", "match"]}},
            ]
        }

    return {
        "$or": [
            {"category": {"$in": [cat_upper, cat_upper.lower()]}},
            {"type": {"$in": [cat_upper, cat_upper.lower()]}},
        ]
    }


def resolve_notification_label(
    sender_role: Optional[str] = None,
    notification_type: Optional[str] = None,
    category: Optional[str] = None,
    title: Optional[str] = None,
    message: Optional[str] = None,
) -> str:
    """
    Classifies and determines the human-readable notification/message label.
    Specifically guarantees:
    - Administrator/Support responses (from super_admin, organizer, admin, arbiter)
      receive the clear label 'Admin Response'.
    - User replies receive 'User Reply'.
    - System notifications receive 'System Notification'.
    - Domain categories (Wallet, Team, Registration, Batches) retain their clear labels.
    - Ambiguous labels like 'Response' are never displayed.
    """
    role_upper = (sender_role or "").strip().upper()
    title_upper = (title or "").strip().upper()
    type_upper = (notification_type or "").strip().upper()
    cat_upper = (category or "").strip().upper()
    msg_upper = (message or "").strip().upper()

    # 1. Explicit Administrator Role
    if role_upper in ("SUPER_ADMIN", "ADMIN", "ORGANIZER", "SUPPORT_ADMIN", "ARBITER"):
        return "Admin Response"

    # 2. Explicit User Role
    if role_upper == "USER":
        return "User Reply"

    # 3. Explicit System Role
    if role_upper == "SYSTEM":
        return "System Notification"

    # 4. Domain categories must NOT be mislabeled as Admin Response
    if cat_upper == "WALLET" or type_upper in (
        "WALLET", "PAYMENT", "WALLET_TOPUP", "PAYOUT", "WITHDRAWAL", "DEPOSIT", "REFUND"
    ):
        return "Wallet"

    if cat_upper == "TEAM" or type_upper in (
        "TEAM", "TEAM_JOIN", "TEAM_KICK", "ROSTER", "SQUAD"
    ):
        return "Team"

    if cat_upper == "REGISTRATION" or type_upper in (
        "REGISTRATION", "TOURNAMENT_REGISTRATION"
    ):
        return "Registration"

    if cat_upper in ("BATCHES", "BATCH") or type_upper in (
        "BATCH", "BATCHES", "BATCH_PUBLISH"
    ):
        return "Batches"

    if cat_upper == "MATCH" or type_upper == "MATCH":
        return "Match"

    if cat_upper == "TOURNAMENT" or type_upper == "TOURNAMENT":
        return "Tournament"

    # 5. Check if message was from user vs administrative response
    if type_upper == "USER" or title_upper in ("USER REPLY", "USER MESSAGE", "INBOUND EMAIL"):
        return "User Reply"

    # 6. Admin / Support responses (including fixing ambiguous "Response")
    if (
        title_upper in ("RESPONSE", "ADMIN RESPONSE", "SUPPORT RESPONSE", "ADMIN REPLY", "SUPPORT REPLY")
        or type_upper in ("ADMIN", "SUPPORT")
        or "ADMIN RESPONSE" in title_upper
        or "SUPPORT RESPONSE" in title_upper
        or "ADMIN REPLY" in title_upper
        or "ADMIN RESPONSE:" in msg_upper
        or "ADMIN REPLY:" in msg_upper
        or "NEW RESPONSE FROM THE AMETHEUS SUPPORT TEAM" in msg_upper
    ):
        if not (
            title_upper.startswith("WELCOME")
            or "AUTOMATED" in title_upper
            or "CONFIRMATION" in title_upper
            or "TICKET SUBMITTED" in title_upper
        ):
            return "Admin Response"

    # 7. Default to System Notification
    return "System Notification"

