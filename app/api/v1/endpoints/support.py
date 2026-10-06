import asyncio
import re
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, get_current_user, get_current_user_optional, require_organizer
from app.models.user import User
from app.models.support import SupportTicket, SupportLogEntry, generate_ticket_id
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

    # Strict normalization to the 5 standard support ticket statuses
    raw_status = str(item.get("status", "OPEN")).strip().upper().replace(" ", "_")
    if raw_status in ("NEW", "UNREAD"):
        raw_status = "OPEN"
    elif raw_status in ("WAITING_FOR_ADMIN", "ON_HOLD"):
        raw_status = "IN_PROGRESS"
    elif raw_status not in ("OPEN", "IN_PROGRESS", "WAITING_FOR_USER", "RESOLVED", "CLOSED", "ARCHIVED"):
        # Prevent tournament/game statuses like REGISTRATION_OPEN from ever appearing
        raw_status = "OPEN"
    item["status"] = raw_status

    raw_msgs = item.get("messages", [])
    if not raw_msgs and item.get("description"):
        raw_msgs = [
            {
                "id": "msg_initial",
                "sender_id": item.get("user_id") or "user",
                "sender_name": item.get("name", "Contender"),
                "sender_role": "USER",
                "message": item.get("description", ""),
                "created_at": str(item.get("created_at") or datetime.now(timezone.utc).isoformat()),
            }
        ]
    item["messages"] = raw_msgs
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
        status="OPEN",
        email_status="PENDING",
        logs=[init_log],
        created_at=now_iso,
        updated_at=now_iso,
    )

    doc = ticket.to_mongo()
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
                link="/support",
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
    admin_mail_ok = False
    admin_mail_err = None

    # Dispatch emails concurrently with defensive error isolation
    async def _send_user_mail():
        return await send_ticket_confirmation_email(ticket_dict)

    async def _send_admin_mail():
        return await send_admin_ticket_notification_email(
            ticket_dict,
            admin_recipient=configured_support_email,
        )

    try:
        results = await asyncio.wait_for(
            asyncio.gather(_send_user_mail(), _send_admin_mail(), return_exceptions=True),
            timeout=2.0,
        )
        if isinstance(results[0], tuple):
            user_mail_ok, user_mail_err = results[0]
        elif isinstance(results[0], Exception):
            user_mail_err = str(results[0])

        if isinstance(results[1], tuple):
            admin_mail_ok, admin_mail_err = results[1]
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
                "$push": {"logs": email_log.to_mongo()},
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


