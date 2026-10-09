import asyncio
import re
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, get_current_user, get_current_user_optional, require_organizer
from app.models.user import User
from app.models.support import SupportTicket, SupportMessage, SupportLogEntry, generate_ticket_id
from app.schemas.support import (
    SupportTicketCreateRequest,
    SupportTicketUpdateRequest,
    SupportTicketReplyRequest,
    SupportTicketResponse,
    SupportTicketPublicResponse,
    SupportTicketListResponse,
    SupportLogResponse,
    SupportSettingsResponse,
    SupportSettingsUpdateRequest,
    SupportEmailRetryRequest,
    SupportEmailStatus,
)
from app.services.email_service import (
    send_ticket_confirmation_email,
    send_admin_ticket_notification_email,
    format_friendly_ticket_status,
)
from app.services.audit import record_audit_log
from app.core.config import settings
from app.schemas.archive import (
    ArchiveFilterRequest,
    ArchivePreviewResponse,
    ArchiveExecutionResponse,
    UnarchiveResponse,
)
from app.services.archive_service import (
    preview_archive,
    execute_archive,
    archive_single_record,
    unarchive_record,
)

router = APIRouter()

VALID_STATUSES = {
    "NEW",
    "OPEN",
    "WAITING_FOR_USER",
    "WAITING_FOR_ADMIN",
    "UNREAD",
    "IN_PROGRESS",
    "ON_HOLD",
    "RESOLVED",
    "CLOSED",
    "ARCHIVED",
}
VALID_CATEGORIES = {
    "PAYMENT",
    "WALLET",
    "TOURNAMENT",
    "REGISTRATION",
    "TEAM",
    "MATCH",
    "ACCOUNT",
    "TECHNICAL_ISSUE",
    "GAME_ISSUE",
    "OTHER",
}


from app.services.email_config_service import get_support_email

async def _get_configured_support_email(db: AsyncIOMotorDatabase) -> str:
    """Returns the platform-configured support destination email for admin alerts."""
    return getattr(settings, "SUPPORT_ADMIN_EMAIL", None) or "support@tenetadmissions.com"


def _clean_message_dict(m: dict, ticket_id: str) -> dict:
    msg = dict(m)
    msg["id"] = str(msg.get("id") or msg.get("_id") or f"msg_{datetime.now(timezone.utc).timestamp()}")
    msg["ticket_id"] = str(msg.get("ticket_id") or ticket_id)
    msg["sender_name"] = str(msg.get("sender_name") or "User")
    msg["message"] = str(msg.get("message") or msg.get("body_text") or "")
    role = str(msg.get("sender_role") or "USER").upper()
    if role not in ("USER", "ADMIN", "SYSTEM"):
        role = "USER"
    msg["sender_role"] = role
    mtype = str(msg.get("message_type") or role).upper()
    if mtype not in ("USER", "ADMIN", "SYSTEM"):
        mtype = role
    msg["message_type"] = mtype
    msg["direction"] = str(msg.get("direction") or ("outbound" if role in ("ADMIN", "SYSTEM") else "inbound"))
    msg["created_at"] = str(msg.get("created_at") or datetime.now(timezone.utc).isoformat())
    raw_attachments = msg.get("attachments", [])
    clean_attachments = []
    if isinstance(raw_attachments, list):
        for att in raw_attachments:
            if isinstance(att, dict):
                clean_attachments.append({
                    "id": str(att.get("id") or "att_1"),
                    "filename": str(att.get("filename") or "attachment"),
                    "content_type": str(att.get("content_type") or att.get("contentType") or "application/octet-stream"),
                    "size": int(att.get("size") or 0),
                    "url": att.get("url") or att.get("download_url"),
                })
    msg["attachments"] = clean_attachments
    return msg


