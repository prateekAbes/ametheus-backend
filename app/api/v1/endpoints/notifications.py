from typing import Any
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.schemas import NotificationResponse, NotificationListResponse

router = APIRouter()

@router.get("", response_model=NotificationListResponse)
async def get_user_notifications(
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Retrieve all notifications for the authenticated user, ordered by newest first from MongoDB.
    """
    cursor = db.notifications.find({"user_id": current_user.id}).sort("created_at", -1).limit(50)
    notifs = await cursor.to_list(length=50)

    unread_count = await db.notifications.count_documents({
        "user_id": current_user.id,
        "is_read": False,
    })

    items = [
        NotificationResponse(
            id=str(n.get("_id", n.get("id"))),
            type=n.get("type", "SYSTEM"),
            title=n["title"],
            message=n["message"],
            is_read=n.get("is_read", False),
            link=n.get("link"),
            created_at=n.get("created_at").isoformat() if hasattr(n.get("created_at"), "isoformat") else str(n.get("created_at", "")),
        )
        for n in notifs
    ]

    return NotificationListResponse(items=items, unread_count=unread_count)

@router.post("/{notification_id}/read", response_model=NotificationResponse)
async def mark_notification_as_read(
    notification_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Mark a single notification as read.
    """
    notif = await db.notifications.find_one({
        "$or": [{"_id": notification_id}, {"id": notification_id}],
        "user_id": current_user.id,
    })
    if not notif:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )

    await db.notifications.update_one({"_id": notif["_id"]}, {"$set": {"is_read": True}})
    updated_notif = await db.notifications.find_one({"_id": notif["_id"]})

    return NotificationResponse(
        id=str(updated_notif.get("_id", updated_notif.get("id"))),
        type=updated_notif.get("type", "SYSTEM"),
        title=updated_notif["title"],
        message=updated_notif["message"],
        is_read=True,
        link=updated_notif.get("link"),
        created_at=updated_notif.get("created_at").isoformat() if hasattr(updated_notif.get("created_at"), "isoformat") else str(updated_notif.get("created_at", "")),
    )

@router.post("/read-all")
async def mark_all_notifications_as_read(
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Mark all unread notifications for the authenticated user as read.
    """
    result = await db.notifications.update_many(
        {
            "user_id": current_user.id,
            "is_read": False,
        },
        {"$set": {"is_read": True}},
    )

    return {"status": "ok", "updated": result.modified_count}

