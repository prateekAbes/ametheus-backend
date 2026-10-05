import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_db, require_organizer, require_super_admin
from app.models.user import User
from app.services.notification import create_notification
from app.services.email_service import send_email
from app.services.audit import record_audit_log

organizer_comm_router = APIRouter()
admin_comm_router = APIRouter()

# ----------------- SCHEMAS -----------------

class MatchRequestCreate(BaseModel):
    tournament_id: str
    match_id: Optional[str] = None
    title: Optional[str] = None
    team_a_name: Optional[str] = None
    team_b_name: Optional[str] = None
    round_name: Optional[str] = None
    scheduled_at: Optional[str] = None
    youtube_live_url: Optional[str] = None
    notes: Optional[str] = None
    urgency: Optional[str] = "NORMAL"  # NORMAL, HIGH, URGENT

class MatchRequestAction(BaseModel):
    status: str  # APPROVED, REJECTED, IN_REVIEW
    admin_notes: Optional[str] = None
    make_match_live: Optional[bool] = True

class OrganizerEmailCreate(BaseModel):
    subject: str
    message: str
    urgency: Optional[str] = "NORMAL"  # NORMAL, HIGH, URGENT
    category: Optional[str] = "GENERAL"  # GENERAL, TOURNAMENT, MATCH_DISPUTE, PAYOUT, TECHNICAL

class OrganizerEmailReply(BaseModel):
    message: str
    status: Optional[str] = None

class OrganizerEmailStatusUpdate(BaseModel):
    status: str  # NEW, OPEN, WAITING_ORGANIZER, WAITING_ADMIN, RESOLVED, CLOSED


# ----------------- ORGANIZER MATCH REQUESTS -----------------