def _clean_ticket_doc(doc: dict) -> dict:
    item = dict(doc)
    item["id"] = str(item.get("_id", item.get("id", "")))
    if "_id" in item:
        item["_id"] = str(item["_id"])
    if not item.get("ticket_id"):
        item["ticket_id"] = item["id"]
    item["is_archived"] = bool(item.get("is_archived", False))
    item["archived_at"] = str(item.get("archived_at")) if item.get("archived_at") else None
    item["archived_by"] = item.get("archived_by")
    item["archive_reason"] = item.get("archive_reason")
    item["has_new_reply"] = bool(item.get("has_new_reply", False))
    item["last_message_at"] = item.get("last_message_at")
    item["last_message_by"] = item.get("last_message_by")

    # Strict normalization to the standard support ticket statuses
    raw_status = str(item.get("status", "OPEN")).strip().upper().replace(" ", "_")
    if raw_status in ("NEW", "UNREAD"):
        raw_status = "OPEN"
    elif raw_status == "ON_HOLD":
        raw_status = "IN_PROGRESS"
    elif raw_status not in ("OPEN", "IN_PROGRESS", "WAITING_FOR_USER", "WAITING_FOR_ADMIN", "RESOLVED", "CLOSED", "ARCHIVED"):
        # Prevent tournament/game statuses like REGISTRATION_OPEN from ever appearing
        raw_status = "OPEN"
    item["status"] = raw_status

    tid = item.get("ticket_id") or item.get("id", "")
    raw_msgs = item.get("messages", [])
    if not raw_msgs and item.get("description"):
        raw_msgs = [
            {
                "id": "msg_initial",
                "ticket_id": tid,
                "sender_id": item.get("user_id") or "user",
                "sender_name": item.get("name", "Contender"),
                "sender_role": "USER",
                "message_type": "USER",
                "direction": "inbound",
                "message": item.get("description", ""),
                "created_at": str(item.get("created_at") or datetime.now(timezone.utc).isoformat()),
                "attachments": [],
            }
        ]
    item["messages"] = [_clean_message_dict(m, tid) for m in raw_msgs]
    return item


@router.get("/settings", response_model=SupportSettingsResponse)
async def get_support_settings():
    """Returns the currently configured support destination email from server environment."""
    return SupportSettingsResponse(support_email=settings.SUPPORT_EMAIL)


