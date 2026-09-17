from typing import Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.models.notification import Notification

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
    return notif

