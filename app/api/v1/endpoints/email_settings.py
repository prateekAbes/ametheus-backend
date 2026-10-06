import logging
from typing import Optional, Dict, Any
from pydantic import BaseModel
from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_db, require_super_admin
from app.models.user import User
from app.schemas.email_settings import (
    EmailSettingsResponse,
    EmailSystemStatusResponse,
    SmtpStatusResponse,
    SendTestEmailRequest,
    SendTestEmailResponse,
    OfficialEmailSendRequest,
    OfficialEmailSendResponse,
    OfficialEmailHistoryResponse,
    OfficialEmailLogItem,
)
from app.services.email_service import (
    get_email_system_status,
    check_smtp_health,
    send_test_email,
)
from app.core.config import settings

logger = logging.getLogger("ametheus.api.email_settings")

# Dedicated router for public/platform configuration lookups
public_router = APIRouter()

# Admin-only router for Super Admin status & diagnostics (Strictly Read-Only + Test Dispatch)
admin_router = APIRouter()


@public_router.get("/email-settings", response_model=EmailSettingsResponse)
async def get_public_email_settings():
    """
    Public endpoint for frontend clients to dynamically retrieve official,
    support, and automated email identities configured in the server environment.
    Never exposes credentials or backend infrastructure details.
    """
    return EmailSettingsResponse(
        official_email=settings.OFFICIAL_EMAIL,
        support_email=settings.SUPPORT_EMAIL,
        automated_email=settings.NOREPLY_EMAIL,
        updated_at=None,
        updated_by=None,
    )