@router.post("/tickets", response_model=SupportTicketPublicResponse, status_code=status.HTTP_201_CREATED)
async def create_support_ticket(
    payload: SupportTicketCreateRequest,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Creates a real support ticket in the database, generates a unique Ticket ID,

    records an audit log, and dispatches confirmation emails.
    """
    raw_name = (payload.name or "").strip() or (current_user.display_name or current_user.username if current_user else "Contender")
    raw_email = (payload.email or "").strip() or (current_user.email if current_user else "")
    email_clean = raw_email.lower()
    if not email_clean or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_clean):
        email_clean = (current_user.email if current_user and current_user.email else "support-user@ametheus.games").strip().lower()

    # Normalize category
    cat_raw = payload.category or "OTHER"
    cat_upper = cat_raw.strip().upper().replace(" ", "_").replace("-", "_")
    if cat_upper not in VALID_CATEGORIES:
        cat_upper = "OTHER"

    # Payment type validation
    payment_type = payload.payment_type.strip().upper() if payload.payment_type else None
    if payment_type in ("NONE", "N/A", "NOT APPLICABLE", ""):
        payment_type = "NOT_APPLICABLE"

    desc = (payload.description or "").strip()
    if not desc and payload.message:
        desc = payload.message.strip()
    if payload.subject and payload.subject not in desc:
        desc = f"[{payload.subject.strip()}] {desc}" if desc else payload.subject.strip()
    if not desc:
        desc = "General support request"

    ticket_id = generate_ticket_id()
    now_iso = datetime.now(timezone.utc).isoformat()

    # User attribution
    user_id = current_user.id if current_user else None
    user_handle = current_user.username if current_user else None
    actor_name = current_user.display_name or current_user.username if current_user else raw_name
    actor_role = "USER" if current_user else "GUEST"

    # Initial creation log entry
    init_log = SupportLogEntry(
        ticket_id=ticket_id,
        action="TICKET_CREATED",
        timestamp=now_iso,
        actor=actor_name,
        actor_role=actor_role,
        details=f"Support ticket created under category '{cat_upper}'.",
    )

    ticket = SupportTicket(
        id=ticket_id,
        ticket_id=ticket_id,
        name=raw_name,
        email=email_clean,
        user_id=user_id,
        user_handle=user_handle,
        category=cat_upper,
        payment_type=payment_type,
        description=desc,
        status="WAITING_FOR_ADMIN",
        has_new_reply=True,
        last_message_at=now_iso,
        last_message_by=raw_name,
        email_status="PENDING",
        messages=[],
        logs=[init_log],
        created_at=now_iso,
        updated_at=now_iso,
    )

    initial_user_msg = SupportMessage(
        ticket_id=ticket_id,
        thread_id=ticket_id,
        sender_id=user_id,
        sender_email=email_clean,
        sender_name=raw_name,
        recipient_email=settings.SUPPORT_EMAIL,
        subject=payload.subject or f"Support Ticket [{ticket_id}]",
        body_text=desc,
        message=desc,
        message_id=f"<AMT-INIT-{ticket_id}@ametheus.games>",
        direction="inbound",
        message_type="USER",
        sender_role="USER",
        created_at=now_iso,
    )
    initial_user_msg_doc = initial_user_msg.to_mongo()
    await db.support_messages.insert_one(initial_user_msg_doc)

    doc = ticket.to_mongo()
    doc["messages"] = [initial_user_msg_doc]
    await db.support_tickets.insert_one(doc)
    await db.support_logs.insert_one(init_log.to_mongo())

    # Create persistent user in-app notification in DB
    target_uid = user_id
    if not target_uid and email_clean:
        try:
            u_doc = await db.users.find_one({"email": email_clean})
            if u_doc:
                target_uid = u_doc.get("id") or str(u_doc.get("_id"))
        except Exception:
            pass
    if target_uid and not str(target_uid).startswith("guest"):
        try:
            from app.services.notification import create_notification
            await create_notification(
                db=db,
                user_id=str(target_uid),
                title=f"Support Ticket Submitted [{ticket_id}]",
                message=f"Your support ticket for '{cat_upper}' has been received. Our operations team will review it shortly.",
                notification_type="SYSTEM",
                category="SYSTEM",
                sender_role="SYSTEM",
                label="System Notification",
                link="/support",
                send_email_copy=False,
            )
        except Exception:
            pass

    # Dispatch emails asynchronously with defensive error isolation
    ticket_dict = {
        "id": ticket_id,
        "ticket_id": ticket_id,
        "name": ticket.name,
        "email": ticket.email,
        "user_id": user_id,
        "category": cat_upper,
        "payment_type": payment_type,
        "description": ticket.description,
        "created_at": now_iso,
    }

    configured_support_email = await _get_configured_support_email(db)

    user_mail_ok = False
    user_mail_err = None
    confirm_msg_id = None
    admin_mail_ok = False
    admin_mail_err = None

    # Dispatch emails concurrently with defensive error isolation
    async def _send_user_mail():
        return await send_ticket_confirmation_email(ticket_dict)

    async def _send_admin_mail():
        # Only notify internal support desk if configured address is not the sender's own email
        if configured_support_email and configured_support_email.lower() != email_clean:
            return await send_admin_ticket_notification_email(
                ticket_dict,
                admin_recipient=configured_support_email,
            )
        return True, None

    try:
        results = await asyncio.wait_for(
            asyncio.gather(_send_user_mail(), _send_admin_mail(), return_exceptions=True),
            timeout=2.0,
        )
        if isinstance(results[0], tuple):
            user_mail_ok = results[0][0]
            user_mail_err = results[0][1] if len(results[0]) > 1 else None
            confirm_msg_id = results[0][2] if len(results[0]) > 2 else None
        elif isinstance(results[0], Exception):
            user_mail_err = str(results[0])

        if isinstance(results[1], tuple):
            admin_mail_ok = results[1][0]
            admin_mail_err = results[1][1] if len(results[1]) > 1 else None
        elif isinstance(results[1], Exception):
            admin_mail_err = str(results[1])
    except asyncio.TimeoutError:
        user_mail_err = "Email dispatch timeout (proceeding in background)"
        admin_mail_err = "Email dispatch timeout (proceeding in background)"
    except Exception as e:
        user_mail_err = str(e)
        admin_mail_err = str(e)

    if user_mail_ok and admin_mail_ok:
        email_status = "SENT"
    elif user_mail_ok or admin_mail_ok:
        email_status = "PARTIAL"
    else:
        email_status = "FAILED"

    user_email_status = "SENT" if user_mail_ok else "FAILED"
    support_email_status = "SENT" if admin_mail_ok else "FAILED"

    email_error_summary = None
    if email_status != "SENT":
        errs = []
        if user_mail_err:
            errs.append(f"User email: {user_mail_err}")
        if admin_mail_err:
            errs.append(f"Admin email: {admin_mail_err}")
        email_error_summary = "; ".join(errs)

    # Record email log entry
    email_log = SupportLogEntry(
        ticket_id=ticket_id,
        action="EMAIL_SENT" if email_status == "SENT" else ("EMAIL_PARTIAL" if email_status == "PARTIAL" else "EMAIL_FAILED"),
        timestamp=datetime.now(timezone.utc).isoformat(),
        actor="SYSTEM",
        actor_role="SYSTEM",
        details="Confirmation email delivered to user and admin" if email_status == "SENT" else f"Email status: {email_status}. Notes: {email_error_summary}",
    )

    system_confirm_msg = SupportMessage(
        ticket_id=ticket_id,
        thread_id=ticket_id,
        sender_email=settings.SUPPORT_EMAIL,
        sender_name="Ametheus System",
        recipient_email=email_clean,
        subject=f"Ametheus Support Ticket Created — {ticket_id}",
        body_text=f"Automated confirmation receipt dispatched to {email_clean}." if user_mail_ok else f"Automated confirmation attempted. Status: {user_mail_err or 'queued'}",
        message=f"Automated confirmation receipt dispatched to {email_clean}." if user_mail_ok else f"Automated confirmation attempted. Status: {user_mail_err or 'queued'}",
        message_id=confirm_msg_id or f"<AMT-CONFIRM-{ticket_id}@ametheus.games>",
        direction="outbound",
        message_type="SYSTEM",
        sender_role="SYSTEM",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    sys_doc = system_confirm_msg.to_mongo()
    await db.support_messages.insert_one(sys_doc)

    try:
        await db.support_logs.insert_one(email_log.to_mongo())
        await db.support_tickets.update_one(
            {"_id": ticket_id},
            {
                "$set": {
                    "email_status": email_status,
                    "email_error": email_error_summary,
                    "user_email_status": user_email_status,
                    "support_email_status": support_email_status,
                },
                "$push": {
                    "messages": sys_doc,
                    "logs": email_log.to_mongo(),
                },
            },
        )
    except Exception:
        pass

    return SupportTicketPublicResponse(
        id=ticket_id,
        ticket_id=ticket_id,
        name=ticket.name,
        category=cat_upper,
        payment_type=payment_type,
        status="OPEN",
        created_at=now_iso,
        email_status=email_status,
        email_error=email_error_summary,
        user_email=SupportEmailStatus(
            status="sent" if user_mail_ok else "failed",
            error=user_mail_err,
        ),
        support_email=SupportEmailStatus(
            status="sent" if admin_mail_ok else "failed",
            error=admin_mail_err,
        ),
        ticket_created=True,
        message=f"Support ticket {ticket_id} created successfully.",
    )



@router.get("/my-tickets", response_model=List[SupportTicketResponse])
@router.get("/tickets/my", response_model=List[SupportTicketResponse])
async def list_my_support_tickets(
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = {
        "$or": [
            {"user_id": current_user.id},
            {"email": current_user.email.lower() if current_user.email else "---"},
        ],
        "is_archived": {"$ne": True},
    }
    cursor = db.support_tickets.find(query).sort("created_at", -1).limit(50)
    docs = await cursor.to_list(length=50)
    return [SupportTicketResponse(**_clean_ticket_doc(d)) for d in docs]


@router.get("/tickets", response_model=SupportTicketListResponse)
async def list_support_tickets(
    status_filter: Optional[str] = Query(None, alias="status"),
    category: Optional[str] = None,
    search: Optional[str] = None,
    is_archived: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to retrieve and search support tickets with status and category filtering."""
    query: dict = {}
    if is_archived:
        query["is_archived"] = True
    else:
        query["is_archived"] = {"$ne": True}

    if status_filter and status_filter.upper() != "ALL":
        st = status_filter.upper()
        if st == "RESOLVED_CLOSED":
            query["status"] = {"$in": ["RESOLVED", "CLOSED"]}
        else:
            query["status"] = st

    if category and category.upper() != "ALL":
        query["category"] = category.upper()

    if search:
        search_rgx = {"$regex": re.escape(search.strip()), "$options": "i"}
        query["$or"] = [
            {"ticket_id": search_rgx},
            {"id": search_rgx},
            {"name": search_rgx},
            {"email": search_rgx},
            {"description": search_rgx},
        ]

    total = await db.support_tickets.count_documents(query)
    cursor = db.support_tickets.find(query).sort("created_at", -1).skip(skip).limit(limit)
    docs = await cursor.to_list(length=limit)

    tickets = [_clean_ticket_doc(d) for d in docs]
    return SupportTicketListResponse(tickets=tickets, total=total)


@router.get("/tickets/counts")
async def get_support_ticket_counts(
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Returns real-time totals of active (non-archived) vs archived support tickets."""
    active_count = await db.support_tickets.count_documents({"is_archived": {"$ne": True}, "is_dummy": {"$ne": True}})
    archived_count = await db.support_tickets.count_documents({"is_archived": True, "is_dummy": {"$ne": True}})
    return {
        "active": active_count,
        "archived": archived_count,
        "total": active_count + archived_count,
    }


@router.post("/tickets/archive/preview", response_model=ArchivePreviewResponse)
async def preview_support_archive_endpoint(
    payload: ArchiveFilterRequest,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    count = await preview_archive(db, "support_tickets", payload)
    return ArchivePreviewResponse(
        section="support_tickets",
        matching_count=count,
        filters_applied=payload.model_dump(exclude_none=True),
    )


@router.post("/tickets/archive", response_model=ArchiveExecutionResponse)
async def execute_support_archive_endpoint(
    payload: ArchiveFilterRequest,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    count = await execute_archive(db, "support_tickets", payload, admin_user)
    return ArchiveExecutionResponse(
        success=True,
        section="support_tickets",
        archived_count=count,
        message=f"Successfully archived {count} support tickets.",
    )


@router.post("/tickets/{ticket_id}/archive", response_model=SupportTicketResponse)
async def archive_single_support_ticket(
    ticket_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    await archive_single_record(db, "support_tickets", ticket_id, admin_user)
    doc = await db.support_tickets.find_one({"$or": [{"_id": ticket_id}, {"ticket_id": ticket_id}, {"id": ticket_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return SupportTicketResponse(**_clean_ticket_doc(doc))


@router.post("/tickets/{ticket_id}/unarchive", response_model=SupportTicketResponse)
@router.post("/tickets/{ticket_id}/restore", response_model=SupportTicketResponse)
async def unarchive_single_support_ticket(
    ticket_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    await unarchive_record(db, "support_tickets", ticket_id, admin_user)
    doc = await db.support_tickets.find_one({"$or": [{"_id": ticket_id}, {"ticket_id": ticket_id}, {"id": ticket_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return SupportTicketResponse(**_clean_ticket_doc(doc))


@router.get("/tickets/{ticket_id}", response_model=SupportTicketResponse)
async def get_support_ticket_details(
    ticket_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to retrieve a full support ticket by Ticket ID along with its audit logs."""
    doc = await db.support_tickets.find_one({"$or": [{"_id": ticket_id}, {"ticket_id": ticket_id}, {"id": ticket_id}]})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Support ticket '{ticket_id}' not found.",
        )

    clean_tid = doc.get("ticket_id", ticket_id)

    # Retrieve all standalone messages associated with this ticket
    msg_cursor = db.support_messages.find({"ticket_id": clean_tid}).sort("created_at", 1)
    db_msgs = await msg_cursor.to_list(length=200)

    # Retrieve all standalone logs associated with this ticket
    log_cursor = db.support_logs.find({"ticket_id": clean_tid}).sort("timestamp", 1)
    db_logs = await log_cursor.to_list(length=100)

    cleaned = _clean_ticket_doc(doc)
    if db_msgs:
        existing_msg_ids = {m["id"] for m in cleaned.get("messages", [])}
        for m in db_msgs:
            m_id = str(m.get("id") or m.get("_id"))
            if m_id not in existing_msg_ids:
                cleaned.setdefault("messages", []).append(_clean_message_dict(m, clean_tid))
        cleaned["messages"].sort(key=lambda x: str(x.get("created_at", "")))

    if db_logs:
        # Merge logs cleanly
        log_ids = {l["id"] for l in cleaned.get("logs", [])}
        for l in db_logs:
            if l.get("id") not in log_ids:
                cleaned.setdefault("logs", []).append(l)

    return SupportTicketResponse(**cleaned)


@router.patch("/tickets/{ticket_id}", response_model=SupportTicketResponse)
async def update_support_ticket(
    ticket_id: str,
    payload: SupportTicketUpdateRequest,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to update ticket status or add internal administrative notes."""
    doc = await db.support_tickets.find_one({"$or": [{"_id": ticket_id}, {"ticket_id": ticket_id}, {"id": ticket_id}]})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Support ticket '{ticket_id}' not found.",
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    update_fields: dict = {"updated_at": now_iso}
    new_logs: List[dict] = []
    actor_name = admin_user.display_name or admin_user.username or "Admin Arbiter"

    if payload.status:
        st_upper = payload.status.strip().upper().replace(" ", "_")
        if st_upper in ("NEW", "UNREAD"):
            st_upper = "OPEN"
        elif st_upper == "ON_HOLD":
            st_upper = "IN_PROGRESS"
        if st_upper not in VALID_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status '{payload.status}'. Allowed: ['OPEN', 'IN_PROGRESS', 'WAITING_FOR_USER', 'WAITING_FOR_ADMIN', 'RESOLVED', 'CLOSED']",
            )
        old_status = doc.get("status", "OPEN")
        if old_status != st_upper:
            update_fields["status"] = st_upper
            if st_upper in ("RESOLVED", "CLOSED"):
                update_fields["resolved_at"] = now_iso

            status_log = SupportLogEntry(
                ticket_id=doc.get("ticket_id", ticket_id),
                action="STATUS_CHANGED",
                timestamp=now_iso,
                actor=actor_name,
                actor_role="ADMIN",
                details=f"Status changed from {old_status} to {st_upper}.",
            )
            new_logs.append(status_log.to_mongo())

    if payload.internal_notes is not None:
        update_fields["internal_notes"] = payload.internal_notes.strip()
        note_log = SupportLogEntry(
            ticket_id=doc.get("ticket_id", ticket_id),
            action="NOTE_ADDED",
            timestamp=now_iso,
            actor=actor_name,
            actor_role="ADMIN",
            details="Internal administrative notes updated.",
        )
        new_logs.append(note_log.to_mongo())

    update_op: dict = {"$set": update_fields}
    if new_logs:
        update_op["$push"] = {"logs": {"$each": new_logs}}
        for nl in new_logs:
            await db.support_logs.insert_one(nl)

    await db.support_tickets.update_one({"_id": doc["_id"]}, update_op)

    updated_doc = await db.support_tickets.find_one({"_id": doc["_id"]})
    return SupportTicketResponse(**_clean_ticket_doc(updated_doc))


@router.get("/users-with-tickets")
async def get_users_with_support_tickets(
    search: Optional[str] = None,
    is_archived: bool = Query(False),
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    User-centric 1st-level organization:
    Groups all tickets by user, reporting ticket counts, active statuses, and latest ticket activity.
    """
    match_query: dict = {"is_archived": True if is_archived else {"$ne": True}}
    if search:
        search_rgx = {"$regex": re.escape(search.strip()), "$options": "i"}
        match_query["$or"] = [
            {"email": search_rgx},
            {"name": search_rgx},
            {"ticket_id": search_rgx},
            {"description": search_rgx},
        ]

    pipeline = [
        {"$match": match_query},
        {"$sort": {"created_at": -1}},
        {
            "$group": {
                "_id": {"$ifNull": ["$email", "$user_id"]},
                "email": {"$first": "$email"},
                "user_name": {"$first": "$name"},
                "user_id": {"$first": "$user_id"},
                "total_tickets": {"$sum": 1},
                "open_tickets": {
                    "$sum": {
                        "$cond": [{"$in": ["$status", ["OPEN", "NEW", "IN_PROGRESS", "WAITING_FOR_ADMIN", "UNREAD"]]}, 1, 0]
                    }
                },
                "latest_activity": {"$first": "$created_at"},
                "tickets": {"$push": "$$ROOT"},
            }
        },
        {"$sort": {"open_tickets": -1, "latest_activity": -1}},
    ]

    results = await db.support_tickets.aggregate(pipeline).to_list(100)
    for r in results:
        r["tickets"] = [_clean_ticket_doc(t) for t in r.get("tickets", [])]
        user_id = r.get("user_id")
        email = r.get("email")
        u_doc = None
        if user_id:
            u_doc = await db.users.find_one({"$or": [{"_id": user_id}, {"id": user_id}, {"clerk_user_id": user_id}]})
        if not u_doc and email:
            u_doc = await db.users.find_one({"email": email.lower()})
        if u_doc:
            r["username"] = u_doc.get("username") or r.get("user_name")
            r["display_name"] = u_doc.get("display_name") or u_doc.get("username")
        else:
            r["username"] = r.get("user_name")
            r["display_name"] = r.get("user_name")
    return results


@router.post("/tickets/{ticket_id}/reply", response_model=SupportTicketResponse)
async def reply_support_ticket(
    ticket_id: str,
    payload: SupportTicketReplyRequest,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Admin sends a direct reply in the ticket conversation thread.
    - Sends email via Brevo SMTP with In-Reply-To and References threading headers
    - Saves ADMIN SupportMessage to support_messages collection and ticket.messages
    - Transitions ticket status to WAITING_FOR_USER (or RESOLVED/CLOSED if specified)
    - Resets has_new_reply flag to False
    - Dispatches in-app notification to user
    - Records audit and support logs
    """
    import secrets
    from app.services.notification import create_notification
    from app.services.email_service import send_support_email_threaded

    doc = await db.support_tickets.find_one({"$or": [{"_id": ticket_id}, {"ticket_id": ticket_id}, {"id": ticket_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Support ticket not found")

    clean_tid = doc.get("ticket_id", ticket_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    actor_name = admin_user.display_name or admin_user.username or "Ametheus Arbiter"

    # Status transition: Admin reply sets status to WAITING_FOR_USER unless RESOLVED or CLOSED was chosen
    raw_target_status = (payload.status or "WAITING_FOR_USER").strip().upper().replace(" ", "_")
    if raw_target_status in ("RESOLVED", "CLOSED"):
        new_status = raw_target_status
    else:
        new_status = "WAITING_FOR_USER"

    # Fetch previous messages to extract Message-IDs for threading
    prev_messages = await db.support_messages.find({"ticket_id": clean_tid}).sort("created_at", 1).to_list(length=200)
    all_msg_ids = [m.get("message_id") for m in prev_messages if m.get("message_id")]
    last_msg_id = all_msg_ids[-1] if all_msg_ids else None
    in_reply_to = last_msg_id
    references_header = " ".join(all_msg_ids) if all_msg_ids else None

    user_email = (doc.get("email") or "").strip().lower()
    admin_msg_id = None

    if user_email:
        friendly_status = format_friendly_ticket_status(new_status)
        category_name = (doc.get("category") or "General Inquiry").replace("_", " ").title()
        from app.services.email_template_system import build_support_admin_reply_email
        email_subject, email_text, email_html = build_support_admin_reply_email(
            user_name=doc.get("name", "Contender"),
            ticket_id=clean_tid,
            category=category_name,
            admin_message=payload.message,
            friendly_status=friendly_status,
        )
        try:
            _, _, admin_msg_id = await send_support_email_threaded(
                to_email=user_email,
                subject=email_subject,
                text_content=email_text,
                html_content=email_html,
                in_reply_to=in_reply_to,
                references=references_header,
                ticket_id=clean_tid,
            )
        except Exception:
            pass
    else:
        email_subject = f"Re: [Ticket {clean_tid}] {doc.get('category')} — Ametheus Support"

    if not admin_msg_id:
        admin_msg_id = f"<AMT-REPLY-{secrets.token_hex(6)}@ametheus.games>"

    # Save ADMIN SupportMessage
    admin_msg = SupportMessage(
        ticket_id=clean_tid,
        thread_id=clean_tid,
        sender_id=admin_user.id,
        sender_email=settings.SUPPORT_EMAIL,
        sender_name=actor_name,
        recipient_email=user_email,
        subject=email_subject,
        body_text=payload.message.strip(),
        message=payload.message.strip(),
        message_id=admin_msg_id,
        in_reply_to=in_reply_to,
        references=all_msg_ids,
        direction="outbound",
        message_type="ADMIN",
        sender_role="ADMIN",
        created_at=now_iso,
    )
    admin_msg_doc = admin_msg.to_mongo()
    await db.support_messages.insert_one(admin_msg_doc)

    reply_log = SupportLogEntry(
        ticket_id=clean_tid,
        action="ADMIN_REPLY",
        timestamp=now_iso,
        actor=actor_name,
        actor_role="ADMIN",
        details=f"Admin replied: {payload.message[:60]}...",
    )
    await db.support_logs.insert_one(reply_log.to_mongo())

    update_fields = {
        "status": new_status,
        "has_new_reply": False,
        "last_message_at": now_iso,
        "last_message_by": actor_name,
        "updated_at": now_iso,
    }
    if new_status in ("RESOLVED", "CLOSED"):
        update_fields["resolved_at"] = now_iso

    await db.support_tickets.update_one(
        {"_id": doc["_id"]},
        {
            "$set": update_fields,
            "$push": {
                "messages": admin_msg_doc,
                "logs": reply_log.to_mongo(),
            },
        },
    )

    user_id = doc.get("user_id")
    if user_id and not str(user_id).startswith("guest"):
        try:
            admin_role = getattr(admin_user, "role", "ADMIN")
            admin_name = getattr(admin_user, "username", None) or "Ametheus Support Team"
            await create_notification(
                db=db,
                user_id=str(user_id),
                title="Admin Response",
                message=f"Your support ticket has received a new response from the Ametheus support team: {payload.message[:120]}",
                notification_type="SUPPORT",
                category="SYSTEM",
                sender_role=admin_role,
                sender_name=admin_name,
                label="Admin Response",
                link="/support",
            )
        except Exception:
            pass

    refreshed = await db.support_tickets.find_one({"_id": doc["_id"]})
    clean_resp = _clean_ticket_doc(refreshed)
    # Ensure all messages including newest are returned
    all_current_msgs = await db.support_messages.find({"ticket_id": clean_tid}).sort("created_at", 1).to_list(length=200)
    if all_current_msgs:
        clean_resp["messages"] = [_clean_message_dict(m, clean_tid) for m in all_current_msgs]
    return SupportTicketResponse(**clean_resp)


@router.post("/tickets/{ticket_id}/retry-email", response_model=SupportTicketResponse)
async def retry_support_ticket_email(
    ticket_id: str,
    payload: SupportEmailRetryRequest,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Retries sending confirmation and/or notification email for a ticket without duplicating the ticket."""
    doc = await db.support_tickets.find_one({"$or": [{"_id": ticket_id}, {"ticket_id": ticket_id}, {"id": ticket_id}]})
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Support ticket '{ticket_id}' not found.",
        )

    t_dict = _clean_ticket_doc(doc)
    target = payload.target.strip().upper()
    now_iso = datetime.now(timezone.utc).isoformat()
    actor_name = admin_user.display_name or admin_user.username or "Admin Arbiter"

    user_mail_ok = None
    user_mail_err = None
    admin_mail_ok = None
    admin_mail_err = None
    log_entries = []

    # 1. User Email retry
    if target in ("USER", "ALL"):
        try:
            user_mail_ok, user_mail_err = await send_ticket_confirmation_email(t_dict)
        except Exception as e:
            user_mail_ok = False
            user_mail_err = str(e)

        log_user = SupportLogEntry(
            ticket_id=t_dict.get("ticket_id", ticket_id),
            action="EMAIL_RETRY_USER",
            timestamp=now_iso,
            actor=actor_name,
            actor_role="ADMIN",
            details="User confirmation email resent successfully." if user_mail_ok else f"User email retry failed: {user_mail_err}",
        )
        log_entries.append(log_user.to_mongo())

    # 2. Support / Admin Notification retry
    if target in ("SUPPORT", "ALL"):
        configured_email = await _get_configured_support_email(db)
        try:
            admin_mail_ok, admin_mail_err = await send_admin_ticket_notification_email(
                t_dict,
                admin_recipient=configured_email,
            )
        except Exception as e:
            admin_mail_ok = False
            admin_mail_err = str(e)

        log_admin = SupportLogEntry(
            ticket_id=t_dict.get("ticket_id", ticket_id),
            action="EMAIL_RETRY_SUPPORT",
            timestamp=now_iso,
            actor=actor_name,
            actor_role="ADMIN",
            details=f"Admin alert email resent to {configured_email}." if admin_mail_ok else f"Admin email retry failed: {admin_mail_err}",
        )
        log_entries.append(log_admin.to_mongo())

    # Calculate overall status
    current_user_status = "SENT" if user_mail_ok else ("FAILED" if user_mail_ok is False else doc.get("user_email_status", "PENDING"))
    current_admin_status = "SENT" if admin_mail_ok else ("FAILED" if admin_mail_ok is False else doc.get("support_email_status", "PENDING"))

    if current_user_status == "SENT" and current_admin_status == "SENT":
        overall_status = "SENT"
    elif current_user_status == "SENT" or current_admin_status == "SENT":
        overall_status = "PARTIAL"
    else:
        overall_status = "FAILED"

    errs = []
    if user_mail_err:
        errs.append(f"User email: {user_mail_err}")
    if admin_mail_err:
        errs.append(f"Admin email: {admin_mail_err}")
    err_summary = "; ".join(errs) if errs else doc.get("email_error")

    update_fields = {
        "email_status": overall_status,
        "email_error": err_summary,
        "user_email_status": current_user_status,
        "support_email_status": current_admin_status,
        "updated_at": now_iso,
    }

    update_op = {"$set": update_fields}
    if log_entries:
        update_op["$push"] = {"logs": {"$each": log_entries}}
        for le in log_entries:
            await db.support_logs.insert_one(le)

    await db.support_tickets.update_one({"_id": doc["_id"]}, update_op)

    updated_doc = await db.support_tickets.find_one({"_id": doc["_id"]})
    return SupportTicketResponse(**_clean_ticket_doc(updated_doc))



@router.get("/my-tickets", response_model=List[SupportTicketResponse])
async def get_my_support_tickets(
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Retrieves tickets created by the authenticated user."""
    query = {
        "$or": [
            {"user_id": current_user.id},
            {"email": current_user.email.lower() if current_user.email else None},
        ]
    }
    cursor = db.support_tickets.find(query).sort("created_at", -1)
    docs = await cursor.to_list(length=50)
    return [SupportTicketResponse(**_clean_ticket_doc(d)) for d in docs]