@organizer_comm_router.post("/match-requests", status_code=status.HTTP_201_CREATED)
async def create_organizer_match_request(
    payload: MatchRequestCreate,
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Organizer requests a live match / broadcast slot for an approved tournament.
    """
    tourn = await db.tournaments.find_one({
        "$or": [{"_id": payload.tournament_id}, {"id": payload.tournament_id}, {"slug": payload.tournament_id}]
    })
    tourn_title = tourn.get("title", "Tournament") if tourn else "Tournament"

    now_utc = datetime.now(timezone.utc)
    req_id = f"mrq_{uuid.uuid4().hex[:12]}"

    doc = {
        "_id": req_id,
        "id": req_id,
        "organizer_id": organizer.id,
        "organizer_name": organizer.display_name or organizer.username,
        "organizer_email": organizer.email,
        "tournament_id": payload.tournament_id,
        "tournament_title": tourn_title,
        "match_id": payload.match_id,
        "title": payload.title or f"{payload.team_a_name or 'Team A'} vs {payload.team_b_name or 'Team B'}",
        "team_a_name": payload.team_a_name,
        "team_b_name": payload.team_b_name,
        "round_name": payload.round_name,
        "scheduled_at": payload.scheduled_at,
        "youtube_live_url": payload.youtube_live_url,
        "notes": payload.notes,
        "urgency": (payload.urgency or "NORMAL").upper(),
        "status": "NEW",  # NEW, UNDER_REVIEW, APPROVED, REJECTED, CANCELLED, RESOLVED
        "admin_notes": None,
        "created_at": now_utc.isoformat(),
        "updated_at": now_utc.isoformat(),
    }
    await db.organizer_match_requests.insert_one(doc)

    await record_audit_log(
        db=db,
        operator=organizer.username,
        role=organizer.role,
        action="REQUEST_LIVE_MATCH",
        target=f"Match Request {req_id} for {tourn_title}",
        details=f"Live stream request submitted by organizer.",
    )

    doc["id"] = str(doc.pop("_id", doc.get("id")))
    return doc


@organizer_comm_router.get("/match-requests")
async def list_organizer_match_requests(
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = {"organizer_id": organizer.id} if organizer.role != "SUPER_ADMIN" else {}
    cursor = db.organizer_match_requests.find(query).sort("created_at", -1)
    docs = await cursor.to_list(100)
    for d in docs:
        d["id"] = str(d.pop("_id", d.get("id")))
    return docs


@admin_comm_router.get("/match-requests")
async def list_admin_organizer_match_requests(
    status_filter: Optional[str] = Query(None),
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = {}
    if status_filter and status_filter.upper() != "ALL":
        sf = status_filter.upper()
        if sf == "NEW":
            query["status"] = {"$in": ["NEW", "PENDING"]}
        elif sf in ("UNDER_REVIEW", "IN_REVIEW"):
            query["status"] = {"$in": ["UNDER_REVIEW", "IN_REVIEW"]}
        else:
            query["status"] = sf
    cursor = db.organizer_match_requests.find(query).sort("created_at", -1)
    docs = await cursor.to_list(100)
    for d in docs:
        d["id"] = str(d.pop("_id", d.get("id")))
    return docs


@admin_comm_router.patch("/match-requests/{request_id}")
async def handle_organizer_match_request_action(
    request_id: str,
    payload: MatchRequestAction,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    req_doc = await db.organizer_match_requests.find_one({"$or": [{"_id": request_id}, {"id": request_id}]})
    if not req_doc:
        raise HTTPException(status_code=404, detail="Match request not found")

    new_st = payload.status.upper()
    now_iso = datetime.now(timezone.utc).isoformat()

    update_fields: Dict[str, Any] = {
        "status": new_st,
        "admin_notes": payload.admin_notes,
        "reviewed_by": admin_user.username,
        "updated_at": now_iso,
    }

    # If approved and make_match_live is true, mark match LIVE if match_id exists
    match_id = req_doc.get("match_id")
    if new_st == "APPROVED" and payload.make_match_live and match_id:
        match_doc = await db.matches.find_one({"$or": [{"_id": match_id}, {"id": match_id}]})
        if match_doc:
            m_update = {"status": "LIVE"}
            if req_doc.get("youtube_live_url"):
                m_update["youtube_live_url"] = req_doc.get("youtube_live_url")
            await db.matches.update_one({"_id": match_doc["_id"]}, {"$set": m_update})

    await db.organizer_match_requests.update_one({"_id": req_doc["_id"]}, {"$set": update_fields})

    # Notify organizer
    org_id = req_doc.get("organizer_id")
    if org_id:
        await create_notification(
            db=db,
            user_id=org_id,
            title=f"Live Match Request {new_st}",
            message=f"Your request for '{req_doc.get('title')}' in {req_doc.get('tournament_title')} has been {new_st.lower()}. Notes: {payload.admin_notes or 'No admin notes provided.'}",
            notification_type="TOURNAMENT",
            link="/organizer",
        )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="MODERATE_MATCH_REQUEST",
        target=f"Match Request {request_id}",
        details=f"Status set to {new_st}. Notes: {payload.admin_notes}",
    )

    updated = await db.organizer_match_requests.find_one({"_id": req_doc["_id"]})
    updated["id"] = str(updated.pop("_id", updated.get("id")))
    return updated


# ----------------- ORGANIZER PERSONAL EMAILS ("EMAIL ADMIN") -----------------

@organizer_comm_router.post("/emails", status_code=status.HTTP_201_CREATED)
async def create_organizer_email_thread(
    payload: OrganizerEmailCreate,
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Organizer creates a direct personal communication thread to Platform Administration.
    Distinct from standard public support tickets.
    """
    now_utc = datetime.now(timezone.utc)
    thread_id = f"oem_{uuid.uuid4().hex[:12]}"
    sender_name = organizer.display_name or organizer.username

    initial_msg = {
        "id": f"msg_{uuid.uuid4().hex[:8]}",
        "sender_id": organizer.id,
        "sender_name": sender_name,
        "sender_role": "ORGANIZER",
        "message": payload.message.strip(),
        "created_at": now_utc.isoformat(),
    }

    doc = {
        "_id": thread_id,
        "id": thread_id,
        "organizer_id": organizer.id,
        "organizer_name": sender_name,
        "organizer_email": organizer.email,
        "subject": payload.subject.strip(),
        "category": (payload.category or "GENERAL").upper(),
        "urgency": (payload.urgency or "NORMAL").upper(),
        "status": "NEW",  # NEW, OPEN, WAITING_ORGANIZER, WAITING_ADMIN, RESOLVED, CLOSED
        "messages": [initial_msg],
        "created_at": now_utc.isoformat(),
        "updated_at": now_utc.isoformat(),
    }
    await db.organizer_admin_threads.insert_one(doc)

    await record_audit_log(
        db=db,
        operator=organizer.username,
        role=organizer.role,
        action="ORGANIZER_EMAIL_ADMIN",
        target=f"Email Thread {thread_id}: {payload.subject[:40]}",
        details="Organizer initiated direct communication with platform admin.",
    )

    doc["id"] = str(doc.pop("_id", doc.get("id")))
    return doc


@organizer_comm_router.get("/emails")
async def list_organizer_email_threads(
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = {"organizer_id": organizer.id} if organizer.role != "SUPER_ADMIN" else {}
    cursor = db.organizer_admin_threads.find(query).sort("updated_at", -1)
    docs = await cursor.to_list(100)
    for d in docs:
        d["id"] = str(d.pop("_id", d.get("id")))
    return docs


@organizer_comm_router.get("/emails/{thread_id}")
async def get_organizer_email_thread(
    thread_id: str,
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    doc = await db.organizer_admin_threads.find_one({"$or": [{"_id": thread_id}, {"id": thread_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Email thread not found")
    if organizer.role != "SUPER_ADMIN" and doc.get("organizer_id") != organizer.id:
        raise HTTPException(status_code=403, detail="Forbidden")

    doc["id"] = str(doc.pop("_id", doc.get("id")))
    return doc


@organizer_comm_router.post("/emails/{thread_id}/reply")
async def organizer_reply_email_thread(
    thread_id: str,
    payload: OrganizerEmailReply,
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    doc = await db.organizer_admin_threads.find_one({"$or": [{"_id": thread_id}, {"id": thread_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Email thread not found")
    if organizer.role != "SUPER_ADMIN" and doc.get("organizer_id") != organizer.id:
        raise HTTPException(status_code=403, detail="Forbidden")

    now_iso = datetime.now(timezone.utc).isoformat()
    msg = {
        "id": f"msg_{uuid.uuid4().hex[:8]}",
        "sender_id": organizer.id,
        "sender_name": organizer.display_name or organizer.username,
        "sender_role": "ORGANIZER",
        "message": payload.message.strip(),
        "created_at": now_iso,
    }

    await db.organizer_admin_threads.update_one(
        {"_id": doc["_id"]},
        {
            "$set": {"status": "WAITING_ADMIN", "updated_at": now_iso},
            "$push": {"messages": msg},
        },
    )

    updated = await db.organizer_admin_threads.find_one({"_id": doc["_id"]})
    updated["id"] = str(updated.pop("_id", updated.get("id")))
    return updated


# --- Admin Side Organizer Emails ---

@admin_comm_router.get("/organizer-emails")
async def list_admin_organizer_emails(
    status_filter: Optional[str] = Query(None),
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = {}
    if status_filter and status_filter.upper() != "ALL":
        query["status"] = status_filter.upper()
    cursor = db.organizer_admin_threads.find(query).sort("updated_at", -1)
    docs = await cursor.to_list(100)
    for d in docs:
        d["id"] = str(d.pop("_id", d.get("id")))
    return docs


@admin_comm_router.get("/organizer-emails/{thread_id}")
async def get_admin_organizer_email_thread(
    thread_id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    doc = await db.organizer_admin_threads.find_one({"$or": [{"_id": thread_id}, {"id": thread_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Email thread not found")
    doc["id"] = str(doc.pop("_id", doc.get("id")))
    return doc


@admin_comm_router.post("/organizer-emails/{thread_id}/reply")
async def admin_reply_organizer_email_thread(
    thread_id: str,
    payload: OrganizerEmailReply,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Super Admin responds to Organizer email thread.
    - Appends to thread
    - Dispatches Brevo SMTP email to organizer's address
    - Dispatches in-app notification to organizer
    - Updates thread status to WAITING_ORGANIZER (or specified status)
    """
    doc = await db.organizer_admin_threads.find_one({"$or": [{"_id": thread_id}, {"id": thread_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Email thread not found")

    now_iso = datetime.now(timezone.utc).isoformat()
    actor_name = admin_user.display_name or admin_user.username or "Platform Executive Admin"

    new_st = (payload.status or "WAITING_ORGANIZER").upper()

    reply_msg = {
        "id": f"msg_{uuid.uuid4().hex[:8]}",
        "sender_id": admin_user.id,
        "sender_name": actor_name,
        "sender_role": "SUPER_ADMIN",
        "message": payload.message.strip(),
        "created_at": now_iso,
    }

    await db.organizer_admin_threads.update_one(
        {"_id": doc["_id"]},
        {
            "$set": {"status": new_st, "updated_at": now_iso},
            "$push": {"messages": reply_msg},
        },
    )

    # 1. Send in-app notification
    org_id = doc.get("organizer_id")
    if org_id:
        await create_notification(
            db=db,
            user_id=org_id,
            title=f"Admin Message: {doc.get('subject')[:35]}",
            message=f"Admin reply: {payload.message[:120]}",
            notification_type="SYSTEM",
            link="/organizer",
        )

    # 2. Dispatch Brevo SMTP email
    org_email = doc.get("organizer_email")
    if org_email:
        email_html = f"""
        <div style="font-family: sans-serif; background-color: #05060a; color: #f4f6fa; padding: 24px;">
            <div style="max-width: 600px; margin: 0 auto; background-color: #0b0d17; border: 1px solid #222; border-radius: 8px; padding: 24px;">
                <h2 style="color: #8b5cff; margin-top: 0;">Ametheus Administration — Organizer Communication</h2>
                <p>Hello <strong>{doc.get('organizer_name', 'Organizer')}</strong>,</p>
                <p>Platform leadership has replied to your communication regarding: <strong>{doc.get('subject')}</strong></p>
                <div style="background-color: #121526; border-left: 4px solid #8b5cff; padding: 16px; margin: 20px 0; border-radius: 4px;">
                    <p style="margin: 0; white-space: pre-wrap; font-size: 14px;">{payload.message}</p>
                </div>
                <p style="font-size: 12px; color: #888;">Status: <strong style="color: #8b5cff;">{new_st}</strong></p>
                <p style="font-size: 12px; color: #888;">Visit your Organizer Panel to continue this dialogue.</p>
            </div>
        </div>
        """
        try:
            await send_email(
                sender_type="official",
                recipient=org_email,
                subject=f"Re: [Organizer Inquiry] {doc.get('subject')} — Ametheus Admin",
                text_content=payload.message,
                html_content=email_html,
            )
        except Exception:
            pass

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="REPLY_ORGANIZER_EMAIL",
        target=f"Thread {thread_id} ({doc.get('subject')})",
        details=f"Admin replied to organizer. Status: {new_st}",
    )

    updated = await db.organizer_admin_threads.find_one({"_id": doc["_id"]})
    updated["id"] = str(updated.pop("_id", updated.get("id")))
    return updated


@admin_comm_router.patch("/organizer-emails/{thread_id}/status")
async def update_admin_organizer_email_status(
    thread_id: str,
    payload: OrganizerEmailStatusUpdate,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    doc = await db.organizer_admin_threads.find_one({"$or": [{"_id": thread_id}, {"id": thread_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Email thread not found")

    new_st = payload.status.upper()
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.organizer_admin_threads.update_one(
        {"_id": doc["_id"]},
        {"$set": {"status": new_st, "updated_at": now_iso}}
    )

    updated = await db.organizer_admin_threads.find_one({"_id": doc["_id"]})
    updated["id"] = str(updated.pop("_id", updated.get("id")))
    return updated