@public_router.get("/stats")
async def get_public_platform_stats(db: AsyncIOMotorDatabase = Depends(get_db)):
    """
    Public platform statistics for About page and public platform metrics.
    """
    total_users = await db.users.count_documents({})
    total_tournaments = await db.tournaments.count_documents({})
    total_games = await db.games.count_documents({"status": "active"})
    if total_games == 0:
        total_games = await db.games.count_documents({})
    total_matches = await db.matches.count_documents({})
    total_teams = await db.teams.count_documents({})

    pipeline = [
        {"$match": {"status": {"$in": ["PAID", "COMPLETED", "APPROVED"]}, "is_dummy": {"$ne": True}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount_paise"}}},
    ]
    agg1 = await db.payouts.aggregate(pipeline).to_list(1)
    agg2 = await db.game_payouts.aggregate(pipeline).to_list(1)
    payout_paise = (agg1[0]["total"] if agg1 else 0) + (agg2[0]["total"] if agg2 else 0)
    payout_inr = payout_paise // 100

    return {
        "total_users": total_users,
        "total_tournaments": total_tournaments,
        "total_games": total_games,
        "total_matches": total_matches,
        "total_teams": total_teams,
        "total_payout_inr": payout_inr,
    }


@admin_router.get("/status", response_model=EmailSystemStatusResponse)
async def get_admin_email_system_status(
    admin_user: User = Depends(require_super_admin),
):
    """
    Super Admin endpoint to inspect the read-only configuration status of all three
    Brevo SMTP identities (official, support, noreply).
    Credentials, passwords, and raw tokens are NEVER returned.
    """
    return get_email_system_status()


@admin_router.get("/settings", response_model=EmailSettingsResponse)
async def get_admin_email_settings(
    admin_user: User = Depends(require_super_admin),
):
    """
    Super Admin read-only endpoint returning currently active email identities.
    (PUT endpoint has been completely removed to prevent unauthorized mutations).
    """
    return EmailSettingsResponse(
        official_email=settings.OFFICIAL_EMAIL,
        support_email=settings.SUPPORT_EMAIL,
        automated_email=settings.NOREPLY_EMAIL,
        updated_at=None,
        updated_by="Server Environment",
    )


@admin_router.get("/smtp-status", response_model=SmtpStatusResponse)
async def get_smtp_health_status(
    service: str = Query("support", description="Service identity to test: 'support', 'noreply', or 'official'"),
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Live diagnostic health check of the backend Brevo SMTP connection for a specific identity.
    Verifies TCP connectivity and Brevo authentication without exposing credentials.
    """
    return await check_smtp_health(service, db)


@admin_router.post("/send-test", response_model=SendTestEmailResponse)
async def send_diagnostic_test_email(
    payload: SendTestEmailRequest,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Sends a live diagnostic test message using the specified service identity ('support', 'noreply', or 'official')
    to the designated destination address via Brevo SMTP.
    """
    return await send_test_email(
        to_email=str(payload.to_email),
        service=str(payload.service),
        db=db,
    )


async def _dispatch_official_broadcast(
    db: AsyncIOMotorDatabase,
    log_id: str,
    target_recipients: list[dict],  # list of {"email": str, "name": str}
    subject: str,
    message: str,
):
    """
    Asynchronously broadcasts official email to recipients respecting rate limits
    and updates the database delivery log.
    """
    from app.services.email_service import send_official_announcement_to_recipient
    import asyncio
    from datetime import datetime, timezone

    sent_count = 0
    fail_count = 0
    errors = []

    for r in target_recipients:
        email = r["email"]
        name = r.get("name") or "Ametheus Member"
        try:
            ok, err = await send_official_announcement_to_recipient(
                to_email=email,
                subject=subject,
                message=message,
                recipient_name=name,
            )
            if ok:
                sent_count += 1
            else:
                fail_count += 1
                if err and len(errors) < 5:
                    errors.append(f"{email}: {err}")
        except Exception as e:
            fail_count += 1
            if len(errors) < 5:
                errors.append(f"{email}: {str(e)}")

        # Respect Brevo rate limits: brief pause between sends in batch
        if len(target_recipients) > 1:
            await asyncio.sleep(0.1)

    status = "COMPLETED" if fail_count == 0 else ("PARTIAL" if sent_count > 0 else "FAILED")
    error_summary = "; ".join(errors) if errors else None

    await db.official_email_logs.update_one(
        {"_id": log_id},
        {
            "$set": {
                "status": status,
                "sent_count": sent_count,
                "fail_count": fail_count,
                "error_summary": error_summary,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        },
    )


@admin_router.post("/official/send", response_model=OfficialEmailSendResponse)
async def send_official_admin_email(
    payload: OfficialEmailSendRequest,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Super Admin endpoint to broadcast official platform communication via official@ametheus.games.
    Audience modes:
    - ALL_USERS: broadcasts to all active registered platform users.
    - SELECTED_USERS: broadcasts to selected user IDs.
    - SELECTED_TEAMS: broadcasts to all members belonging to selected team IDs.
    - CUSTOM_EMAILS: broadcasts to explicit email addresses.
    """
    import asyncio
    import uuid
    from datetime import datetime, timezone
    from app.schemas.email_settings import EMAIL_REGEX

    target_recipients: list[dict] = []
    seen_emails: set[str] = set()

    def add_recipient(email: Optional[str], name: Optional[str] = None):
        if not email:
            return
        clean = email.strip().lower()
        if clean and EMAIL_REGEX.match(clean) and clean not in seen_emails:
            seen_emails.add(clean)
            target_recipients.append({"email": clean, "name": name or "Contender"})

    rec_type = payload.recipient_type.upper()

    if rec_type == "ALL_USERS":
        cursor = db.users.find({"email": {"$exists": True, "$ne": None}}, {"email": 1, "display_name": 1, "username": 1})
        async for u in cursor:
            add_recipient(u.get("email"), u.get("display_name") or u.get("username"))

    elif rec_type in ("ALL_ORGANIZERS", "ORGANIZERS"):
        cursor = db.users.find(
            {
                "$or": [
                    {"role": {"$in": ["ORGANIZER", "organizer", "TOURNAMENT_ADMIN"]}},
                    {"is_organizer": True},
                ],
                "email": {"$exists": True, "$ne": None},
            },
            {"email": 1, "display_name": 1, "username": 1}
        )
        async for u in cursor:
            add_recipient(u.get("email"), u.get("display_name") or u.get("username"))

    elif rec_type == "SELECTED_USERS":
        if not payload.user_ids:
            raise HTTPException(status_code=400, detail="user_ids cannot be empty when recipient_type is 'SELECTED_USERS'")
        cursor = db.users.find(
            {"$or": [{"_id": {"$in": payload.user_ids}}, {"id": {"$in": payload.user_ids}}]},
            {"email": 1, "display_name": 1, "username": 1}
        )
        async for u in cursor:
            add_recipient(u.get("email"), u.get("display_name") or u.get("username"))

    elif rec_type == "SELECTED_TEAMS":
        if not payload.team_ids:
            raise HTTPException(status_code=400, detail="team_ids cannot be empty when recipient_type is 'SELECTED_TEAMS'")
        cursor = db.teams.find(
            {"$or": [{"_id": {"$in": payload.team_ids}}, {"id": {"$in": payload.team_ids}}]}
        )
        team_user_ids = set()
        async for team in cursor:
            if team.get("captain_id"):
                team_user_ids.add(team["captain_id"])
            for m in team.get("members", []):
                uid = m.get("user_id") if isinstance(m, dict) else m
                if uid:
                    team_user_ids.add(uid)

        if team_user_ids:
            u_cursor = db.users.find(
                {"$or": [{"_id": {"$in": list(team_user_ids)}}, {"id": {"$in": list(team_user_ids)}}]},
                {"email": 1, "display_name": 1, "username": 1}
            )
            async for u in u_cursor:
                add_recipient(u.get("email"), u.get("display_name") or u.get("username"))

    elif rec_type == "CUSTOM_EMAILS":
        if not payload.custom_emails:
            raise HTTPException(status_code=400, detail="custom_emails cannot be empty when recipient_type is 'CUSTOM_EMAILS'")
        for ce in payload.custom_emails:
            add_recipient(ce)

    if not target_recipients:
        raise HTTPException(
            status_code=400,
            detail="No valid recipient email addresses found for the selected criteria."
        )

    log_id = f"OFFICIAL-{uuid.uuid4().hex[:12].upper()}"
    now_iso = datetime.now(timezone.utc).isoformat()
    sample_recipients = [r["email"] for r in target_recipients[:10]]

    log_doc = {
        "_id": log_id,
        "id": log_id,
        "sender": settings.OFFICIAL_EMAIL,
        "recipient_type": rec_type,
        "recipient_count": len(target_recipients),
        "recipients_sample": sample_recipients,
        "subject": payload.subject.strip(),
        "message": payload.message.strip(),
        "status": "QUEUED",
        "created_at": now_iso,
        "operator_id": admin_user.id,
        "sent_count": 0,
        "fail_count": 0,
        "error_summary": None,
    }
    await db.official_email_logs.insert_one(log_doc)

    # Launch asynchronous broadcast in background
    asyncio.create_task(
        _dispatch_official_broadcast(
            db=db,
            log_id=log_id,
            target_recipients=target_recipients,
            subject=payload.subject.strip(),
            message=payload.message.strip(),
        )
    )

    return OfficialEmailSendResponse(
        success=True,
        message=f"Official broadcast scheduled for {len(target_recipients)} recipient(s).",
        recipient_count=len(target_recipients),
        log_id=log_id,
    )


@admin_router.get("/official/history", response_model=OfficialEmailHistoryResponse)
async def get_official_email_history(
    limit: int = Query(50, ge=1, le=100),
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns history of official email broadcasts dispatched by Super Admins.
    """
    total = await db.official_email_logs.count_documents({})
    cursor = db.official_email_logs.find({}).sort("created_at", -1).limit(limit)

    items = []
    async for doc in cursor:
        items.append(
            OfficialEmailLogItem(
                id=str(doc.get("id") or doc.get("_id")),
                sender=doc.get("sender") or settings.OFFICIAL_EMAIL,
                recipient_type=doc.get("recipient_type") or "UNKNOWN",
                recipient_count=int(doc.get("recipient_count") or 0),
                recipients_sample=doc.get("recipients_sample") or [],
                subject=doc.get("subject") or "",
                status=doc.get("status") or "UNKNOWN",
                created_at=doc.get("created_at") or "",
                operator_id=doc.get("operator_id"),
                error_summary=doc.get("error_summary"),
            )
        )

    return OfficialEmailHistoryResponse(total=total, items=items)


class PersonalEmailSendRequest(BaseModel):
    recipient_email: str
    recipient_name: Optional[str] = "Contender"
    sender_identity: Optional[str] = "official"
    subject: str
    message: str


@admin_router.post("/send-personal")
async def send_personal_admin_email(
    payload: PersonalEmailSendRequest,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Sends a direct personal email from an admin to a specific user, organizer, or custom email address.
    Returns real-time Brevo delivery status, error code, and error details.
    """
    from app.services.email_service import send_personal_email_with_details
    from app.services.audit import record_audit_log

    email_clean = payload.recipient_email.strip().lower()
    snd_ident = payload.sender_identity.strip().lower() if payload.sender_identity else "official"
    if snd_ident not in ("official", "support", "noreply"):
        snd_ident = "official"

    html_content = f"""
    <div style="font-family: sans-serif; background-color: #05060a; color: #f4f6fa; padding: 24px;">
        <div style="max-width: 600px; margin: 0 auto; background-color: #0b0d17; border: 1px solid #222; border-radius: 8px; padding: 24px;">
            <h2 style="color: #3ef2ff; margin-top: 0;">Ametheus Esports — Administrative Communication</h2>
            <p>Hello <strong>{payload.recipient_name or 'Contender'}</strong>,</p>
            <div style="background-color: #121526; border-left: 4px solid #3ef2ff; padding: 16px; margin: 20px 0; border-radius: 4px;">
                <p style="margin: 0; white-space: pre-wrap; font-size: 14px; color: #f4f6fa;">{payload.message}</p>
            </div>
            <p style="font-size: 12px; color: #888;">Dispatched by Ametheus Administration ({admin_user.username}).</p>
        </div>
    </div>
    """

    res = await send_personal_email_with_details(
        sender_type=snd_ident,
        recipient=email_clean,
        subject=payload.subject.strip(),
        text_content=payload.message.strip(),
        html_content=html_content,
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="SEND_PERSONAL_EMAIL",
        target=f"{email_clean} ({payload.subject})",
        details=f"Identity: {snd_ident}. Delivered: {res.get('success')}. Code: {res.get('error_code')}",
    )

    return res