@router.post("/tickets/{ticket_id}/archive")
async def archive_single_support_ticket(
    ticket_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    ok = await archive_single_record(db, "support_tickets", ticket_id, admin_user)
    return {"status": "success", "message": "Support ticket archived", "ticket_id": ticket_id}


@router.post("/tickets/{ticket_id}/unarchive")
async def unarchive_single_support_ticket(
    ticket_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    ok = await unarchive_record(db, "support_tickets", ticket_id, admin_user)
    return {"status": "success", "message": "Support ticket restored", "ticket_id": ticket_id}


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

    # Retrieve all standalone logs associated with this ticket
    log_cursor = db.support_logs.find({"ticket_id": doc.get("ticket_id", ticket_id)}).sort("timestamp", 1)
    db_logs = await log_cursor.to_list(length=100)

    cleaned = _clean_ticket_doc(doc)
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
        elif st_upper in ("WAITING_FOR_ADMIN", "ON_HOLD"):
            st_upper = "IN_PROGRESS"
        if st_upper not in VALID_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid status '{payload.status}'. Allowed: ['OPEN', 'IN_PROGRESS', 'WAITING_FOR_USER', 'RESOLVED', 'CLOSED']",
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
    - Appends to messages array
    - Dispatches Brevo email to user's email address
    - Dispatches in-app notification to user
    - Updates ticket status (e.g. WAITING_FOR_USER)
    - Records audit and support logs
    """
    import uuid
    from app.services.notification import create_notification
    from app.services.email_service import send_support_email

    doc = await db.support_tickets.find_one({"$or": [{"_id": ticket_id}, {"ticket_id": ticket_id}, {"id": ticket_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Support ticket not found")

    clean_tid = doc.get("ticket_id", ticket_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    actor_name = admin_user.display_name or admin_user.username or "Ametheus Arbiter"

    reply_msg = {
        "id": f"msg_{uuid.uuid4().hex[:10]}",
        "sender_id": admin_user.id,
        "sender_name": actor_name,
        "sender_role": "ADMIN",
        "message": payload.message.strip(),
        "created_at": now_iso,
    }

    new_status = (payload.status or "WAITING_FOR_USER").strip().upper()
    if new_status not in VALID_STATUSES:
        new_status = "WAITING_FOR_USER"

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
        "updated_at": now_iso,
    }
    if new_status in ("RESOLVED", "CLOSED"):
        update_fields["resolved_at"] = now_iso

    await db.support_tickets.update_one(
        {"_id": doc["_id"]},
        {
            "$set": update_fields,
            "$push": {
                "messages": reply_msg,
                "logs": reply_log.to_mongo(),
            },
        },
    )

    user_id = doc.get("user_id")
    if user_id and not str(user_id).startswith("guest"):
        await create_notification(
            db=db,
            user_id=str(user_id),
            title=f"Support Ticket Update [{clean_tid}]",
            message=f"Admin response: {payload.message[:120]}",
            notification_type="SUPPORT",
            link="/support",
        )

    user_email = doc.get("email")
    if user_email:
        email_html = f"""
        <div style="font-family: sans-serif; background-color: #05060a; color: #f4f6fa; padding: 24px;">
            <div style="max-width: 600px; margin: 0 auto; background-color: #0b0d17; border: 1px solid #222; border-radius: 8px; padding: 24px;">
                <h2 style="color: #3ef2ff; margin-top: 0;">Ametheus Support — Ticket Update</h2>
                <p>Hello <strong>{doc.get('name', 'Contender')}</strong>,</p>
                <p>An official support arbiter has responded to your ticket <strong>[{clean_tid}]</strong> ({doc.get('category')}):</p>
                <div style="background-color: #121526; border-left: 4px solid #3ef2ff; padding: 16px; margin: 20px 0; border-radius: 4px;">
                    <p style="margin: 0; white-space: pre-wrap; font-size: 14px;">{payload.message}</p>
                </div>
                <p style="font-size: 12px; color: #888;">Status: <strong style="color: #3ef2ff;">{new_status}</strong></p>
                <p style="font-size: 12px; color: #888;">You can reply to this email or visit the platform support desk to follow up.</p>
            </div>
        </div>
        """
        try:
            await send_support_email(
                to_email=user_email,
                subject=f"Re: [Ticket {clean_tid}] {doc.get('category')} — Ametheus Support",
                text_content=payload.message,
                html_content=email_html,
            )
        except Exception:
            pass

    refreshed = await db.support_tickets.find_one({"_id": doc["_id"]})
    return SupportTicketResponse(**_clean_ticket_doc(refreshed))


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


@router.post("/tickets/{ticket_id}/archive", response_model=SupportTicketResponse)
async def toggle_archive_support_ticket(
    ticket_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    doc = await db.support_tickets.find_one({"$or": [{"_id": ticket_id}, {"ticket_id": ticket_id}, {"id": ticket_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Ticket not found")
    new_arch = not doc.get("is_archived", False)
    now_iso = datetime.now(timezone.utc).isoformat()
    update_data = {"is_archived": new_arch, "updated_at": now_iso}
    if new_arch:
        update_data["status"] = "ARCHIVED"
    elif doc.get("status") == "ARCHIVED":
        update_data["status"] = "OPEN"

    await db.support_tickets.update_one(
        {"_id": doc["_id"]},
        {"$set": update_data}
    )
    updated = await db.support_tickets.find_one({"_id": doc["_id"]})
    return SupportTicketResponse(**_clean_ticket_doc(updated))
