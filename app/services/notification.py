from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.notification import Notification

async def create_notification(
    db: AsyncSession,
    user_id: str,
    title: str,
    message: str,
    notification_type: str = "SYSTEM",
    link: Optional[str] = None,
    metadata_json: str = "{}",
) -> Notification:
    """
    Create a persisted notification record for a user.
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
    db.add(notif)
    await db.commit()
    await db.refresh(notif)
    return notif
