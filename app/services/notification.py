import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.models.notification import Notification

from app.services.notification_category import resolve_canonical_category, resolve_notification_label

logger = logging.getLogger("ametheus.notifications")

USER_FACING_NOTIFICATION_TYPES = {
    "TOURNAMENT",
    "PAYMENT",
    "WALLET",
    "MATCH",
    "TEAM",
    "ACCOUNT",
    "SYSTEM",
    "REGISTRATION",
    "BATCHES",
}


async def _safe_send_notification_email(
    db: AsyncIOMotorDatabase,
    event_hash: str,
    u_email: str,
    u_name: str,
    title: str,
    message: str,
    link: Optional[str] = None,
    notification_type: Optional[str] = None,
):
    """Dispatches email notification with duplicate protection and error isolation."""
    try:
        from app.services.email_service import send_notification_email

        # Mark in-flight in sent_email_notifications
        await db.sent_email_notifications.update_one(
            {"event_hash": event_hash},
            {
                "$setOnInsert": {
                    "event_hash": event_hash,
                    "recipient": u_email,
                    "title": title,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "status": "PROCESSING",
                }
            },
            upsert=True,
        )

        ok, err = await send_notification_email(
            recipient=u_email,
            user_name=u_name,
            title=title,
            message=message,
            link=link,
            notification_type=notification_type,
        )

        await db.sent_email_notifications.update_one(
            {"event_hash": event_hash},
            {
                "$set": {
                    "status": "SENT" if ok else "FAILED",
                    "error": err,
                    "dispatched_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
    except Exception as e:
        logger.warning(f"[Ametheus Notification] Async notification email dispatch error: {e}")


async def create_notification(
    db: AsyncIOMotorDatabase,
    user_id: str,
    title: str,
    message: str,
    notification_type: str = "SYSTEM",
    category: Optional[str] = None,
    link: Optional[str] = None,
    metadata_json: str = "{}",
    send_email_copy: bool = True,
    sender_role: Optional[str] = None,
    sender_name: Optional[str] = None,
    label: Optional[str] = None,
) -> Notification:
    """
    Create a persisted notification record for a user in MongoDB.
    Assigns canonical category (SYSTEM, TEAM, WALLET, REGISTRATION, BATCHES)
    and resolves the human-readable label (e.g. 'Admin Response', 'User Reply', 'System Notification').
    Also dispatches an asynchronous email copy to the user's registered address
    for important user-facing events, with duplicate protection.
    """
    canonical_cat = resolve_canonical_category(category or notification_type, title)

    # Check user notification preferences before persisting or dispatching
    user = await db.users.find_one({"$or": [{"_id": user_id}, {"id": user_id}]})
    if user:
        user_prefs = user.get("preferences") or {}
        notif_prefs = user_prefs.get("notifications") or {}

        # Category suppression checks (critical SYSTEM/security notifications are always delivered)
        if canonical_cat in ("TOURNAMENT", "REGISTRATION", "BATCHES"):
            if notif_prefs.get("Tournament updates") is False:
                logger.info(f"[Notification Suppressed] User {user_id} disabled 'Tournament updates'")
                return None
        elif canonical_cat == "MATCH":
            if notif_prefs.get("Match reminders") is False:
                logger.info(f"[Notification Suppressed] User {user_id} disabled 'Match reminders'")
                return None
        elif canonical_cat == "TEAM":
            if notif_prefs.get("Team invites") is False:
                logger.info(f"[Notification Suppressed] User {user_id} disabled 'Team invites'")
                return None
        elif canonical_cat in ("WALLET", "PAYMENT"):
            if notif_prefs.get("Wallet & payments") is False:
                logger.info(f"[Notification Suppressed] User {user_id} disabled 'Wallet & payments'")
                return None

        # Email copy suppression check
        if notif_prefs.get("Email notifications") is False or notif_prefs.get("Email Notifications") is False:
            send_email_copy = False

    # Normalize notification_type if it was generic PAYMENT
    normalized_type = notification_type
    if normalized_type.upper() == "PAYMENT":
        normalized_type = "WALLET"

    resolved_label = label or resolve_notification_label(
        sender_role=sender_role,
        notification_type=normalized_type,
        category=canonical_cat,
        title=title,
        message=message,
    )

    # Ambiguous title normalization: if title was simply 'Response' and sender was admin/support, normalize to 'Admin Response'
    final_title = title
    if final_title.strip().lower() == "response" and resolved_label == "Admin Response":
        final_title = "Admin Response"

    notif = Notification(
        user_id=user_id,
        type=normalized_type,
        category=canonical_cat,
        title=final_title,
        message=message,
        link=link,
        metadata_json=metadata_json,
        sender_role=sender_role,
        sender_name=sender_name,
        label=resolved_label,
        is_read=False,
        is_cleared=False,
    )
    await db.notifications.insert_one(notif.to_mongo())

    # Centralized Website Notification -> Email Pipeline with Deduplication
    n_type = (normalized_type or "SYSTEM").upper()
    c_type = canonical_cat.upper()
    if send_email_copy and (n_type in USER_FACING_NOTIFICATION_TYPES or c_type in USER_FACING_NOTIFICATION_TYPES):
        try:
            # Generate deterministic idempotency hash to prevent duplicate emails
            event_hash = hashlib.sha256(f"{user_id}:{final_title}:{message}".encode("utf-8")).hexdigest()
            already_sent = await db.sent_email_notifications.find_one({"event_hash": event_hash})
            if not already_sent:
                if not user:
                    user = await db.users.find_one({"$or": [{"_id": user_id}, {"id": user_id}]})
                if user and user.get("email"):
                    u_email = str(user["email"]).strip().lower()
                    u_name = user.get("display_name") or user.get("username") or "Contender"
                    # Fire non-blocking background task so website notification / API never waits on SMTP
                    asyncio.create_task(
                        _safe_send_notification_email(
                            db=db,
                            event_hash=event_hash,
                            u_email=u_email,
                            u_name=u_name,
                            title=final_title,
                            message=message,
                            link=link,
                            notification_type=c_type or n_type,
                        )
                    )
        except Exception as e:
            logger.warning(f"[Ametheus Notification] Email trigger failed silently: {e}")

    return notif


async def migrate_notification_categories(db: AsyncIOMotorDatabase) -> int:
    """
    Safe normalization of historical notification records in MongoDB.
    Assigns canonical category and label, and normalizes ambiguous 'Response' titles
    without deleting or losing any historical records.
    """
    cursor = db.notifications.find({
        "$or": [
            {"category": {"$exists": False}},
            {"category": None},
            {"category": ""},
            {"type": "PAYMENT"},
            {"label": {"$exists": False}},
            {"label": None},
            {"title": {"$regex": "^response$", "$options": "i"}},
        ]
    })
    updated_count = 0
    async for doc in cursor:
        doc_id = doc["_id"]
        n_type = doc.get("type", "SYSTEM")
        raw_title = doc.get("title", "")
        cat = resolve_canonical_category(doc.get("category") or n_type, raw_title)
        s_role = doc.get("sender_role")
        s_name = doc.get("sender_name")
        lbl = doc.get("label") or resolve_notification_label(
            sender_role=s_role,
            notification_type=n_type,
            category=cat,
            title=raw_title,
            message=doc.get("message"),
        )

        update_fields: Dict[str, Any] = {"category": cat, "label": lbl}
        if n_type.upper() == "PAYMENT":
            update_fields["type"] = "WALLET"
        if raw_title.strip().lower() == "response" and lbl == "Admin Response":
            update_fields["title"] = "Admin Response"

        await db.notifications.update_one({"_id": doc_id}, {"$set": update_fields})
        updated_count += 1

    return updated_count


WELCOME_NOTIFICATION_TITLE = "Welcome to Ametheus! 🎮"
WELCOME_NOTIFICATION_MESSAGE = "Your account has been created successfully.\nExplore games, join teams, and participate in upcoming events."


async def ensure_welcome_notification(
    db: AsyncIOMotorDatabase,
    user_id: str,
) -> Optional[Notification]:
    """
    Ensures that a user receives the official Welcome Notification exactly once upon joining.
    Checks whether a welcome notification has already been created for the user.
    If not, creates it reliably as an unread notification with link to /tournaments.
    Execution is strictly isolated from SMTP failures.
    """
    if not user_id:
        return None

    try:
        # Check if already issued
        existing = await db.notifications.find_one({
            "user_id": user_id,
            "title": {"$in": [WELCOME_NOTIFICATION_TITLE, "Welcome to AMETHEUS"]},
        })
        if existing:
            return Notification.from_mongo(existing)

        return await create_notification(
            db=db,
            user_id=user_id,
            title=WELCOME_NOTIFICATION_TITLE,
            message=WELCOME_NOTIFICATION_MESSAGE,
            notification_type="SYSTEM",
            link="/tournaments",
        )
    except Exception as e:
        logger.warning(f"[Ametheus Notification] ensure_welcome_notification failed safely: {e}")
        return None
