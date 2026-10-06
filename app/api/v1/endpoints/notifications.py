from datetime import datetime, timezone
from typing import Any, Dict, Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.core.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.schemas import NotificationResponse, NotificationListResponse

from app.services.notification_category import (
    resolve_canonical_category,
    get_category_query_filter,
    resolve_notification_label,
)

router = APIRouter()


def _serialize_notification(n: dict) -> NotificationResponse:
    created = n.get("created_at")
    archived = n.get("archived_at")
    raw_title = n.get("title", "")
    n_type = n.get("type")
    cat = n.get("category") or resolve_canonical_category(n_type, raw_title)
    s_role = n.get("sender_role")
    s_name = n.get("sender_name")

    lbl = n.get("label") or resolve_notification_label(
        sender_role=s_role,
        notification_type=n_type,
        category=cat,
        title=raw_title,
        message=n.get("message"),
    )

    title = raw_title
    if title.strip().lower() == "response" and lbl == "Admin Response":
        title = "Admin Response"

    return NotificationResponse(
        id=str(n.get("_id", n.get("id"))),
        type=n.get("type", cat),
        category=cat,
        title=title,
        message=n.get("message", ""),
        is_read=n.get("is_read", False),
        link=n.get("link"),
        created_at=created.isoformat() if hasattr(created, "isoformat") else (str(created) if created else None),
        is_archived=bool(n.get("is_archived", False)),
        archived_at=archived.isoformat() if hasattr(archived, "isoformat") else (str(archived) if archived else None),
        archived_by=n.get("archived_by"),
        archive_reason=n.get("archive_reason"),
        is_cleared=bool(n.get("is_cleared", False)),
        sender_role=s_role,
        sender_name=s_name,
        label=lbl,
    )


@router.get("", response_model=NotificationListResponse)
async def get_user_notifications(
    category: Optional[str] = Query(None, description="Filter notifications by category (e.g. ALL, SYSTEM, TEAM, WALLET, REGISTRATION, BATCHES)"),
    include_archived: bool = Query(False, description="Whether to include archived notifications"),
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Retrieve all notifications for the authenticated user, ordered by newest first from MongoDB.
    Supports filtering by canonical category (WALLET, REGISTRATION, TEAM, SYSTEM, BATCHES).
    Defaults to active notifications (is_archived != True and is_cleared != True).
    """
    valid_user_ids = [str(current_user.id)]
    if getattr(current_user, "clerk_user_id", None):
        valid_user_ids.append(str(current_user.clerk_user_id))

    query: Dict[str, Any] = {"user_id": {"$in": valid_user_ids}}
    if not include_archived:
        query["is_archived"] = {"$ne": True}
        query["is_cleared"] = {"$ne": True}

    if category and category.strip().upper() != "ALL":
        cat_filter = get_category_query_filter(category)
        if cat_filter:
            query.update(cat_filter)

    cursor = db.notifications.find(query).sort("created_at", -1).limit(50)
    notifs = await cursor.to_list(length=50)

    unread_query: Dict[str, Any] = {
        "user_id": {"$in": valid_user_ids},
        "is_read": False,
    }
    if not include_archived:
        unread_query["is_archived"] = {"$ne": True}
        unread_query["is_cleared"] = {"$ne": True}
    unread_count = await db.notifications.count_documents(unread_query)

    items = [_serialize_notification(n) for n in notifs]
    return NotificationListResponse(items=items, unread_count=unread_count)


@router.get("/unread-count")
async def get_unread_notification_count(
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> Any:
    """
    Get the unread notification count for the current user.
    """
    valid_user_ids = [current_user.id]
    if getattr(current_user, "clerk_user_id", None):
        valid_user_ids.append(current_user.clerk_user_id)
    count = await db.notifications.count_documents({
        "user_id": {"$in": valid_user_ids},
        "is_read": False,
        "is_cleared": {"$ne": True},
        "is_archived": {"$ne": True},
    })
    return {"unread_count": count}


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


@router.post("/{notification_id}/clear", response_model=NotificationResponse)
async def clear_user_notification(
    notification_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Clear a single notification from the user's active view.
    Does NOT physically delete the database record, keeping it safe for
    auditing, historical tracking, and troubleshooting.
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

    now_iso = datetime.now(timezone.utc).isoformat()
    await db.notifications.update_one(
        {"_id": notif["_id"]},
        {"$set": {
            "is_cleared": True,
            "is_archived": True,
            "archived_at": now_iso,
            "archived_by": str(current_user.id),
            "archive_reason": "USER_CLEARED",
        }}
    )
    updated_notif = await db.notifications.find_one({"_id": notif["_id"]})
    return _serialize_notification(updated_notif)


@router.post("/clear-all")
async def clear_all_user_notifications(
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Clear all active notifications for the authenticated user from active view.
    Does NOT physically delete database records.
    """
    valid_user_ids = [str(current_user.id)]
    if getattr(current_user, "clerk_user_id", None):
        valid_user_ids.append(str(current_user.clerk_user_id))

    now_iso = datetime.now(timezone.utc).isoformat()
    result = await db.notifications.update_many(
        {
            "user_id": {"$in": valid_user_ids},
            "is_cleared": {"$ne": True},
            "is_archived": {"$ne": True},
        },
        {"$set": {
            "is_cleared": True,
            "is_archived": True,
            "archived_at": now_iso,
            "archived_by": str(current_user.id),
            "archive_reason": "USER_CLEARED_ALL",
        }},
    )
    return {"status": "ok", "cleared_count": result.modified_count}


@router.post("/{notification_id}/archive", response_model=NotificationResponse)
async def archive_user_notification(
    notification_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """
    Archive a single notification for the authenticated user.
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

    now_iso = datetime.now(timezone.utc).isoformat()
    await db.notifications.update_one(
        {"_id": notif["_id"]},
        {"$set": {
            "is_archived": True,
            "archived_at": now_iso,
            "archived_by": str(current_user.id),
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

    await db.notifications.update_one(
        {"_id": notif["_id"]},
        {"$set": {
            "is_archived": False,
            "is_cleared": False,
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
    valid_user_ids = [str(current_user.id)]
    if getattr(current_user, "clerk_user_id", None):
        valid_user_ids.append(str(current_user.clerk_user_id))

    now_iso = datetime.now(timezone.utc).isoformat()
    result = await db.notifications.update_many(
        {
            "user_id": {"$in": valid_user_ids},
            "is_read": True,
            "is_archived": {"$ne": True},
        },
        {"$set": {
            "is_archived": True,
            "archived_at": now_iso,
            "archived_by": str(current_user.id),
            "archive_reason": "USER_CLEARED_READ",
        }},
    )
    return {"status": "ok", "archived_count": result.modified_count}



