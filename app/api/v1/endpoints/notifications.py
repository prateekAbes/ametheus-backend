from datetime import datetime, timezone
from typing import Any, Dict
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.schemas import NotificationResponse, NotificationListResponse

router = APIRouter()


def _serialize_notification(n: dict) -> NotificationResponse:
    created = n.get("created_at")
    archived = n.get("archived_at")
    return NotificationResponse(
        id=str(n.get("_id", n.get("id"))),
        type=n.get("type", "SYSTEM"),
        title=n.get("title", ""),
        message=n.get("message", ""),
        is_read=n.get("is_read", False),
        link=n.get("link"),
        created_at=created.isoformat() if hasattr(created, "isoformat") else (str(created) if created else None),
        is_archived=bool(n.get("is_archived", False)),
        archived_at=archived.isoformat() if hasattr(archived, "isoformat") else (str(archived) if archived else None),
        archived_by=n.get("archived_by"),
        archive_reason=n.get("archive_reason"),
    )


@router.get("", response_model=NotificationListResponse)
async def get_user_notifications(
    include_archived: bool = Query(False, description="Whether to include archived notifications"),
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Retrieve all notifications for the authenticated user, ordered by newest first from MongoDB.
    Defaults to active notifications (is_archived != True).
    """
    valid_user_ids = [str(current_user.id)]
    if getattr(current_user, "clerk_user_id", None):
        valid_user_ids.append(str(current_user.clerk_user_id))

    query: Dict[str, Any] = {"user_id": {"$in": valid_user_ids}}
    if not include_archived:
        query["is_archived"] = {"$ne": True}

    cursor = db.notifications.find(query).sort("created_at", -1).limit(50)
    notifs = await cursor.to_list(length=50)

    unread_query: Dict[str, Any] = {
        "user_id": {"$in": valid_user_ids},
        "is_read": False,
    }
    if not include_archived:
        unread_query["is_archived"] = {"$ne": True}
    unread_count = await db.notifications.count_documents(unread_query)

    items = [_serialize_notification(n) for n in notifs]
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
    valid_user_ids = [str(current_user.id)]
    if getattr(current_user, "clerk_user_id", None):
        valid_user_ids.append(str(current_user.clerk_user_id))

    notif = await db.notifications.find_one({
        "$or": [{"_id": notification_id}, {"id": notification_id}],
        "user_id": {"$in": valid_user_ids},
    })
    if not notif:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found",
        )

    await db.notifications.update_one({"_id": notif["_id"]}, {"$set": {"is_read": True}})
    updated_notif = await db.notifications.find_one({"_id": notif["_id"]})
    return _serialize_notification(updated_notif)


@router.post("/read-all")
async def mark_all_notifications_as_read(
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Mark all unread notifications for the authenticated user as read.
    """
    valid_user_ids = [str(current_user.id)]
    if getattr(current_user, "clerk_user_id", None):
        valid_user_ids.append(str(current_user.clerk_user_id))

    result = await db.notifications.update_many(
        {
            "user_id": {"$in": valid_user_ids},
            "is_read": False,
        },
        {"$set": {"is_read": True}},
    )

    return {"status": "ok", "updated": result.modified_count}


@router.post("/{notification_id}/archive", response_model=NotificationResponse)
async def archive_user_notification(
    notification_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Archive a single notification for the authenticated user.
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

    now_iso = datetime.now(timezone.utc).isoformat()
    await db.notifications.update_one(
        {"_id": notif["_id"]},
        {"$set": {
            "is_archived": True,
            "archived_at": now_iso,
            "archived_by": current_user.id,
            "archive_reason": "USER_ACTION",
        }}
    )
    updated_notif = await db.notifications.find_one({"_id": notif["_id"]})
    return _serialize_notification(updated_notif)


@router.post("/{notification_id}/unarchive", response_model=NotificationResponse)
async def unarchive_user_notification(
    notification_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Unarchive / restore a single notification for the authenticated user.
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

    await db.notifications.update_one(
        {"_id": notif["_id"]},
        {"$set": {
            "is_archived": False,
            "archived_at": None,
            "archived_by": None,
            "archive_reason": None,
        }}
    )
    updated_notif = await db.notifications.find_one({"_id": notif["_id"]})
    return _serialize_notification(updated_notif)


@router.post("/archive-read")
async def archive_read_notifications(
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Archive all read notifications for the current user.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    result = await db.notifications.update_many(
        {
            "user_id": current_user.id,
            "is_read": True,
            "is_archived": {"$ne": True},
        },
        {"$set": {
            "is_archived": True,
            "archived_at": now_iso,
            "archived_by": current_user.id,
            "archive_reason": "USER_CLEARED_READ",
        }},
    )
    return {"status": "ok", "archived_count": result.modified_count}


