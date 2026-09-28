import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.models.notification import Notification

logger = logging.getLogger("ametheus.notifications")

USER_FACING_NOTIFICATION_TYPES = {
    "TOURNAMENT",
    "PAYMENT",
    "MATCH",
    "TEAM",
    "ACCOUNT",
    "SYSTEM",
}


async def _safe_send_notification_email(
    db: AsyncIOMotorDatabase,
    event_hash: str,
    u_email: str,
    u_name: str,
    title: str,
    message: str,
    link: Optional[str] = None,
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
    link: Optional[str] = None,
    metadata_json: str = "{}",
) -> Notification:
    """
    Create a persisted notification record for a user in MongoDB.
    Also dispatches an asynchronous email copy to the user's registered address
    for important user-facing events, with duplicate protection.
    """
    notif = Notification(
        user_id=user_id,
        type=notification_type,
        title=title,
        message=message,
        link=link,
        metadata_json=metadata_json,
        is_read=False,
    )
    await db.notifications.insert_one(notif.to_mongo())

    # Centralized Website Notification -> Email Pipeline with Deduplication
    n_type = (notification_type or "SYSTEM").upper()
    if n_type in USER_FACING_NOTIFICATION_TYPES:
        try:
            # Generate deterministic idempotency hash to prevent duplicate emails
            event_hash = hashlib.sha256(f"{user_id}:{title}:{message}".encode("utf-8")).hexdigest()
            already_sent = await db.sent_email_notifications.find_one({"event_hash": event_hash})
            if not already_sent:
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
                            title=title,
                            message=message,
                            link=link,
                        )
                    )
        except Exception as e:
            logger.warning(f"[Ametheus Notification] Email trigger failed silently: {e}")

    return notif


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
