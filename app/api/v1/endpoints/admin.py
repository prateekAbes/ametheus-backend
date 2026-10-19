import re
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status, Request, Response, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, get_current_user, require_super_admin, require_organizer
from app.models.user import User
from app.models.admin import (
    TournamentAdminAssignment,
    AuditLog,
)
from app.models.match import Match
from app.models.tournament import Tournament, TournamentStage, StageRound, TournamentRound
from app.models.dispute import Dispute
from app.schemas import (
    AdminAssignmentCreate,
    AuditLogResponse,
    DisputeResolve,
    DisputeResponse,
    AdminMatchPairRequest,
    AdminMatchCreatePayload,
    AdminMatchUpdatePayload,
    AdminPublishRosterPayload,
    AdminMarkMatchLivePayload,
    AdminCompleteMatchPayload,
    AdminMatchScheduleUpdate,
    AdminMatchStatusUpdate,
    AdminMatchScoreUpdate,
    AdminMatchSetupItem,
    AdminSaveLiveStructurePayload,
    TournamentStageCreate,
    TournamentStageUpdate,
    StageReorderPayload,
    StageRoundCreate,
    StageRoundUpdate,
    StageMatchCreate,
    MatchAdvancementPayload,
    MatchSwapPayload,
    TournamentStreamUpdatePayload,
    TournamentFormatUpdatePayload,
    AdminSendNotificationRequest,
    TournamentCommunityUpdatePayload,
    MatchJoinInfoUpdatePayload,
    MatchReminderRequest,
    CreateRoundPayload,
    WalletAuditSummaryResponse,
    RatingAdjustmentBulkRequest,
    RatingReversalRequest,
    RatingAuditLogResponse,
    MatchCredentialsUpdatePayload,
    BattleRoyaleTeamResult,
    BattleRoyaleSubmitResultsPayload,
    ParticipantRatingItem,
    MatchRatingsSubmitPayload,
    RoundPlayerRatingItem,
    RoundRatingsSubmitPayload,
    RoundFinalizePayload,
    TournamentRoundCreatePayload,
    TournamentRoundCredentialsPayload,
    TournamentRoundQualifiersPayload,
    TournamentRoundMvpPayload,
    TournamentFinalMvpPayload,
    TournamentFinalResultsPayload,
    KnockoutMatchCreatePayload,
    KnockoutMatchResultPayload,
    TournamentRoundUpdatePayload,
    KnockoutMatchUpdatePayload,
)
from app.services.rating_service import (
    get_rating_management_hierarchy,
    apply_manual_rating_adjustment,
    reverse_rating_adjustment,
    resolve_match_players,
)
from app.schemas.archive import (
    ArchiveFilterRequest,
    ArchivePreviewResponse,
    ArchiveExecutionResponse,
    UnarchiveResponse,
    BulkUnarchiveRequest,
)
from app.services.archive_service import (
    preview_archive,
    execute_archive,
    archive_single_record,
    unarchive_record,
    unarchive_bulk_records,
)
from app.services.audit import record_audit_log
from app.services.bracket_engine import (
    swap_match_teams,
    calculate_advancement_suggestion,
    advance_match_winner,
    revert_match_advancement,
    get_tournament_history,
    get_team_journey,
)

router = APIRouter()

# ----------------- SUPER ADMIN ONLY -----------------

@router.post("/tournaments/{id}/assign-admin")
async def assign_tournament_admin(
    id: str,
    payload: AdminAssignmentCreate,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    # Rule 2.2: Assign tournament admin in MongoDB
    assignment = TournamentAdminAssignment(
        admin_user_id=payload.admin_user_id,
        tournament_id=id,
        assigned_by=admin_user.id,
    )
    await db.tournament_admin_assignments.insert_one(assignment.to_mongo())

    # Record in cryptographic audit log
    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="ASSIGN_TOURNAMENT_ADMIN",
        target=f"Admin {payload.admin_user_id} -> Tournament {id}",
    )
    return {"status": "success", "message": "Tournament Admin successfully assigned"}

def _format_audit_ts(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc).isoformat()
        return val.isoformat()
    s = str(val).strip()
    if s in ("None", "null", ""):
        return None
    return s


@router.get("/audit-logs", response_model=List[AuditLogResponse])
async def list_audit_logs(
    response: Response,
    is_archived: bool = False,
    search: Optional[str] = None,
    action: Optional[str] = None,
    operator: Optional[str] = None,
    date_range: Optional[str] = None,
    limit: int = 100,
    skip: int = 0,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query: Dict[str, Any] = {}
    if is_archived:
        query["is_archived"] = True
    else:
        query["is_archived"] = {"$ne": True}

    if action and action.upper() != "ALL":
        query["action"] = {"$regex": re.escape(action.strip()), "$options": "i"}
    if operator and operator.strip():
        query["operator"] = {"$regex": re.escape(operator.strip()), "$options": "i"}
    if search and search.strip():
        s = search.strip()
        rgx = {"$regex": re.escape(s), "$options": "i"}
        query["$or"] = [{"operator": rgx}, {"action": rgx}, {"target": rgx}, {"details": rgx}]

    if date_range and date_range.upper() != "ALL":
        now = datetime.now(timezone.utc)
        dr = date_range.upper()
        if dr == "TODAY":
            start_date = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
            query["created_at"] = {"$gte": start_date}
        elif dr == "YESTERDAY":
            start_date = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) - timedelta(days=1)
            end_date = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
            query["created_at"] = {"$gte": start_date, "$lt": end_date}
        elif dr == "LAST_7_DAYS":
            query["created_at"] = {"$gte": now - timedelta(days=7)}
        elif dr == "LAST_30_DAYS":
            query["created_at"] = {"$gte": now - timedelta(days=30)}

    total = await db.audit_logs.count_documents(query)
    response.headers["X-Total-Count"] = str(total)

    cursor = db.audit_logs.find(query).sort("created_at", -1).skip(skip).limit(limit)
    logs = await cursor.to_list(length=limit)

    results = []
    for l in logs:
        ts = _format_audit_ts(l.get("created_at")) or _format_audit_ts(l.get("timestamp"))
        results.append(
            AuditLogResponse(
                id=str(l.get("_id", l.get("id"))),
                timestamp=ts,
                created_at=ts,
                operator=l.get("operator", "SYSTEM"),
                role=l.get("role", "SUPER_ADMIN"),
                action=l.get("action", "EVENT"),
                target=l.get("target", "N/A"),
                details=l.get("details"),
                ip_address=l.get("ip_address", "127.0.0.1"),
                prev_hash=l.get("prev_hash"),
                record_hash=l.get("record_hash"),
                is_archived=bool(l.get("is_archived", False)),
                archived_at=_format_audit_ts(l.get("archived_at")),
                archived_by=l.get("archived_by"),
                archive_reason=l.get("archive_reason"),
            )
        )
    return results


@router.get("/audit-logs/count")
async def get_audit_logs_count(
    is_archived: bool = False,
    search: Optional[str] = None,
    action: Optional[str] = None,
    operator: Optional[str] = None,
    date_range: Optional[str] = None,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query: Dict[str, Any] = {}
    if is_archived:
        query["is_archived"] = True
    else:
        query["is_archived"] = {"$ne": True}

    if action and action.upper() != "ALL":
        query["action"] = {"$regex": re.escape(action.strip()), "$options": "i"}
    if operator and operator.strip():
        query["operator"] = {"$regex": re.escape(operator.strip()), "$options": "i"}
    if search and search.strip():
        s = search.strip()
        rgx = {"$regex": re.escape(s), "$options": "i"}
        query["$or"] = [{"operator": rgx}, {"action": rgx}, {"target": rgx}, {"details": rgx}]

    if date_range and date_range.upper() != "ALL":
        now = datetime.now(timezone.utc)
        dr = date_range.upper()
        if dr == "TODAY":
            start_date = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
            query["created_at"] = {"$gte": start_date}
        elif dr == "YESTERDAY":
            start_date = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) - timedelta(days=1)
            end_date = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
            query["created_at"] = {"$gte": start_date, "$lt": end_date}
        elif dr == "LAST_7_DAYS":
            query["created_at"] = {"$gte": now - timedelta(days=7)}
        elif dr == "LAST_30_DAYS":
            query["created_at"] = {"$gte": now - timedelta(days=30)}

    total = await db.audit_logs.count_documents(query)
    return {"total": total}


# ----------------- TOURNAMENT ADMIN (SCOPED) -----------------

@router.get("/my-tournaments")
async def get_my_assigned_tournaments(
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    if current_user.role == "SUPER_ADMIN":
        cursor = db.tournaments.find()
        docs = await cursor.to_list(length=100)
        from app.core.database import docs_to_list
        return docs_to_list(docs)

    # Query assigned tournaments
    assign_cursor = db.tournament_admin_assignments.find({"admin_user_id": current_user.id})
    assignments = await assign_cursor.to_list(length=100)
    tourn_ids = [a["tournament_id"] for a in assignments if a.get("tournament_id")]
    
    if not tourn_ids:
        return []

    cursor = db.tournaments.find({"$or": [{"_id": {"$in": tourn_ids}}, {"id": {"$in": tourn_ids}}]})
    docs = await cursor.to_list(length=100)
    from app.core.database import docs_to_list
    return docs_to_list(docs)

@router.delete("/tournaments/{id}")
async def delete_tournament_admin_endpoint(
    id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from app.api.v1.endpoints.organizer import delete_tournament
    return await delete_tournament(id=id, organizer=admin_user, db=db)

@router.post("/matches/{id}/verify-result")
async def verify_match_result(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    match_doc = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")

    match = Match.from_mongo(match_doc)

    # Verify authorization for this tournament
    privileged_roles = ["SUPER_ADMIN", "ORGANIZER"]
    if current_user.role not in privileged_roles:
        assign_check = await db.tournament_admin_assignments.find_one({
            "admin_user_id": current_user.id,
            "tournament_id": match.tournament_id,
        })
        if not assign_check:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You are not assigned to manage this match's tournament (Rule 2.2)",
            )

    winner_team_id = match.winner_team_id
    if not winner_team_id or winner_team_id not in [match.team_a_id, match.team_b_id]:
        winner_team_id = match.team_a_id if (match.score_a or 0) >= (match.score_b or 0) else match.team_b_id

    from app.services.bracket_engine import advance_bracket_match
    next_match = await advance_bracket_match(
        tournament_id=match.tournament_id,
        completed_match=match,
        winner_team_id=winner_team_id,
        db=db,
    )

    # Add audit log
    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="VERIFY_MATCH_RESULT",
        target=f"Match {match.id} (Winner: {winner_team_id})",
    )

    from app.core.ws import ws_manager
    await ws_manager.broadcast({
        "type": "MATCH_VERIFIED",
        "match_id": match.id,
        "winner_team_id": winner_team_id,
        "status": "COMPLETED",
        "next_match_id": next_match.id if next_match else None,
    })

    return {
        "status": "success",
        "message": "Match result verified and winner advanced in bracket",
        "winner_team_id": winner_team_id,
        "next_match_id": next_match.id if next_match else None,
    }

class MatchForfeitPayload(BaseModel):
    forfeited_team_id: str
    reason: str = "Disqualification or check-in forfeiture"

@router.post("/matches/{id}/forfeit")
async def force_match_forfeit(
    id: str,
    payload: MatchForfeitPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    privileged_roles = ["SUPER_ADMIN", "ORGANIZER"]
    if current_user.role not in privileged_roles:
        raise HTTPException(status_code=403, detail="Forbidden: Organizer or Super Admin authority required")

    match_doc = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")

    match = Match.from_mongo(match_doc)
    winner_id = match.team_b_id if payload.forfeited_team_id == match.team_a_id else match.team_a_id

    # Update match status to COMPLETED with forfeit note
    await db.matches.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {
            "status": "COMPLETED",
            "winner_team_id": winner_id,
            "dispute_notes": f"FORFEIT: Team {payload.forfeited_team_id} forfeited. Reason: {payload.reason}",
        }}
    )

    from app.services.bracket_engine import advance_bracket_match
    next_match = await advance_bracket_match(
        tournament_id=match.tournament_id,
        completed_match=match,
        winner_team_id=winner_id,
        db=db,
    )

    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="FORCE_MATCH_FORFEIT",
        target=f"Match {match.id} (Forfeited: {payload.forfeited_team_id}, Winner: {winner_id})",
        details=payload.reason,
    )
    return {"status": "success", "message": f"Team {payload.forfeited_team_id} marked as forfeited. Winner: {winner_id}"}

class MatchScoreOverridePayload(BaseModel):
    score_a: int
    score_b: int
    reason: str = "Official arbitrator score correction"

@router.post("/matches/{id}/override-score")
async def override_match_score(
    id: str,
    payload: MatchScoreOverridePayload,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    privileged_roles = ["SUPER_ADMIN", "ORGANIZER"]
    if current_user.role not in privileged_roles:
        raise HTTPException(status_code=403, detail="Forbidden: Organizer or Super Admin authority required")

    match_doc = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")

    match = Match.from_mongo(match_doc)
    winner_id = match.team_a_id if payload.score_a >= payload.score_b else match.team_b_id

    await db.matches.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {
            "score_a": payload.score_a,
            "score_b": payload.score_b,
            "status": "COMPLETED",
            "winner_team_id": winner_id,
            "dispute_notes": f"SCORE OVERRIDE ({payload.score_a}-{payload.score_b}): {payload.reason}",
        }}
    )

    from app.services.bracket_engine import advance_bracket_match
    next_match = await advance_bracket_match(
        tournament_id=match.tournament_id,
        completed_match=match,
        winner_team_id=winner_id,
        db=db,
    )

    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="OVERRIDE_MATCH_SCORE",
        target=f"Match {match.id} ({payload.score_a} - {payload.score_b})",
        details=payload.reason,
    )
    return {"status": "success", "message": f"Score overridden to {payload.score_a}-{payload.score_b} and certified"}

class MatchRejectProofPayload(BaseModel):
    reason: str = "Evidence image blurry or inconclusive"

@router.post("/matches/{id}/reject-proof")
async def reject_match_proof(
    id: str,
    payload: MatchRejectProofPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    privileged_roles = ["SUPER_ADMIN", "ORGANIZER"]
    if current_user.role not in privileged_roles:
        raise HTTPException(status_code=403, detail="Forbidden: Organizer or Super Admin authority required")

    match_doc = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")

    # Reset match status to LIVE and attach rejection note
    await db.matches.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {
            "status": "LIVE",
            "dispute_notes": f"EVIDENCE REJECTED: {payload.reason}. Team captains must re-upload valid proof.",
        }}
    )

    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="REJECT_MATCH_PROOF",
        target=f"Match {id}",
        details=payload.reason,
    )
    return {"status": "success", "message": f"Match proof rejected. Status reset to LIVE for re-upload."}

@router.post("/users/{id}/promote-organizer")
async def promote_user_to_organizer(
    id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Promote an existing player account to ORGANIZER (Rule 22).
    """
    user_doc = await db.users.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")

    await db.users.update_one({"_id": user_doc["_id"]}, {"$set": {"role": "ORGANIZER"}})

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="PROMOTE_ORGANIZER",
        target=f"User {user_doc['username']} ({id}) -> ORGANIZER",
    )
    return {"status": "success", "message": f"User {user_doc['username']} successfully promoted to ORGANIZER"}

class UserRoleUpdate(BaseModel):
    role: str

@router.post("/users/{id}/set-role")
async def set_user_role(
    id: str,
    payload: UserRoleUpdate,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    valid_roles = [
        "PLAYER",
        "ORGANIZER",
        "SUPER_ADMIN",
    ]
    if payload.role not in valid_roles:
        raise HTTPException(status_code=400, detail=f"Invalid role. Must be one of: {valid_roles}")
    user_doc = await db.users.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")
    await db.users.update_one({"_id": user_doc["_id"]}, {"$set": {"role": payload.role}})
    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="UPDATE_USER_ROLE",
        target=f"User {user_doc['username']} -> {payload.role}",
    )
    return {"status": "success", "message": f"User {user_doc['username']} role updated to {payload.role}"}

@router.get("/users")
async def list_platform_users(
    search: Optional[str] = None,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    List platform users with search support for admin/organizer dashboards and notification dispatcher.
    """
    query: dict[str, Any] = {}
    if search and search.strip():
        term = search.strip()
        query = {
            "$or": [
                {"username": {"$regex": term, "$options": "i"}},
                {"email": {"$regex": term, "$options": "i"}},
                {"display_name": {"$regex": term, "$options": "i"}},
            ]
        }
    cursor = db.users.find(query).sort("created_at", -1)
    users = await cursor.to_list(length=100)
    return [
        {
            "id": str(u.get("_id", u.get("id"))),
            "clerk_user_id": u.get("clerk_user_id"),
            "username": u.get("username", "user"),
            "display_name": u.get("display_name"),
            "email": u.get("email", ""),
            "role": u.get("role", "PLAYER"),
            "inr_balance": str(round(u.get("inr_balance_paise", 0) / 100, 2)) if "inr_balance_paise" in u else str(u.get("inr_balance", "0")),
            "created_at": str(u.get("created_at")),
        }
        for u in users
    ]

@router.get("/users/{user_id}/wallet-summary")
async def get_user_wallet_summary(
    user_id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns full wallet accounting for a specific user:
    available balance, reserved balance, total deposited, total withdrawn, total spent,
    and the chronological ledger transactions.
    """
    user_doc = await db.users.find_one({"$or": [{"_id": user_id}, {"id": user_id}, {"clerk_user_id": user_id}]})
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")

    target_id = str(user_doc.get("_id", user_doc.get("id")))
    from app.services.wallet_service import get_user_wallet_balances
    balances = await get_user_wallet_balances(db, target_id)

    # Fetch all ledger entries for this user
    cursor = db.wallet_ledger.find({"user_id": target_id}).sort("created_at", -1)
    ledger_docs = await cursor.to_list(length=200)

    total_deposited_paise = 0
    total_withdrawn_paise = 0
    total_spent_paise = 0

    ledger_entries = []
    for entry in ledger_docs:
        amt = entry.get("amount_paise", 0)
        direction = entry.get("direction", "")
        tt = entry.get("txn_type", "")

        if direction == "CREDIT" or tt in ["WALLET_TOPUP", "PRIZE_PAYOUT", "ADMIN_CREDIT"]:
            total_deposited_paise += amt
        elif tt in ["WITHDRAWAL_FINALIZE", "WITHDRAWAL"]:
            total_withdrawn_paise += amt
        elif direction == "DEBIT" or tt in ["GAME_ENTRY_PAYMENT", "TOURNAMENT_ENTRY", "PLATFORM_FEE"]:
            total_spent_paise += amt

        ledger_entries.append({
            "id": str(entry.get("_id", entry.get("id"))),
            "txn_type": tt,
            "direction": direction,
            "amount_inr": round(amt / 100, 2),
            "amount_paise": amt,
            "balance_after_inr": round(entry.get("balance_after_paise", 0) / 100, 2),
            "reference_id": entry.get("reference_id"),
            "description": entry.get("description", ""),
            "created_at": str(entry.get("created_at")),
        })

    return {
        "user_id": target_id,
        "username": user_doc.get("username"),
        "email": user_doc.get("email"),
        "available_balance_inr": balances["available_balance"],
        "reserved_balance_inr": balances["reserved_balance"],
        "total_balance_inr": balances["inr_balance"],
        "total_deposited_inr": round(total_deposited_paise / 100, 2),
        "total_withdrawn_inr": round(total_withdrawn_paise / 100, 2),
        "total_spent_inr": round(total_spent_paise / 100, 2),
        "ledger": ledger_entries,
    }

@router.get("/disputes", response_model=List[DisputeResponse])
async def list_disputes(
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    cursor = db.disputes.find().sort("created_at", -1)
    disputes = await cursor.to_list(length=100)
    return [
        DisputeResponse(
            id=str(d.get("_id", d.get("id"))),
            tournament_id=d["tournament_id"],
            match_id=d["match_id"],
            submitted_by=d["submitted_by"],
            reason=d["reason"],
            description=d["description"],
            evidence_url=d.get("evidence_url"),
            status=d.get("status", "OPEN"),
            created_at=str(d.get("created_at")),
        )
        for d in disputes
    ]

@router.post("/disputes/{id}/resolve")
async def resolve_dispute(
    id: str,
    payload: DisputeResolve,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    dispute_doc = await db.disputes.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not dispute_doc:
        raise HTTPException(status_code=404, detail="Dispute not found")

    await db.disputes.update_one(
        {"_id": dispute_doc["_id"]},
        {"$set": {
            "status": payload.decision,
            "resolution_notes": payload.resolution_notes,
            "resolved_by": admin_user.id,
        }}
    )

    # Unfreeze associated match if it was suspended in DISPUTED state
    match_id = dispute_doc.get("match_id")
    if match_id:
        match_doc = await db.matches.find_one({"$or": [{"_id": match_id}, {"id": match_id}]})
        if match_doc and match_doc.get("status") == "DISPUTED":
            new_status = "PENDING_VERIFICATION" if (match_doc.get("score_a") is not None and match_doc.get("score_b") is not None) else "LIVE"
            notes = f"Dispute resolved ({payload.decision}): {payload.resolution_notes}"
            await db.matches.update_one(
                {"_id": match_doc["_id"]},
                {"$set": {"status": new_status, "dispute_notes": notes}}
            )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="RESOLVE_DISPUTE",
        target=f"Dispute {id} ({payload.decision})",
        details=payload.resolution_notes,
    )
    return {
        "status": "success",
        "message": f"Dispute {id} marked as {payload.decision} by Super Admin",
    }

@router.get("/stats")
async def get_admin_stats(
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Real-time platform KPI statistics for Super Admin dashboard from MongoDB.
    """
    total_users = await db.users.count_documents({})
    total_tournaments = await db.tournaments.count_documents({})
    active_tournaments = await db.tournaments.count_documents({
        "status": {"$in": ["REGISTRATION_OPEN", "ONGOING", "LIVE"]}
    })
    total_teams = await db.teams.count_documents({})
    total_games = await db.games.count_documents({})
    pending_disputes = await db.disputes.count_documents({"status": "OPEN"})
    active_matches = await db.matches.count_documents({
        "status": {"$in": ["LIVE", "CHECK_IN_OPEN", "PENDING_VERIFICATION", "DISPUTED"]}
    })

    # Compute platform escrow vault: sum of all approved entry payments (in paise → INR)
    pipeline = [
        {"$match": {"status": "APPROVED", "payment_type": "GAME_ENTRY"}},
        {"$group": {"_id": None, "total": {"$sum": "$amount_paise"}}},
    ]
    agg = await db.game_payments.aggregate(pipeline).to_list(1)
    platform_escrow_paise = agg[0]["total"] if agg else 0
    platform_escrow_vault = platform_escrow_paise // 100  # convert paise to INR

    return {
        "total_users": total_users,
        "total_tournaments": total_tournaments,
        "active_tournaments": active_tournaments,
        "total_teams": total_teams,
        "total_games": total_games,
        "pending_disputes": pending_disputes,
        "active_matches": active_matches,
        "platform_escrow_vault": platform_escrow_vault,
    }

@router.post("/seed-catalog")
async def trigger_seed_catalog(
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from app.seed import populate_seed_data
    await populate_seed_data(db)
    return {"status": "success", "message": "Platform seed data created successfully"}

# ----------------- PERSONA & AVATAR POOL MANAGEMENT -----------------

DEFAULT_PERSONA_NAMES = [
    "Shadow", "Nova", "Vortex", "Phantom", "Cyber", "Apex", "Ghost", "Frost",
    "Titan", "Neon", "Viper", "Rogue", "Blaze", "Pulse", "Storm", "Iron",
    "Astral", "Echo", "Zero", "Venom", "Spectre", "Hyper", "Havoc", "Glitch",
    "Atomic", "Zenith", "Omega", "Krypton", "Solar", "Valor"
]

DEFAULT_PERSONA_AVATARS = [
    {
        "id": "stitch-pro-vortex",
        "name": "Pro Vortex Cyber",
        "url": "https://images.unsplash.com/photo-1566492031773-4f4e44671857?q=80&w=300&auto=format&fit=crop"
    },
    {
        "id": "stitch-smash-neon",
        "name": "Neon Striker",
        "url": "https://images.unsplash.com/photo-1534528741775-53994a69daeb?q=80&w=300&auto=format&fit=crop"
    },
    {
        "id": "stitch-tactical-operator",
        "name": "Tactical Operator",
        "url": "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?q=80&w=300&auto=format&fit=crop"
    },
    {
        "id": "bottts-cyber-reaper",
        "name": "Cyber Reaper",
        "url": "https://api.dicebear.com/7.x/bottts/svg?seed=CyberReaper&backgroundColor=0f172a&colors=amber,orange,red"
    },
    {
        "id": "bottts-nova-strike",
        "name": "Nova Strike",
        "url": "https://api.dicebear.com/7.x/bottts/svg?seed=NovaStrike&backgroundColor=1e293b&colors=blue,cyan,sky"
    },
    {
        "id": "bottts-apex-ghost",
        "name": "Apex Ghost",
        "url": "https://api.dicebear.com/7.x/bottts/svg?seed=ApexGhost&backgroundColor=1e1b4b&colors=emerald,teal,green"
    }
]

class PersonaNamePayload(BaseModel):
    name: str

class PersonaAvatarPayload(BaseModel):
    name: str
    url: str

@router.get("/persona-pool")
async def get_persona_pool(db: AsyncIOMotorDatabase = Depends(get_db)):
    """
    Returns the dynamic random name and avatar pool assigned to first-time players.
    """
    doc = await db.platform_settings.find_one({"_id": "persona_pool"})
    if not doc:
        doc = {
            "_id": "persona_pool",
            "names": DEFAULT_PERSONA_NAMES,
            "avatars": DEFAULT_PERSONA_AVATARS,
        }
        await db.platform_settings.insert_one(doc)

    return {
        "names": doc.get("names", DEFAULT_PERSONA_NAMES),
        "avatars": doc.get("avatars", DEFAULT_PERSONA_AVATARS),
    }

@router.post("/persona-pool/names")
async def add_persona_name(
    payload: PersonaNamePayload,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    clean_name = payload.name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")

    await db.platform_settings.update_one(
        {"_id": "persona_pool"},
        {"$addToSet": {"names": clean_name}},
        upsert=True
    )
    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="ADD_PERSONA_NAME",
        target=f"Name: {clean_name}",
    )
    return {"status": "success", "message": f'Added "{clean_name}" to persona names pool'}

@router.delete("/persona-pool/names/{name}")
async def remove_persona_name(
    name: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    await db.platform_settings.update_one(
        {"_id": "persona_pool"},
        {"$pull": {"names": name}}
    )
    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="REMOVE_PERSONA_NAME",
        target=f"Name: {name}",
    )
    return {"status": "success", "message": f'Removed "{name}" from persona names pool'}

@router.post("/persona-pool/avatars")
async def add_persona_avatar(
    payload: PersonaAvatarPayload,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    import uuid
    clean_url = payload.url.strip()
    clean_name = payload.name.strip()
    if not clean_url or not clean_name:
        raise HTTPException(status_code=400, detail="Name and Image URL are required")

    avatar_id = f"avatar-{uuid.uuid4().hex[:8]}"
    new_avatar = {
        "id": avatar_id,
        "name": clean_name,
        "url": clean_url,
    }

    await db.platform_settings.update_one(
        {"_id": "persona_pool"},
        {"$push": {"avatars": new_avatar}},
        upsert=True
    )
    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="ADD_PERSONA_AVATAR",
        target=f"Avatar: {clean_name}",
        details=clean_url,
    )
    return {"status": "success", "message": f'Added "{clean_name}" to avatar pool', "avatar": new_avatar}

@router.delete("/persona-pool/avatars/{avatar_id}")
async def remove_persona_avatar(
    avatar_id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    await db.platform_settings.update_one(
        {"_id": "persona_pool"},
        {"$pull": {"avatars": {"$or": [{"id": avatar_id}, {"url": avatar_id}]}}}
    )
    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="REMOVE_PERSONA_AVATAR",
        target=f"Avatar ID: {avatar_id}",
    )
    return {"status": "success", "message": f"Removed avatar {avatar_id} from persona pool"}

# ----------------- PAYMENT VERIFICATION & PAYOUTS -----------------

class PaymentVerifyActionPayload(BaseModel):
    rejection_reason: Optional[str] = None

class PayoutCompletePayload(BaseModel):
    outgoing_utr: str

class PayoutRejectPayload(BaseModel):
    rejection_reason: str

class CreateWinningPayoutPayload(BaseModel):
    user_id: str
    game_id: str
    rank_or_placement: str
    amount_inr: float
    destination: str = "WALLET"  # WALLET, UPI
    upi_id: Optional[str] = None

def parse_payment_date_preset(
    date_preset: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Tuple[Optional[datetime], Optional[datetime]]:
    dp = (date_preset or "").upper().strip()
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist_tz)

    if dp == "TODAY":
        start_ist = datetime(now_ist.year, now_ist.month, now_ist.day, 0, 0, 0, tzinfo=ist_tz)
        end_ist = datetime(now_ist.year, now_ist.month, now_ist.day, 23, 59, 59, 999999, tzinfo=ist_tz)
        return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)

    elif dp == "YESTERDAY":
        y_ist = now_ist - timedelta(days=1)
        start_ist = datetime(y_ist.year, y_ist.month, y_ist.day, 0, 0, 0, tzinfo=ist_tz)
        end_ist = datetime(y_ist.year, y_ist.month, y_ist.day, 23, 59, 59, 999999, tzinfo=ist_tz)
        return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)

    elif dp == "LAST_7_DAYS":
        start_utc = datetime.now(timezone.utc) - timedelta(days=7)
        end_utc = datetime.now(timezone.utc)
        return start_utc, end_utc

    elif dp == "THIS_WEEK":
        mon = now_ist - timedelta(days=now_ist.weekday())
        start_ist = datetime(mon.year, mon.month, mon.day, 0, 0, 0, tzinfo=ist_tz)
        end_ist = datetime.now(timezone.utc)
        return start_ist.astimezone(timezone.utc), end_ist

    elif dp == "LAST_WEEK":
        mon = now_ist - timedelta(days=now_ist.weekday())
        last_mon = mon - timedelta(days=7)
        start_ist = datetime(last_mon.year, last_mon.month, last_mon.day, 0, 0, 0, tzinfo=ist_tz)
        end_ist = datetime(mon.year, mon.month, mon.day, 0, 0, 0, tzinfo=ist_tz) - timedelta(microseconds=1)
        return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)

    elif dp == "THIS_MONTH":
        start_ist = datetime(now_ist.year, now_ist.month, 1, 0, 0, 0, tzinfo=ist_tz)
        end_ist = datetime.now(timezone.utc)
        return start_ist.astimezone(timezone.utc), end_ist

    elif dp == "LAST_MONTH":
        if now_ist.month == 1:
            start_ist = datetime(now_ist.year - 1, 12, 1, 0, 0, 0, tzinfo=ist_tz)
            end_ist = datetime(now_ist.year, 1, 1, 0, 0, 0, tzinfo=ist_tz) - timedelta(microseconds=1)
        else:
            start_ist = datetime(now_ist.year, now_ist.month - 1, 1, 0, 0, 0, tzinfo=ist_tz)
            end_ist = datetime(now_ist.year, now_ist.month, 1, 0, 0, 0, tzinfo=ist_tz) - timedelta(microseconds=1)
        return start_ist.astimezone(timezone.utc), end_ist.astimezone(timezone.utc)

    elif dp == "THIS_YEAR":
        start_ist = datetime(now_ist.year, 1, 1, 0, 0, 0, tzinfo=ist_tz)
        end_ist = datetime.now(timezone.utc)
        return start_ist.astimezone(timezone.utc), end_ist

    # CUSTOM or explicit start_date / end_date
    s_dt: Optional[datetime] = None
    e_dt: Optional[datetime] = None
    if start_date:
        try:
            s_clean = start_date.strip().replace("Z", "+00:00")
            if len(s_clean) == 10:  # YYYY-MM-DD
                s_dt = datetime.fromisoformat(f"{s_clean}T00:00:00+05:30").astimezone(timezone.utc)
            else:
                s_dt = datetime.fromisoformat(s_clean)
                if s_dt.tzinfo is None:
                    s_dt = s_dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    if end_date:
        try:
            e_clean = end_date.strip().replace("Z", "+00:00")
            if len(e_clean) == 10:  # YYYY-MM-DD
                e_dt = datetime.fromisoformat(f"{e_clean}T23:59:59.999999+05:30").astimezone(timezone.utc)
            else:
                e_dt = datetime.fromisoformat(e_clean)
                if e_dt.tzinfo is None:
                    e_dt = e_dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass

    return s_dt, e_dt


async def build_payments_query(
    db: AsyncIOMotorDatabase,
    status_filter: Optional[str] = None,
    game_id: Optional[str] = None,
    search: Optional[str] = None,
    payment_type: Optional[str] = None,
    tournament_id: Optional[str] = None,
    team_id: Optional[str] = None,
    date_preset: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    is_archived: bool = False,
) -> Dict[str, Any]:
    and_conditions: List[Dict[str, Any]] = [{"is_dummy": {"$ne": True}}]

    # Archive segregation:
    # Archived view shows approved, rejected, or explicitly archived payments.
    # Active pending view shows strictly pending / unapproved / unrejected records.
    if is_archived:
        and_conditions.append({
            "$or": [
                {"is_archived": True},
                {"status": {"$in": ["APPROVED", "REJECTED"]}},
            ]
        })
    else:
        and_conditions.append({
            "is_archived": {"$ne": True},
            "status": {"$nin": ["APPROVED", "REJECTED"]},
        })

    # Status filter
    if status_filter and status_filter.upper() != "ALL":
        sf = status_filter.upper()
        if sf == "PENDING":
            and_conditions.append({"status": {"$in": ["PENDING", "PENDING_VERIFICATION", "SUBMITTED", "INITIATED"]}})
        elif sf == "APPROVED":
            and_conditions.append({"status": "APPROVED"})
        elif sf == "REJECTED":
            and_conditions.append({"status": "REJECTED"})
        elif sf == "ARCHIVED":
            and_conditions.append({"is_archived": True})
        else:
            and_conditions.append({"status": sf})

    # Payment type filter
    if payment_type and payment_type.upper() != "ALL":
        and_conditions.append({"payment_type": payment_type.upper()})

    # Specific tournament / team / game filters
    if tournament_id:
        and_conditions.append({
            "$or": [
                {"tournament_id": tournament_id},
                {"tournament_id": str(tournament_id)},
            ]
        })
    if team_id:
        and_conditions.append({"team_id": team_id})
    if game_id:
        and_conditions.append({"game_id": game_id})

    # Amount range filters
    amt_q: Dict[str, Any] = {}
    if min_amount is not None:
        try:
            amt_q["$gte"] = float(min_amount)
        except ValueError:
            pass
    if max_amount is not None:
        try:
            amt_q["$lte"] = float(max_amount)
        except ValueError:
            pass
    if amt_q:
        and_conditions.append({"amount_inr": amt_q})

    # Date range filters
    s_dt, e_dt = parse_payment_date_preset(date_preset, start_date, end_date)
    if s_dt or e_dt:
        date_q: Dict[str, Any] = {}
        date_iso_q: Dict[str, Any] = {}
        if s_dt:
            date_q["$gte"] = s_dt
            date_iso_q["$gte"] = s_dt.isoformat()
        if e_dt:
            date_q["$lte"] = e_dt
            date_iso_q["$lte"] = e_dt.isoformat()

        and_conditions.append({
            "$or": [
                {"created_at": date_q},
                {"created_at": date_iso_q},
            ]
        })

    # Comprehensive search across references, users, tournaments, VPAs
    if search and search.strip():
        s_pat = re.escape(search.strip())
        rgx = {"$regex": s_pat, "$options": "i"}

        matched_uids = []
        try:
            users_cursor = db.users.find(
                {"$or": [{"username": rgx}, {"email": rgx}, {"display_name": rgx}]},
                {"_id": 1, "id": 1, "clerk_user_id": 1}
            ).limit(40)
            users_matched = await users_cursor.to_list(length=40)
            for u in users_matched:
                if u.get("_id"): matched_uids.append(str(u["_id"]))
                if u.get("id"): matched_uids.append(str(u["id"]))
                if u.get("clerk_user_id"): matched_uids.append(str(u["clerk_user_id"]))
        except Exception:
            pass

        matched_tids = []
        try:
            tourn_cursor = db.tournaments.find(
                {"$or": [{"title": rgx}, {"name": rgx}, {"slug": rgx}]},
                {"_id": 1, "id": 1, "slug": 1}
            ).limit(40)
            tourns_matched = await tourn_cursor.to_list(length=40)
            for t in tourns_matched:
                if t.get("_id"): matched_tids.append(str(t["_id"]))
                if t.get("id"): matched_tids.append(str(t["id"]))
                if t.get("slug"): matched_tids.append(str(t["slug"]))
        except Exception:
            pass

        search_or: List[Dict[str, Any]] = [
            {"utr": rgx},
            {"id": rgx},
            {"_id": rgx},
            {"user_id": rgx},
            {"team_name": rgx},
            {"team_id": rgx},
            {"tournament_id": rgx},
            {"game_name": rgx},
            {"sender_upi_id": rgx},
            {"upi_id": rgx},
            {"notes": rgx},
            {"description": rgx},
        ]
        if matched_uids:
            search_or.append({"user_id": {"$in": matched_uids}})
        if matched_tids:
            search_or.append({"tournament_id": {"$in": matched_tids}})

        and_conditions.append({"$or": search_or})

    return {"$and": and_conditions} if len(and_conditions) > 1 else and_conditions[0]


@router.get("/payments")
async def list_admin_payments(
    status_filter: Optional[str] = None,
    game_id: Optional[str] = None,
    search: Optional[str] = None,
    payment_type: Optional[str] = None,
    tournament_id: Optional[str] = None,
    team_id: Optional[str] = None,
    date_preset: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    sort_by: Optional[str] = "NEWEST",
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    is_archived: bool = False,
    with_meta: bool = False,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    import math
    query = await build_payments_query(
        db=db,
        status_filter=status_filter,
        game_id=game_id,
        search=search,
        payment_type=payment_type,
        tournament_id=tournament_id,
        team_id=team_id,
        date_preset=date_preset,
        start_date=start_date,
        end_date=end_date,
        min_amount=min_amount,
        max_amount=max_amount,
        is_archived=is_archived,
    )

    sort_spec = [("created_at", -1)]
    if sort_by:
        sb = sort_by.upper().strip()
        if sb == "OLDEST":
            sort_spec = [("created_at", 1)]
        elif sb == "AMOUNT_DESC":
            sort_spec = [("amount_inr", -1), ("created_at", -1)]
        elif sb == "AMOUNT_ASC":
            sort_spec = [("amount_inr", 1), ("created_at", -1)]
        elif sb == "RECENTLY_PROCESSED":
            sort_spec = [("verified_at", -1), ("archived_at", -1), ("created_at", -1)]
        elif sb == "OLDEST_PENDING":
            sort_spec = [("created_at", 1)]

    skip = (page - 1) * limit
    total = await db.game_payments.count_documents(query)

    cursor = db.game_payments.find(query).sort(sort_spec).skip(skip).limit(limit)
    docs = await cursor.to_list(length=limit)

    user_ids = [str(d.get("user_id")) for d in docs if d.get("user_id")]
    u_map = {}
    if user_ids:
        users = await db.users.find({"$or": [{"_id": {"$in": user_ids}}, {"id": {"$in": user_ids}}, {"clerk_user_id": {"$in": user_ids}}]}).to_list(len(user_ids) + 10)
        for u in users:
            uid = str(u.get("_id", u.get("id")))
            u_map[uid] = u
            if u.get("clerk_user_id"):
                u_map[str(u["clerk_user_id"])] = u

    tourn_ids = [str(d.get("tournament_id")) for d in docs if d.get("tournament_id")]
    t_map = {}
    if tourn_ids:
        tourns = await db.tournaments.find({"$or": [{"_id": {"$in": tourn_ids}}, {"id": {"$in": tourn_ids}}, {"slug": {"$in": tourn_ids}}]}).to_list(len(tourn_ids) + 10)
        for t in tourns:
            tid = str(t.get("_id", t.get("id")))
            t_map[tid] = t
            if t.get("slug"):
                t_map[t["slug"]] = t

    def _format_iso(val: Any) -> Optional[str]:
        if not val:
            return None
        if hasattr(val, "isoformat"):
            return val.isoformat()
        return str(val)

    for d in docs:
        d["id"] = str(d.get("_id", d.get("id")))
        d.pop("_id", None)
        d["is_archived"] = bool(d.get("is_archived", False) or d.get("status") in ["APPROVED", "REJECTED"])
        d["archived_at"] = _format_iso(d.get("archived_at"))
        d["created_at"] = _format_iso(d.get("created_at"))
        d["updated_at"] = _format_iso(d.get("updated_at"))
        d["verified_at"] = _format_iso(d.get("verified_at"))
        d["approved_at"] = _format_iso(d.get("approved_at") or (d.get("verified_at") if d.get("status") == "APPROVED" else None))
        d["rejected_at"] = _format_iso(d.get("rejected_at") or (d.get("verified_at") if d.get("status") == "REJECTED" else None))

        if not d.get("approved_by") and d.get("status") == "APPROVED":
            d["approved_by"] = d.get("verified_by")
        if not d.get("rejected_by") and d.get("status") == "REJECTED":
            d["rejected_by"] = d.get("verified_by")

        uid = str(d.get("user_id", ""))
        if uid in u_map:
            u_info = u_map[uid]
            if not d.get("user_email"):
                d["user_email"] = u_info.get("email")
            if not d.get("user_name"):
                d["user_name"] = u_info.get("display_name") or u_info.get("username")
            if not d.get("user_username"):
                d["user_username"] = u_info.get("username")

        tid = str(d.get("tournament_id", ""))
        if tid in t_map:
            t_info = t_map[tid]
            if not d.get("tournament_title"):
                d["tournament_title"] = t_info.get("title")
            if not d.get("game_name"):
                d["game_name"] = t_info.get("game_name")

    if with_meta:
        return {
            "items": docs,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": max(1, math.ceil(total / limit)),
        }
    return docs


@router.get("/payments/summary")
async def get_admin_payments_summary(
    status_filter: Optional[str] = None,
    game_id: Optional[str] = None,
    search: Optional[str] = None,
    payment_type: Optional[str] = None,
    tournament_id: Optional[str] = None,
    team_id: Optional[str] = None,
    date_preset: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    is_archived: bool = False,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = await build_payments_query(
        db=db,
        status_filter=status_filter,
        game_id=game_id,
        search=search,
        payment_type=payment_type,
        tournament_id=tournament_id,
        team_id=team_id,
        date_preset=date_preset,
        start_date=start_date,
        end_date=end_date,
        min_amount=min_amount,
        max_amount=max_amount,
        is_archived=is_archived,
    )

    pipeline = [
        {"$match": query},
        {
            "$group": {
                "_id": None,
                "matching_count": {"$sum": 1},
                "total_volume_inr": {"$sum": {"$ifNull": ["$amount_inr", 0]}},
                "pending_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PENDING", "PENDING_VERIFICATION", "SUBMITTED", "INITIATED"]]},
                            1,
                            0,
                        ]
                    }
                },
                "pending_volume_inr": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PENDING", "PENDING_VERIFICATION", "SUBMITTED", "INITIATED"]]},
                            {"$ifNull": ["$amount_inr", 0]},
                            0,
                        ]
                    }
                },
                "approved_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "APPROVED"]}, 1, 0]}
                },
                "approved_volume_inr": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "APPROVED"]},
                            {"$ifNull": ["$amount_inr", 0]},
                            0,
                        ]
                    }
                },
                "rejected_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "REJECTED"]}, 1, 0]}
                },
                "rejected_volume_inr": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "REJECTED"]},
                            {"$ifNull": ["$amount_inr", 0]},
                            0,
                        ]
                    }
                },
            }
        }
    ]

    cursor = db.game_payments.aggregate(pipeline)
    res_list = await cursor.to_list(length=1)
    if res_list:
        res = res_list[0]
        res.pop("_id", None)
        return res
    return {
        "matching_count": 0,
        "total_volume_inr": 0.0,
        "pending_count": 0,
        "pending_volume_inr": 0.0,
        "approved_count": 0,
        "approved_volume_inr": 0.0,
        "rejected_count": 0,
        "rejected_volume_inr": 0.0,
    }


@router.post("/payments/{payment_id}/approve")
async def approve_payment(
    payment_id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from bson import ObjectId
    from datetime import datetime, timezone

    p_query = {"$or": [{"_id": payment_id}, {"id": payment_id}]}
    if ObjectId.is_valid(payment_id):
        p_query["$or"].append({"_id": ObjectId(payment_id)})

    payment = await db.game_payments.find_one(p_query)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment record not found")

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # Idempotency: If already approved, ensure archived and return success
    if payment.get("status") == "APPROVED":
        if not payment.get("is_archived"):
            await db.game_payments.update_one(
                {"_id": payment["_id"]},
                {
                    "$set": {
                        "is_archived": True,
                        "archived_at": now_iso,
                        "archived_by": admin_user.username,
                        "archive_reason": f"Payment approved by {admin_user.username}",
                    }
                }
            )
        return {"status": "success", "message": "Payment was already approved", "payment_id": payment_id, "new_status": "APPROVED"}

    # Business rule: Cannot approve an already rejected payment
    if payment.get("status") == "REJECTED":
        raise HTTPException(
            status_code=400,
            detail="Cannot approve an already rejected payment. Please issue a new payment request if required.",
        )

    if not payment.get("utr") or not str(payment.get("utr")).strip():
        raise HTTPException(
            status_code=400,
            detail="Cannot approve payment without a valid submitted UTR reference.",
        )

    if payment.get("payment_type") == "WALLET_TOPUP" and not (payment.get("screenshot_url") or payment.get("proof_url")):
        raise HTTPException(
            status_code=400,
            detail="Cannot approve wallet top-up payment without a verified payment screenshot.",
        )

    # Transition from any valid pending status to APPROVED and auto-archive
    valid_statuses = ["PENDING", "PENDING_VERIFICATION", "SUBMITTED", "INITIATED"]
    update_res = await db.game_payments.update_one(
        {"_id": payment["_id"], "status": {"$in": valid_statuses}},
        {
            "$set": {
                "status": "APPROVED",
                "verified_by": admin_user.username,
                "verified_at": now,
                "approved_by": admin_user.username,
                "approved_at": now,
                "is_archived": True,
                "archived_at": now_iso,
                "archived_by": admin_user.username,
                "archive_reason": f"Payment approved by {admin_user.username}",
            }
        }
    )

    if update_res.modified_count == 0 and payment.get("status") not in valid_statuses:
        return {"status": "success", "message": f"Payment is already {payment.get('status')}"}

    # If it was a tournament entry payment, set registration to PAID
    if payment.get("payment_type") == "TOURNAMENT_ENTRY":
        reg_id = payment.get("registration_id")
        tid = payment.get("tournament_id")
        reg_query = {"$or": [{"_id": reg_id}, {"id": reg_id}]} if reg_id else {"payment_id": payment_id}
        reg_doc = await db.tournament_registrations.find_one(reg_query)
        if reg_doc:
            await db.tournament_registrations.update_one(
                {"_id": reg_doc["_id"]},
                {"$set": {"payment_status": "PAID"}}
            )
            actual_tid = reg_doc.get("tournament_id") or tid
            if actual_tid:
                reg_count = await db.tournament_registrations.count_documents({
                    "$or": [{"tournament_id": actual_tid}, {"tournament_id": str(actual_tid)}],
                    "payment_status": {"$in": ["PAID", "FREE"]}
                })
                tourn_doc = await db.tournaments.find_one({"$or": [{"_id": actual_tid}, {"id": actual_tid}, {"slug": actual_tid}]})
                if tourn_doc and reg_count >= tourn_doc.get("max_teams", 16):
                    await db.tournaments.update_one({"_id": tourn_doc["_id"]}, {"$set": {"status": "REGISTRATION_CLOSED"}})

    # If it was a game entry payment, set team to PAID
    if payment.get("payment_type") == "GAME_ENTRY" and payment.get("team_id"):
        await db.teams.update_one(
            {"$or": [{"_id": payment["team_id"]}, {"id": payment["team_id"]}]},
            {"$set": {"payment_status": "PAID", "payment_id": payment_id}}
        )

    # If it was a wallet topup, credit the wallet
    if payment.get("payment_type") == "WALLET_TOPUP":
        from app.services.wallet_service import credit_wallet_funds
        await credit_wallet_funds(
            db=db,
            user_id=payment["user_id"],
            amount_paise=payment["amount_paise"],
            txn_type="WALLET_TOPUP",
            reference_id=payment_id,
            description=f"UPI Top-up (Ref: {payment.get('utr', 'Manual Approval')})",
            admin_id=admin_user.id,
        )

    # Notify the user that their payment was approved
    from app.services.notification import create_notification
    if payment.get("payment_type") == "TOURNAMENT_ENTRY":
        actual_tid = payment.get("tournament_id")
        tourn_doc = await db.tournaments.find_one({"$or": [{"_id": actual_tid}, {"id": actual_tid}, {"slug": actual_tid}]}) if actual_tid else None
        tourn_title = (tourn_doc.get("title") if tourn_doc else payment.get("game_name")) or "Tournament"
        tourn_slug = (tourn_doc.get("slug") if tourn_doc else actual_tid) or ""
        await create_notification(
            db=db,
            user_id=payment["user_id"],
            title="Tournament Registration Confirmed",
            message=f'Your payment has been approved and your squad is officially registered for "{tourn_title}". Review your tournament schedule and rules, and join the official WhatsApp group for updates.',
            notification_type="REGISTRATION",
            category="REGISTRATION",
            link=f"/tournaments/{tourn_slug}",
        )
    else:
        await create_notification(
            db=db,
            user_id=payment["user_id"],
            title="Payment Approved",
            message=f"Your payment of ₹{payment.get('amount_inr', 0)} for {payment.get('payment_type', 'Platform Service')} has been verified and approved.",
            notification_type="WALLET",
            category="WALLET",
            link="/dashboard",
        )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="APPROVE_PAYMENT",
        target=f"Payment {payment_id} (₹{payment.get('amount_inr')})",
        details=f"Approved and archived by {admin_user.username} (UTR: {payment.get('utr')})",
    )

    return {"status": "success", "message": "Payment verified, approved, and archived successfully.", "payment_id": payment_id, "new_status": "APPROVED"}


@router.post("/payments/{payment_id}/reject")
async def reject_payment(
    payment_id: str,
    payload: PaymentVerifyActionPayload,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from bson import ObjectId
    from datetime import datetime, timezone

    p_query = {"$or": [{"_id": payment_id}, {"id": payment_id}]}
    if ObjectId.is_valid(payment_id):
        p_query["$or"].append({"_id": ObjectId(payment_id)})

    payment = await db.game_payments.find_one(p_query)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment record not found")

    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    # Idempotency: Prevent duplicate rejection and ensure archived
    if payment.get("status") == "REJECTED":
        if not payment.get("is_archived"):
            await db.game_payments.update_one(
                {"_id": payment["_id"]},
                {
                    "$set": {
                        "is_archived": True,
                        "archived_at": now_iso,
                        "archived_by": admin_user.username,
                        "archive_reason": f"Payment rejected by {admin_user.username}",
                    }
                }
            )
        return {"status": "success", "message": "Payment is already rejected.", "payment_id": payment_id, "new_status": "REJECTED"}

    # Business rule: Cannot reject already approved payments
    if payment.get("status") == "APPROVED":
        raise HTTPException(
            status_code=400,
            detail="Cannot reject an already approved payment. Please issue an administrative adjustment if required.",
        )

    reason = payload.rejection_reason or "Invalid transaction details"

    # Atomic transition to REJECTED and auto-archive
    update_res = await db.game_payments.update_one(
        {"_id": payment["_id"], "status": {"$ne": "REJECTED"}},
        {
            "$set": {
                "status": "REJECTED",
                "rejection_reason": reason,
                "verified_by": admin_user.username,
                "verified_at": now_utc,
                "rejected_by": admin_user.username,
                "rejected_at": now_utc,
                "is_archived": True,
                "archived_at": now_iso,
                "archived_by": admin_user.username,
                "archive_reason": f"Payment rejected by {admin_user.username}: {reason}",
            }
        }
    )

    if update_res.modified_count == 0 and payment.get("status") == "REJECTED":
        return {"status": "success", "message": "Payment is already rejected.", "payment_id": payment_id, "new_status": "REJECTED"}

    # Tournament entry registration state transition
    if payment.get("payment_type") == "TOURNAMENT_ENTRY":
        reg_id = payment.get("registration_id")
        reg_or = []
        if reg_id:
            reg_or.extend([{"_id": reg_id}, {"id": reg_id}])
            if ObjectId.is_valid(str(reg_id)):
                reg_or.append({"_id": ObjectId(str(reg_id))})
        reg_or.append({"payment_id": payment_id})
        await db.tournament_registrations.update_one(
            {"$or": reg_or},
            {"$set": {"payment_status": "REJECTED"}}
        )

        actual_tid = payment.get("tournament_id")
        if actual_tid:
            active_count = await db.tournament_registrations.count_documents({
                "$or": [{"tournament_id": actual_tid}, {"tournament_id": str(actual_tid)}],
                "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
            })
            tourn_doc = await db.tournaments.find_one({"$or": [{"_id": actual_tid}, {"id": actual_tid}, {"slug": actual_tid}]})
            if tourn_doc and tourn_doc.get("status") == "REGISTRATION_CLOSED" and active_count < tourn_doc.get("max_teams", 16):
                await db.tournaments.update_one({"_id": tourn_doc["_id"]}, {"$set": {"status": "REGISTRATION_OPEN"}})

    # Game entry team state transition
    if payment.get("payment_type") == "GAME_ENTRY" and payment.get("team_id"):
        t_id = payment.get("team_id")
        team_or = [{"_id": t_id}, {"id": t_id}]
        if ObjectId.is_valid(str(t_id)):
            team_or.append({"_id": ObjectId(str(t_id))})
        await db.teams.update_one(
            {"$or": team_or},
            {"$set": {"payment_status": "PAYMENT_REJECTED"}}
        )

    # Asynchronously trigger user notification and audit log
    try:
        from app.services.notification import create_notification
        await create_notification(
            db=db,
            user_id=payment["user_id"],
            title="Payment Rejected",
            message=f"Your payment of ₹{payment.get('amount_inr', 0)} was rejected: {reason}",
            notification_type="WALLET",
            category="WALLET",
            link="/dashboard",
        )
    except Exception as e:
        import logging
        logging.getLogger("ametheus.admin").warning(f"Failed to create rejection notification: {e}")

    try:
        await record_audit_log(
            db=db,
            operator=admin_user.username,
            role="SUPER_ADMIN",
            action="REJECT_PAYMENT",
            target=f"Payment {payment_id}",
            details=f"Rejected and archived by {admin_user.username}: {reason}",
        )
    except Exception as e:
        import logging
        logging.getLogger("ametheus.admin").warning(f"Failed to record audit log for payment rejection: {e}")

    return {
        "status": "success",
        "message": "Payment rejected and archived successfully.",
        "payment_id": payment_id,
        "new_status": "REJECTED",
    }

@router.get("/registration-revenue-analytics")
async def get_registration_revenue_analytics(
    time_range: Optional[str] = "all",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Tournament Registration & Revenue Analytics:
    - Confirmed registrations vs reserved seats vs available capacity.
    - Verified registration revenue vs pending payment totals.
    - Separate wallet top-up metrics (no double counting).
    - Tournament-wise registration performance.
    """
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    date_filter = {}
    if time_range == "this_week":
        start_dt = now - timedelta(days=now.weekday())
        date_filter = {"$gte": start_dt.replace(hour=0, minute=0, second=0, microsecond=0)}
    elif time_range == "last_week":
        end_dt = now - timedelta(days=now.weekday())
        start_dt = end_dt - timedelta(days=7)
        date_filter = {
            "$gte": start_dt.replace(hour=0, minute=0, second=0, microsecond=0),
            "$lt": end_dt.replace(hour=0, minute=0, second=0, microsecond=0),
        }
    elif time_range == "this_month":
        start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        date_filter = {"$gte": start_dt}
    elif time_range == "last_month":
        first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_month_end = first_this_month
        prev_month = (first_this_month - timedelta(days=1)).replace(day=1)
        date_filter = {"$gte": prev_month, "$lt": last_month_end}
    elif time_range == "custom" and (start_date or end_date):
        if start_date:
            try:
                date_filter["$gte"] = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
            except Exception:
                pass
        if end_date:
            try:
                date_filter["$lte"] = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            except Exception:
                pass

    reg_match = {"status": {"$nin": ["DELETED"]}}
    if date_filter:
        reg_match["created_at"] = date_filter

    confirmed_regs = await db.tournament_registrations.count_documents({
        **reg_match,
        "payment_status": {"$in": ["PAID", "FREE"]},
    })
    pending_regs = await db.tournament_registrations.count_documents({
        **reg_match,
        "payment_status": {"$in": ["PENDING", "PENDING_VERIFICATION"]},
    })
    rejected_regs = await db.tournament_registrations.count_documents({
        **reg_match,
        "payment_status": {"$in": ["REJECTED", "CANCELLED", "REFUNDED"]},
    })

    # Approved direct tournament entry revenue
    pay_match_approved = {
        "payment_type": "TOURNAMENT_ENTRY",
        "status": "APPROVED",
        "is_dummy": {"$ne": True},
    }
    if date_filter:
        pay_match_approved["created_at"] = date_filter
    approved_rev_agg = await db.game_payments.aggregate([
        {"$match": pay_match_approved},
        {"$group": {"_id": None, "total_inr": {"$sum": "$amount_inr"}}},
    ]).to_list(1)
    approved_revenue_inr = approved_rev_agg[0]["total_inr"] if approved_rev_agg else 0

    # Pending payment amounts
    pay_match_pending = {
        "payment_type": "TOURNAMENT_ENTRY",
        "status": {"$in": ["PENDING", "PENDING_VERIFICATION", "INITIATED"]},
        "is_dummy": {"$ne": True},
    }
    if date_filter:
        pay_match_pending["created_at"] = date_filter
    pending_rev_agg = await db.game_payments.aggregate([
        {"$match": pay_match_pending},
        {"$group": {"_id": None, "total_inr": {"$sum": "$amount_inr"}}},
    ]).to_list(1)
    pending_payment_inr = pending_rev_agg[0]["total_inr"] if pending_rev_agg else 0

    # Separate wallet top-up volume
    wallet_match = {
        "payment_type": "WALLET_TOPUP",
        "status": "APPROVED",
        "is_dummy": {"$ne": True},
    }
    if date_filter:
        wallet_match["created_at"] = date_filter
    wallet_agg = await db.game_payments.aggregate([
        {"$match": wallet_match},
        {"$group": {"_id": None, "total_inr": {"$sum": "$amount_inr"}}},
    ]).to_list(1)
    wallet_topup_inr = wallet_agg[0]["total_inr"] if wallet_agg else 0

    # Tournament-wise breakdown
    tournaments = await db.tournaments.find({
        "status": {"$nin": ["DELETED", "HIDDEN", "DRAFT"]},
        "is_dummy": {"$ne": True},
    }).sort("created_at", -1).to_list(100)

    tournament_breakdown = []
    total_capacity = 0
    total_available_seats = 0

    for t in tournaments:
        t_id = str(t.get("_id", t.get("id")))
        t_slug = t.get("slug", t_id)
        cap = int(t.get("max_teams", 16))
        total_capacity += cap

        t_conf = await db.tournament_registrations.count_documents({
            "$or": [{"tournament_id": t_id}, {"tournament_id": t_slug}],
            "payment_status": {"$in": ["PAID", "FREE"]},
        })
        t_pend = await db.tournament_registrations.count_documents({
            "$or": [{"tournament_id": t_id}, {"tournament_id": t_slug}],
            "payment_status": {"$in": ["PENDING", "PENDING_VERIFICATION"]},
        })
        t_avail = max(0, cap - t_conf - t_pend)
        total_available_seats += t_avail

        t_rev_agg = await db.game_payments.aggregate([
            {"$match": {
                "tournament_id": {"$in": [t_id, t_slug]},
                "payment_type": "TOURNAMENT_ENTRY",
                "status": "APPROVED",
            }},
            {"$group": {"_id": None, "total_inr": {"$sum": "$amount_inr"}}},
        ]).to_list(1)
        t_rev = t_rev_agg[0]["total_inr"] if t_rev_agg else 0

        tournament_breakdown.append({
            "id": t_id,
            "title": t.get("title", "Tournament"),
            "slug": t_slug,
            "status": t.get("status", "REGISTRATION_OPEN"),
            "entry_fee_inr": t.get("entry_fee_inr", 0),
            "capacity": cap,
            "confirmed_registrations": t_conf,
            "pending_reservations": t_pend,
            "available_seats": t_avail,
            "approved_revenue_inr": t_rev,
        })

    return {
        "confirmed_registrations": confirmed_regs,
        "pending_reservations": pending_regs,
        "available_seats": total_available_seats,
        "total_capacity": total_capacity,
        "rejected_cancelled_registrations": rejected_regs,
        "approved_tournament_revenue_inr": approved_revenue_inr,
        "pending_payment_amount_inr": pending_payment_inr,
        "wallet_topup_volume_inr": wallet_topup_inr,
        "time_range": time_range,
        "tournaments": tournament_breakdown,
    }


@router.get("/payouts")
async def list_admin_payouts(
    status_filter: Optional[str] = None,
    is_archived: bool = False,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = {"is_dummy": {"$ne": True}}
    if is_archived:
        query["is_archived"] = True
    else:
        query["is_archived"] = {"$ne": True}

    if status_filter and status_filter.upper() != "ALL":
        query["status"] = status_filter.upper()

    cursor = db.payouts.find(query).sort("created_at", -1)
    docs = await cursor.to_list(length=200)
    for d in docs:
        d["id"] = str(d.get("_id", d.get("id")))
        d.pop("_id", None)
        d["amount_inr"] = d.get("amount_paise", 0) / 100
        d["is_archived"] = bool(d.get("is_archived", False))
        if d.get("archived_at") and hasattr(d["archived_at"], "isoformat"):
            d["archived_at"] = d["archived_at"].isoformat()
    return docs

@router.post("/payouts/create-winning")
async def create_winning_payout(
    payload: CreateWinningPayoutPayload,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    import uuid
    game = await db.games.find_one({"$or": [{"_id": payload.game_id}, {"id": payload.game_id}, {"slug": payload.game_id}]})
    game_name = game.get("name") if game else "Game"

    user = await db.users.find_one({"$or": [{"_id": payload.user_id}, {"id": payload.user_id}]})
    user_name = user.get("display_name") or user.get("username") if user else "Gamer"

    amount_paise = int(round(payload.amount_inr * 100))

    from app.models.payment import Payout
    from app.services.wallet_service import credit_wallet_funds
    from datetime import datetime, timezone

    if payload.destination.upper() == "WALLET":
        # Instant credit to wallet
        await credit_wallet_funds(
            db=db,
            user_id=payload.user_id,
            amount_paise=amount_paise,
            txn_type="GAME_WINNING",
            reference_id=f"win_{uuid.uuid4().hex[:8]}",
            description=f"Prize for {payload.rank_or_placement} in {game_name}",
            admin_id=admin_user.id,
        )
        payout = Payout(
            payout_type="GAME_WINNING",
            user_id=payload.user_id,
            user_name=user_name,
            amount_paise=amount_paise,
            destination="WALLET",
            game_id=payload.game_id,
            game_name=game_name,
            rank_or_placement=payload.rank_or_placement,
            status="PAID",
            admin_id=admin_user.id,
            paid_at=datetime.now(timezone.utc),
        )
        await db.payouts.insert_one(payout.to_mongo())
        return {"status": "success", "message": f"Prize of ₹{payload.amount_inr:.2f} credited to user wallet."}

    # Destination is UPI
    payout = Payout(
        payout_type="GAME_WINNING",
        user_id=payload.user_id,
        user_name=user_name,
        amount_paise=amount_paise,
        destination="UPI",
        upi_id=payload.upi_id,
        game_id=payload.game_id,
        game_name=game_name,
        rank_or_placement=payload.rank_or_placement,
        status="PENDING",
    )
    await db.payouts.insert_one(payout.to_mongo())
    return {"status": "success", "message": "UPI winning payout created and waiting for disbursement."}

@router.post("/payouts/{payout_id}/complete")
async def complete_payout(
    payout_id: str,
    payload: PayoutCompletePayload,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    payout = await db.payouts.find_one({"$or": [{"_id": payout_id}, {"id": payout_id}]})
    if not payout:
        raise HTTPException(status_code=404, detail="Payout record not found")

    if payout.get("status") == "PAID":
        return {"status": "success", "message": "Payout was already marked as PAID"}

    from datetime import datetime, timezone
    from app.services.wallet_service import finalize_reserved_deduction

    now = datetime.now(timezone.utc)
    out_utr = payload.outgoing_utr.strip()

    # Atomically transition status from PENDING to PAID
    res = await db.payouts.update_one(
        {"_id": payout["_id"], "status": "PENDING"},
        {
            "$set": {
                "status": "PAID",
                "outgoing_utr": out_utr,
                "admin_id": admin_user.id,
                "paid_at": now,
            }
        }
    )
    if res.modified_count == 0:
        return {"status": "success", "message": "Payout was already processed."}

    # If it was a withdrawal, permanently deduct the reserved balance
    if payout.get("payout_type") == "WALLET_WITHDRAWAL":
        await finalize_reserved_deduction(
            db=db,
            user_id=payout["user_id"],
            amount_paise=payout["amount_paise"],
            reference_id=payout_id,
            admin_id=admin_user.id,
            description=f"Withdrawal to {payout.get('upi_id')} (UTR: {out_utr})",
        )

    from app.services.notification import create_notification
    amt_inr = payout.get("amount_paise", 0) / 100
    await create_notification(
        db=db,
        user_id=payout["user_id"],
        title="Payout Processed",
        message=f"Your payout of ₹{amt_inr:.2f} has been completed via UPI. Reference UTR: {out_utr}.",
        notification_type="WALLET",
        category="WALLET",
        link="/dashboard",
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="COMPLETE_PAYOUT",
        target=f"Payout {payout_id}",
        details=f"Marked PAID with Outgoing UTR: {out_utr}",
    )

    return {"status": "success", "message": "Payout completed and marked as PAID."}

@router.post("/payouts/{payout_id}/reject")
async def reject_payout(
    payout_id: str,
    payload: PayoutRejectPayload,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    payout = await db.payouts.find_one({"$or": [{"_id": payout_id}, {"id": payout_id}]})
    if not payout:
        raise HTTPException(status_code=404, detail="Payout record not found")

    from app.services.wallet_service import release_reserved_funds

    # Atomically transition status from PENDING to REJECTED
    res = await db.payouts.update_one(
        {"_id": payout["_id"], "status": "PENDING"},
        {
            "$set": {
                "status": "REJECTED",
                "rejection_reason": payload.rejection_reason,
                "admin_id": admin_user.id,
            }
        }
    )
    if res.modified_count == 0:
        return {"status": "success", "message": "Payout was already processed."}

    # Release reserved funds back to user's wallet
    if payout.get("payout_type") == "WALLET_WITHDRAWAL":
        await release_reserved_funds(
            db=db,
            user_id=payout["user_id"],
            amount_paise=payout["amount_paise"],
        )

    from app.services.notification import create_notification
    amt_inr = payout.get("amount_paise", 0) / 100
    await create_notification(
        db=db,
        user_id=payout["user_id"],
        title="Payout Request Rejected",
        message=f"Your withdrawal request of ₹{amt_inr:.2f} was rejected: {payload.rejection_reason}. Reserved funds have been returned to your wallet.",
        notification_type="WALLET",
        category="WALLET",
        link="/dashboard",
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="REJECT_PAYOUT",
        target=f"Payout {payout_id}",
        details=f"Reason: {payload.rejection_reason}",
    )

    return {"status": "success", "message": "Payout rejected and reserved funds returned to available balance."}

# ----------------- ADMIN LIVE MATCHES & PAIRING SYSTEM -----------------

@router.get("/matches/live-dashboard")
async def get_live_matches_dashboard(
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns aggregated dashboard data for the 4-tab Live Matches workflow:
    1. Tournaments ready for match creation (with registered teams & pairing status)
    2. Scheduled matches
    3. Live Now matches
    4. Completed matches
    """
    from datetime import datetime

    tournaments = await db.tournaments.find({
        "status": {"$in": ["REGISTRATION_CLOSED", "LIVE", "ONGOING", "IN_PROGRESS"]}
    }).to_list(length=100)
    ready_tournaments = []

    for t in tournaments:
        tid = str(t.get("_id", t.get("id")))
        tslug = t.get("slug", tid)
        reg_cursor = db.tournament_registrations.find({
            "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
            "payment_status": {"$in": ["PAID", "FREE"]}
        })
        registrations = await reg_cursor.to_list(length=100)

        tourn_matches = await db.matches.find({
            "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
            "status": {"$in": ["SCHEDULED", "CHECK_IN_OPEN", "LIVE", "PENDING_VERIFICATION", "DISPUTED"]}
        }).to_list(length=100)

        paired_team_ids = set()
        for m in tourn_matches:
            if m.get("team_a_id"):
                paired_team_ids.add(str(m["team_a_id"]))
            if m.get("team_b_id"):
                paired_team_ids.add(str(m["team_b_id"]))

        registered_teams_list = []
        for reg in registrations:
            team_id = reg.get("team_id")
            player_id = reg.get("player_id")
            if team_id:
                team_doc = await db.teams.find_one({"$or": [{"_id": team_id}, {"id": team_id}]})
                if team_doc:
                    t_id_str = str(team_doc.get("_id", team_doc.get("id")))
                    registered_teams_list.append({
                        "id": t_id_str,
                        "name": team_doc.get("name", "Team"),
                        "tag": team_doc.get("tag", "TEAM"),
                        "captain_username": team_doc.get("captain_username", "Captain"),
                        "is_paired": t_id_str in paired_team_ids,
                    })
            elif player_id:
                user_doc = await db.users.find_one({"$or": [{"_id": player_id}, {"id": player_id}]})
                if user_doc:
                    u_id_str = str(user_doc.get("_id", user_doc.get("id")))
                    registered_teams_list.append({
                        "id": u_id_str,
                        "name": user_doc.get("username", "Contender"),
                        "tag": "SOLO",
                        "captain_username": user_doc.get("username", "Contender"),
                        "is_paired": u_id_str in paired_team_ids,
                    })

        ready_tournaments.append({
            "id": tid,
            "slug": tslug,
            "title": t.get("title", "Tournament"),
            "status": "REGISTRATION_CLOSED",
            "max_teams": t.get("max_teams", 16),
            "match_format": t.get("match_format") or t.get("tournament_type") or "Single Elimination",
            "series_format": t.get("series_format") or "BO1",
            "stage_formats": t.get("stage_formats") or {},
            "teams": registered_teams_list,
            "unpaired_count": len([x for x in registered_teams_list if not x["is_paired"]]),
            "paired_count": len([x for x in registered_teams_list if x["is_paired"]]),
        })

    async def enrich_match(m: dict) -> Optional[dict]:
        mid = str(m.get("_id", m.get("id")))
        tourn_id = m.get("tournament_id")
        tourn = await db.tournaments.find_one({"$or": [{"_id": tourn_id}, {"id": tourn_id}, {"slug": tourn_id}]}) if tourn_id else None
        if not tourn:
            # Omit orphaned matches belonging to deleted tournaments
            return None

        team_a_id = m.get("team_a_id")
        team_b_id = m.get("team_b_id")

        team_a = await db.teams.find_one({"$or": [{"_id": team_a_id}, {"id": team_a_id}]}) if team_a_id else None
        if not team_a and team_a_id:
            user_a = await db.users.find_one({"$or": [{"_id": team_a_id}, {"id": team_a_id}]})
            if user_a:
                team_a = {"name": user_a.get("username") or "Player A", "tag": "SOLO"}
        if not team_a and m.get("team_a_placeholder"):
            team_a = {"name": m.get("team_a_placeholder"), "tag": "TBD"}

        team_b = await db.teams.find_one({"$or": [{"_id": team_b_id}, {"id": team_b_id}]}) if team_b_id else None
        if not team_b and team_b_id:
            user_b = await db.users.find_one({"$or": [{"_id": team_b_id}, {"id": team_b_id}]})
            if user_b:
                team_b = {"name": user_b.get("username") or "Player B", "tag": "SOLO"}
        if not team_b and m.get("team_b_placeholder"):
            team_b = {"name": m.get("team_b_placeholder"), "tag": "TBD"}

        scheduled_at = m.get("scheduled_at")
        scheduled_at_iso = scheduled_at.isoformat() if isinstance(scheduled_at, datetime) else scheduled_at

        return {
            "id": mid,
            "tournament_id": tourn_id,
            "tournament_title": tourn.get("title") if tourn else "Tournament",
            "round_name": m.get("round_name") or m.get("stage", "Round 1"),
            "round_number": m.get("round_number", 1),
            "match_number": m.get("match_number", 1),
            "stage": m.get("stage", "Round 1"),
            "match_type": m.get("match_type", "MATCH"),
            "format": m.get("series_format") or m.get("format") or "BO1",
            "series_format": m.get("series_format") or m.get("format") or "BO1",
            "order": m.get("order", 0),
            "status": m.get("status", "SCHEDULED"),
            "scheduled_time": m.get("scheduled_time", "TBD"),
            "scheduled_at": scheduled_at_iso,
            "live_link": m.get("youtube_live_url") or m.get("live_link"),
            "youtube_live_url": m.get("youtube_live_url") or m.get("live_link"),
            "team_a": {"id": team_a_id, "name": team_a["name"], "tag": team_a.get("tag", "TEAM"), "score": m.get("score_a", 0)} if team_a else None,
            "team_b": {"id": team_b_id, "name": team_b["name"], "tag": team_b.get("tag", "TEAM"), "score": m.get("score_b", 0)} if team_b else None,
            "team_a_placeholder": m.get("team_a_placeholder"),
            "team_b_placeholder": m.get("team_b_placeholder"),
            "winner_team_id": m.get("winner_team_id"),
            "check_in_status": m.get("check_in_status") or {"team_a": False, "team_b": False},
            "games": m.get("games", []),
            "score_a": m.get("score_a", 0),
            "score_b": m.get("score_b", 0),
        }

    sched_cursor = db.matches.find({"status": {"$in": ["SCHEDULED", "CHECK_IN_OPEN"]}}).sort("created_at", -1)
    sched_raw = await sched_cursor.to_list(length=100)
    scheduled_matches = [res for res in [await enrich_match(m) for m in sched_raw] if res is not None]

    live_cursor = db.matches.find({"status": {"$in": ["LIVE", "PENDING_VERIFICATION", "DISPUTED"]}}).sort("created_at", -1)
    live_raw = await live_cursor.to_list(length=100)
    live_matches = [res for res in [await enrich_match(m) for m in live_raw] if res is not None]

    comp_cursor = db.matches.find({"status": "COMPLETED"}).sort("updated_at", -1).limit(50)
    comp_raw = await comp_cursor.to_list(length=50)
    completed_matches = [res for res in [await enrich_match(m) for m in comp_raw] if res is not None]

    return {
        "tournaments_ready": ready_tournaments,
        "ready_for_match_creation": ready_tournaments,
        "scheduled_matches": scheduled_matches,
        "scheduled": scheduled_matches,
        "live_matches": live_matches,
        "live_now": live_matches,
        "completed_matches": completed_matches,
        "completed": completed_matches,
    }

@router.post("/matches/pair")
async def pair_match_teams(
    payload: AdminMatchPairRequest,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    import uuid
    from datetime import datetime, timezone
    from app.services.notification import create_notification

    if payload.team_a_id == payload.team_b_id:
        raise HTTPException(status_code=400, detail="Cannot pair a team against itself.")

    tourn = await db.tournaments.find_one({
        "$or": [{"_id": payload.tournament_id}, {"id": payload.tournament_id}, {"slug": payload.tournament_id}]
    })
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    t_status = str(tourn.get("status") or "").upper()
    t_reg_status = str(tourn.get("registration_status") or "").upper()
    is_closed = (
        t_reg_status == "CLOSED"
        or t_status == "REGISTRATION_CLOSED"
        or bool(tourn.get("is_registration_closed"))
        or t_status in ["COMPLETED", "IN_PROGRESS"]
    )
    if not is_closed:
        raise HTTPException(
            status_code=400,
            detail="Match creation is only allowed for tournaments whose registration is CLOSED. Please close registration before pairing matches.",
        )

    tid = str(tourn.get("_id", tourn.get("id")))

    team_a = await db.teams.find_one({"$or": [{"_id": payload.team_a_id}, {"id": payload.team_a_id}]})
    if not team_a:
        team_a = await db.users.find_one({"$or": [{"_id": payload.team_a_id}, {"id": payload.team_a_id}]})
    if not team_a:
        raise HTTPException(status_code=404, detail="Team/Player A not found")

    team_b = await db.teams.find_one({"$or": [{"_id": payload.team_b_id}, {"id": payload.team_b_id}]})
    if not team_b:
        team_b = await db.users.find_one({"$or": [{"_id": payload.team_b_id}, {"id": payload.team_b_id}]})
    if not team_b:
        raise HTTPException(status_code=404, detail="Team/Player B not found")

    name_a = team_a.get("name") or team_a.get("username") or "Team A"
    name_b = team_b.get("name") or team_b.get("username") or "Team B"

    scheduled_at_dt = None
    scheduled_time_str = "TBD"
    if payload.scheduled_at:
        try:
            scheduled_at_dt = datetime.fromisoformat(payload.scheduled_at.replace("Z", "+00:00"))
            scheduled_time_str = scheduled_at_dt.strftime("%b %d, %I:%M %p UTC")
        except Exception:
            scheduled_time_str = payload.scheduled_at

    fmt = payload.match_format or tourn.get("match_format") or "Best of 1"
    if fmt.lower() in ["bo1", "best of 1"]:
        fmt = "Best of 1"
    elif fmt.lower() in ["bo3", "best of 3"]:
        fmt = "Best of 3"
    elif fmt.lower() in ["bo5", "best of 5"]:
        fmt = "Best of 5"

    match = Match(
        tournament_id=tid,
        stage=payload.stage,
        format=fmt,
        scheduled_at=scheduled_at_dt,
        scheduled_time=scheduled_time_str,
        live_link=payload.live_link,
        status="SCHEDULED",
        team_a_id=payload.team_a_id,
        team_b_id=payload.team_b_id,
        server_region=tourn.get("server_region", "AWS Mumbai (ap-south-1)"),
        lobby_code=f"AMT-{uuid.uuid4().hex[:6].upper()}",
        lobby_password=f"PASS-{uuid.uuid4().hex[:6].upper()}",
        check_in_status={"team_a": False, "team_b": False},
    )
    await db.matches.insert_one(match.to_mongo())

    notif_users = []
    if "captain_id" in team_a:
        notif_users.append(team_a["captain_id"])
        for m in team_a.get("members", []):
            uid = m.get("user_id") if isinstance(m, dict) else getattr(m, "user_id", None)
            if uid:
                notif_users.append(uid)
    else:
        notif_users.append(payload.team_a_id)

    if "captain_id" in team_b:
        notif_users.append(team_b["captain_id"])
        for m in team_b.get("members", []):
            uid = m.get("user_id") if isinstance(m, dict) else getattr(m, "user_id", None)
            if uid:
                notif_users.append(uid)
    else:
        notif_users.append(payload.team_b_id)

    for uid in set(notif_users):
        await create_notification(
            db=db,
            user_id=uid,
            title="Match Scheduled",
            message=f"Your match ({name_a} vs {name_b}) in {payload.stage} of {tourn.get('title')} has been scheduled for {scheduled_time_str}.",
            notification_type="MATCH",
            link="/matches",
        )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="PAIR_MATCH_TEAMS",
        target=f"Match {match.id} ({name_a} vs {name_b})",
        details=f"Stage: {payload.stage} | Format: {fmt} | Time: {scheduled_time_str}",
    )

    return {"status": "success", "message": f"Successfully paired {name_a} vs {name_b}!", "match_id": match.id}

@router.patch("/matches/{id}/schedule")
async def update_match_schedule(
    id: str,
    payload: AdminMatchScheduleUpdate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from datetime import datetime

    match = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    update_fields = {}
    if payload.scheduled_at:
        try:
            dt = datetime.fromisoformat(payload.scheduled_at.replace("Z", "+00:00"))
            update_fields["scheduled_at"] = dt
            update_fields["scheduled_time"] = dt.strftime("%b %d, %I:%M %p UTC")
        except Exception:
            pass

    if payload.scheduled_time and "scheduled_time" not in update_fields:
        update_fields["scheduled_time"] = payload.scheduled_time

    if payload.live_link is not None:
        update_fields["live_link"] = payload.live_link.strip() if payload.live_link else None

    if payload.match_format:
        fmt = payload.match_format
        if fmt.lower() in ["bo1", "best of 1"]:
            fmt = "Best of 1"
        elif fmt.lower() in ["bo3", "best of 3"]:
            fmt = "Best of 3"
        elif fmt.lower() in ["bo5", "best of 5"]:
            fmt = "Best of 5"
        update_fields["format"] = fmt

    if payload.stage:
        update_fields["stage"] = payload.stage

    if update_fields:
        await db.matches.update_one({"_id": match["_id"]}, {"$set": update_fields})

        # Notify affected rosters if time was changed
        if "scheduled_time" in update_fields or "scheduled_at" in update_fields:
            from app.services.notification import create_notification
            notify_users = []
            for tid in [match.get("team_a_id"), match.get("team_b_id")]:
                if not tid:
                    continue
                team_doc = await db.teams.find_one({"$or": [{"_id": tid}, {"id": tid}]})
                if team_doc:
                    if team_doc.get("captain_id"):
                        notify_users.append(team_doc["captain_id"])
                    for mem in team_doc.get("members", []):
                        uid = mem.get("user_id") if isinstance(mem, dict) else getattr(mem, "user_id", None)
                        if uid:
                            notify_users.append(uid)
                else:
                    notify_users.append(tid)

            for uid in set(notify_users):
                await create_notification(
                    db=db,
                    user_id=uid,
                    title="Match Rescheduled",
                    message=f"Your match in {update_fields.get('stage', match.get('stage', 'the tournament'))} has been rescheduled to {update_fields.get('scheduled_time', 'a new time')}.",
                    notification_type="MATCH",
                    link="/matches",
                )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="UPDATE_MATCH_SCHEDULE",
        target=f"Match {id}",
        details=str(update_fields),
    )

    return {"status": "success", "message": "Match schedule updated successfully", "updated": update_fields}

@router.patch("/matches/{id}/status")
async def update_match_status(
    id: str,
    payload: AdminMatchStatusUpdate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from app.core.ws import ws_manager
    from app.models.base import get_utc_now
    from bson import ObjectId

    m_query = {"$or": [{"_id": id}, {"id": id}]}
    if ObjectId.is_valid(id):
        m_query["$or"].append({"_id": ObjectId(id)})

    match = await db.matches.find_one(m_query)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    valid_statuses = [
        "DRAFT", "SCHEDULED", "CHECK_IN_OPEN", "LIVE",
        "ENDED", "RESULTS_SAVED", "RESULTS_PENDING",
        "FINALIZED", "COMPLETED", "CANCELLED"
    ]
    stat_upper = payload.status.upper()
    if stat_upper not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid match status: {payload.status}")

    current_status = str(match.get("status", "SCHEDULED")).upper()

    # Prevent restarting completed or finalized matches
    if current_status in ["COMPLETED", "FINALIZED"] and stat_upper in ["LIVE", "SCHEDULED", "CHECK_IN_OPEN"]:
        raise HTTPException(status_code=400, detail="Cannot restart a completed or finalized match.")

    # Idempotent: if already LIVE and setting to LIVE
    if stat_upper == "LIVE" and current_status == "LIVE":
        return {"status": "success", "message": "Match is already LIVE", "status": "LIVE"}

    update_dict: Dict[str, Any] = {"status": stat_upper, "updated_at": get_utc_now()}
    if stat_upper == "LIVE":
        update_dict["credentials_released"] = True
        update_dict["credentials_released_at"] = get_utc_now()
        update_dict["credentials_released_by"] = admin_user.username
        update_dict["join_info_enabled"] = True

    await db.matches.update_one({"_id": match["_id"]}, {"$set": update_dict})

    try:
        from app.services.live_match_service import sync_live_match_for_tournament
        tid = str(match.get("tournament_id"))
        if tid:
            await sync_live_match_for_tournament(tid, db)
    except Exception:
        pass

    await ws_manager.broadcast({
        "type": "MATCH_STATUS_CHANGED",
        "match_id": str(match.get("_id", match.get("id"))),
        "status": stat_upper,
    })

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="UPDATE_MATCH_STATUS",
        target=f"Match {id}",
        details=f"Status set to {stat_upper}",
    )

    return {"status": "success", "message": f"Match status updated to {stat_upper}", "status_code": stat_upper}

@router.post("/matches/{id}/score")
async def update_match_score_admin(
    id: str,
    payload: AdminMatchScoreUpdate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from app.services.bracket_engine import advance_bracket_match
    from app.core.ws import ws_manager
    from app.models.base import get_utc_now
    from bson import ObjectId

    m_query = {"$or": [{"_id": id}, {"id": id}]}
    if ObjectId.is_valid(id):
        m_query["$or"].append({"_id": ObjectId(id)})

    match_doc = await db.matches.find_one(m_query)
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")

    current_status = str(match_doc.get("status", "SCHEDULED")).upper()
    if current_status in ["SCHEDULED", "DRAFT"]:
        raise HTTPException(
            status_code=400,
            detail="Cannot enter or submit match scores: match must be ended or live first."
        )

    match = Match.from_mongo(match_doc)
    winner_id = payload.winner_team_id
    if not winner_id and payload.complete_match:
        winner_id = match.team_a_id if payload.score_a >= payload.score_b else match.team_b_id

    update_doc: dict[str, Any] = {
        "score_a": payload.score_a,
        "score_b": payload.score_b,
        "updated_at": get_utc_now(),
    }
    if payload.complete_match:
        update_doc["status"] = "COMPLETED"
        update_doc["winner_team_id"] = winner_id
    else:
        update_doc["status"] = "RESULTS_SAVED"

    await db.matches.update_one({"_id": match_doc["_id"]}, {"$set": update_doc})

    if payload.complete_match and winner_id:
        try:
            await advance_bracket_match(
                tournament_id=match.tournament_id,
                completed_match=match,
                winner_team_id=winner_id,
                db=db,
            )
        except Exception:
            pass

    try:
        from app.services.live_match_service import sync_live_match_for_tournament
        await sync_live_match_for_tournament(str(match.tournament_id), db)
    except Exception:
        pass

    new_stat = "COMPLETED" if payload.complete_match else "RESULTS_SAVED"

    await ws_manager.broadcast({
        "type": "MATCH_SCORE_SUBMITTED",
        "match_id": match.id,
        "score_a": payload.score_a,
        "score_b": payload.score_b,
        "status": new_stat,
    })

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="ADMIN_UPDATE_MATCH_SCORE",
        target=f"Match {match.id} ({payload.score_a} - {payload.score_b})",
        details=f"Winner: {winner_id or 'Draft'} | Complete: {payload.complete_match} | Status: {new_stat}",
    )

    return {
        "status": "success",
        "message": f"Scores updated to {payload.score_a}-{payload.score_b}. Match {'completed' if payload.complete_match else 'results saved as draft'}.",
        "match_status": new_stat,
    }

# ----------------- TOURNAMENT & LIVE MATCHES REDESIGN ENDPOINTS -----------------

@router.post("/tournaments/{id}/close-registration")
async def close_tournament_registration_admin(
    id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Rule 6: Admin manually closes registration for a tournament.
    Tournament status becomes REGISTRATION_CLOSED and becomes available in Live Matches.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    await db.tournaments.update_one(
        {"_id": tourn["_id"]},
        {"$set": {
            "status": "REGISTRATION_CLOSED",
            "registration_status": "CLOSED",
            "is_registration_closed": True,
        }}
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="CLOSE_REGISTRATION",
        target=f"Tournament {tourn.get('title')}",
        details="Registration manually closed by admin. Tournament now available in Live Matches.",
    )

    return {
        "status": "success",
        "message": f'Tournament "{tourn.get("title")}" registration is now CLOSED. It is now active in Live Matches!',
    }

@router.post("/tournaments/{id}/reopen-registration")
async def reopen_tournament_registration_admin(
    id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Requirement 22: Admin reopens registration for a tournament.
    Tournament status becomes REGISTRATION_OPEN, allowing new contenders/teams to register.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    await db.tournaments.update_one(
        {"_id": tourn["_id"]},
        {"$set": {
            "status": "REGISTRATION_OPEN",
            "registration_status": "OPEN",
            "is_registration_closed": False,
        }}
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="REOPEN_REGISTRATION",
        target=f"Tournament {tourn.get('title')}",
        details="Registration reopened by admin. Contenders can register again.",
    )

    return {
        "status": "success",
        "message": f'Tournament "{tourn.get("title")}" registration has been REOPENED. Contenders can register again!',
    }

@router.post("/tournaments/{id}/start-live")
async def start_tournament_live_admin(
    id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Super Admin / Organizer transitions tournament directly to LIVE status.
    Updates scheduled matches with teams to LIVE.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    await db.tournaments.update_one(
        {"_id": tourn["_id"]},
        {"$set": {"status": "LIVE"}}
    )

    await db.matches.update_many(
        {
            "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
            "team_a_id": {"$ne": None},
            "team_b_id": {"$ne": None},
            "status": {"$in": ["SCHEDULED", "CHECK_IN_OPEN"]},
        },
        {"$set": {"status": "LIVE"}}
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="TOURNAMENT_SET_LIVE",
        target=f"Tournament {tourn.get('title')}",
        details="Tournament status transitioned to LIVE by admin.",
    )

    return {
        "status": "success",
        "message": f'Tournament "{tourn.get("title")}" is now LIVE! Live matches are in progress.',
    }

@router.post("/tournaments/{id}/complete")
async def complete_tournament_admin(
    id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Super Admin / Organizer transitions tournament to COMPLETED status.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    await db.tournaments.update_one(
        {"_id": tourn["_id"]},
        {"$set": {"status": "COMPLETED"}}
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="TOURNAMENT_COMPLETED",
        target=f"Tournament {tourn.get('title')}",
        details="Tournament status transitioned to COMPLETED by admin.",
    )

    return {
        "status": "success",
        "message": f'Tournament "{tourn.get("title")}" has been marked as COMPLETED.',
    }

@router.get("/tournaments/{id}/registered-rosters")
async def get_tournament_registered_rosters(
    id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Retrieve all verified registered teams and player rosters for the selected tournament.
    Queries using both string IDs, slugs, and ObjectId, filtering valid non-rejected registrations.
    Returns normalized teams and rosters with captains and members.
    """
    from bson import ObjectId

    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    tid_query = [tid, tslug]
    try:
        if ObjectId.is_valid(tid):
            tid_query.append(ObjectId(tid))
    except Exception:
        pass

    reg_cursor = db.tournament_registrations.find({
        "$or": [{"tournament_id": {"$in": tid_query}}, {"tournament_id": tid}, {"tournament_id": tslug}],
        "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
    })
    registrations = await reg_cursor.to_list(length=200)

    # Check which teams are already paired in matches
    matches = await db.matches.find({
        "$or": [{"tournament_id": tid}, {"tournament_id": tslug}]
    }).to_list(length=200)

    paired_team_ids = set()
    for m in matches:
        if m.get("team_a_id"):
            paired_team_ids.add(str(m["team_a_id"]))
        if m.get("team_b_id"):
            paired_team_ids.add(str(m["team_b_id"]))

    teams_list = []
    for reg in registrations:
        team_id = reg.get("team_id")
        player_id = reg.get("player_id") or reg.get("user_id")

        if team_id:
            team_or = [{"_id": team_id}, {"id": team_id}]
            try:
                if ObjectId.is_valid(str(team_id)):
                    team_or.append({"_id": ObjectId(str(team_id))})
            except Exception:
                pass
            team_doc = await db.teams.find_one({"$or": team_or})
            if team_doc:
                t_id_str = str(team_doc.get("_id", team_doc.get("id")))

                # Captain lookup
                cap_user = None
                cap_id = team_doc.get("captain_id")
                if cap_id:
                    cap_or = [{"_id": cap_id}, {"id": cap_id}]
                    try:
                        if ObjectId.is_valid(str(cap_id)):
                            cap_or.append({"_id": ObjectId(str(cap_id))})
                    except Exception:
                        pass
                    cap_user = await db.users.find_one({"$or": cap_or})

                cap_username = (
                    cap_user.get("username")
                    or cap_user.get("display_name")
                    or team_doc.get("captain_username", "Captain")
                    if cap_user
                    else team_doc.get("captain_username", "Captain")
                )
                cap_game_id = cap_user.get("game_id") if cap_user else None

                # Members list
                members_list = []
                members_handles = []
                for m in team_doc.get("members", []):
                    if isinstance(m, dict):
                        u_id = str(m.get("user_id", ""))
                        u_name = m.get("username", "Player")
                        g_handle = m.get("game_handle", m.get("game_id", ""))
                        role = m.get("role", "PLAYER")
                    else:
                        u_id = str(getattr(m, "user_id", ""))
                        u_name = getattr(m, "username", "Player")
                        g_handle = getattr(m, "game_handle", getattr(m, "game_id", ""))
                        role = getattr(m, "role", "PLAYER")
                    members_list.append({
                        "user_id": u_id,
                        "username": u_name,
                        "game_id": g_handle,
                        "role": role,
                    })
                    members_handles.append(g_handle or u_name)

                # Ensure captain is listed in members if missing
                if cap_id and not any(m["user_id"] == str(cap_id) for m in members_list):
                    members_list.insert(0, {
                        "user_id": str(cap_id),
                        "username": cap_username,
                        "game_id": cap_game_id or cap_username,
                        "role": "CAPTAIN",
                    })
                    members_handles.insert(0, cap_game_id or cap_username)

                entry = {
                    "id": t_id_str,
                    "team_id": t_id_str,
                    "name": team_doc.get("name", "Team"),
                    "team_name": team_doc.get("name", "Team"),
                    "tag": team_doc.get("tag", "TEAM"),
                    "team_tag": team_doc.get("tag", "TEAM"),
                    "captain": {
                        "user_id": str(cap_id or ""),
                        "username": cap_username,
                        "game_id": cap_game_id,
                    },
                    "captain_username": cap_username,
                    "members": members_list,
                    "member_handles": members_handles,
                    "is_paired": t_id_str in paired_team_ids,
                    "is_solo": False,
                }
                teams_list.append(entry)

        elif player_id:
            user_or = [{"_id": player_id}, {"id": player_id}]
            try:
                if ObjectId.is_valid(str(player_id)):
                    user_or.append({"_id": ObjectId(str(player_id))})
            except Exception:
                pass
            user_doc = await db.users.find_one({"$or": user_or})
            if user_doc:
                u_id_str = str(user_doc.get("_id", user_doc.get("id")))
                p_name = user_doc.get("display_name") or user_doc.get("username", "Contender")
                g_id = user_doc.get("game_id", "")
                entry = {
                    "id": u_id_str,
                    "team_id": u_id_str,
                    "name": p_name,
                    "team_name": p_name,
                    "tag": "SOLO",
                    "team_tag": "SOLO",
                    "captain": {
                        "user_id": u_id_str,
                        "username": p_name,
                        "game_id": g_id,
                    },
                    "captain_username": p_name,
                    "members": [{
                        "user_id": u_id_str,
                        "username": p_name,
                        "game_id": g_id,
                        "role": "SOLO",
                    }],
                    "member_handles": [p_name],
                    "is_paired": u_id_str in paired_team_ids,
                    "is_solo": True,
                }
                teams_list.append(entry)

    return {
        "tournament_id": tid,
        "tournament_title": tourn.get("title"),
        "tournament_status": "REGISTRATION_CLOSED",
        "game_name": tourn.get("game_name", "Esports"),
        "game_slug": tourn.get("game_slug", "esports"),
        "match_format": tourn.get("match_format") or tourn.get("tournament_type") or "Single Elimination",
        "series_format": tourn.get("series_format") or "BO1",
        "total_teams": len(teams_list),
        "total_registered": len(teams_list),
        "teams": teams_list,
        "rosters": teams_list,
    }

# ----------------- BATTLE ROYALE MATCH BUILDER & CALCULATION -----------------

class GenerateBRMatchesPayload(BaseModel):
    stage_name: str = "Round 1"
    round_name: str = "Round 1"
    balanced: bool = False
    lobby_size: Optional[int] = None
    max_teams_per_match: Optional[int] = None
    custom_distribution: Optional[List[Dict[str, Any]]] = None

class RebalanceBRMatchesPayload(BaseModel):
    team_id: str
    from_match_id: str
    to_match_id: str

@router.get("/tournaments/{id}/battle-royale-calc")
async def get_battle_royale_calculation_admin(
    id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Authoritative Battle Royale calculation based strictly on confirmed registered teams in database.
    Calculates Maximum Teams Per Match = floor(Room Capacity / Players Per Team)
    and Required Matches = ceil(Actual Registered Teams / Maximum Teams Per Match).
    """
    from app.services.battle_royale_engine import (
        get_confirmed_registered_teams,
        get_battle_royale_specs,
        calculate_battle_royale_parameters,
        distribute_teams_into_matches,
    )

    tourn, teams_list = await get_confirmed_registered_teams(id, db)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    game_slug_or_id = tourn.get("game_slug") or tourn.get("game_id", "")
    game_doc = await db.games.find_one({"$or": [{"_id": game_slug_or_id}, {"id": game_slug_or_id}, {"slug": game_slug_or_id}]})

    room_capacity, players_per_team = get_battle_royale_specs(
        game_slug_or_name=game_slug_or_id,
        tournament_doc=tourn,
        game_doc=game_doc,
    )

    calc = calculate_battle_royale_parameters(
        actual_registered_teams=len(teams_list),
        room_capacity=room_capacity,
        players_per_team=players_per_team,
    )

    chunks = distribute_teams_into_matches(
        teams=teams_list,
        max_teams_per_match=calc["max_teams_per_match"],
        required_matches=calc["required_matches"],
        balanced=False,
    )

    preview_distribution = []
    for idx, chunk in enumerate(chunks):
        preview_distribution.append({
            "match_number": idx + 1,
            "match_name": f"Match {idx + 1} Lobby",
            "team_count": len(chunk),
            "teams": chunk,
        })

    # Fetch existing BR matches in database for this tournament
    existing_cursor = db.matches.find({
        "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
        "$or": [{"format": "Battle Royale"}, {"match_type": "BATTLE_ROYALE"}],
    })
    existing_matches_raw = await existing_cursor.to_list(length=100)
    existing_matches = []
    for em in existing_matches_raw:
        m_obj = Match.from_mongo(em)
        if m_obj:
            existing_matches.append(m_obj.model_dump())

    return {
        "tournament_id": tid,
        "tournament_title": tourn.get("title"),
        "game_name": tourn.get("game_name") or (game_doc.get("name") if game_doc else "Esports"),
        "game_slug": game_slug_or_id,
        "registered_teams_count": calc["actual_registered_teams"],
        "room_capacity": calc["room_capacity"],
        "players_per_team": calc["players_per_team"],
        "max_teams_per_match": calc["max_teams_per_match"],
        "required_matches": calc["required_matches"],
        "teams": teams_list,
        "preview_distribution": preview_distribution,
        "existing_matches": existing_matches,
    }

@router.post("/tournaments/{id}/generate-battle-royale-matches")
async def generate_battle_royale_matches_admin(
    id: str,
    payload: Optional[GenerateBRMatchesPayload] = None,
    round_number: Optional[int] = None,
    lobby_size: Optional[int] = None,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Generates Battle Royale match lobbies distributing the actual registered teams.
    Saves to db.matches and marks tournament format as Battle Royale.
    """
    import math
    from app.services.battle_royale_engine import (
        get_confirmed_registered_teams,
        get_battle_royale_specs,
        calculate_battle_royale_parameters,
        distribute_teams_into_matches,
    )
    from app.models.base import generate_uuid

    if payload is None:
        payload = GenerateBRMatchesPayload()

    tourn, teams_list = await get_confirmed_registered_teams(id, db)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    game_slug_or_id = tourn.get("game_slug") or tourn.get("game_id", "")
    game_doc = await db.games.find_one({"$or": [{"_id": game_slug_or_id}, {"id": game_slug_or_id}, {"slug": game_slug_or_id}]})

    room_capacity, players_per_team = get_battle_royale_specs(
        game_slug_or_name=game_slug_or_id,
        tournament_doc=tourn,
        game_doc=game_doc,
    )

    effective_round_num = round_number or payload.round_number or 1
    if effective_round_num > 1:
        prev_round = effective_round_num - 1
        qual_docs = await db.tournament_qualifications.find({
            "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
            "source_round_number": prev_round,
            "status": "QUALIFIED",
        }).to_list(length=500)
        if qual_docs:
            qual_ids = set([d["participant_id"] for d in qual_docs])
            teams_list = [t for t in teams_list if t["id"] in qual_ids]
        else:
            # Fallback to previous round match qualifiers
            prev_match = await db.matches.find_one({
                "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
                "round_number": prev_round,
            })
            if prev_match and prev_match.get("qualifier_team_ids"):
                qual_ids = set(prev_match["qualifier_team_ids"])
                teams_list = [t for t in teams_list if t["id"] in qual_ids]

    calc = calculate_battle_royale_parameters(
        actual_registered_teams=len(teams_list),
        room_capacity=room_capacity,
        players_per_team=players_per_team,
    )

    effective_max_teams = None
    if lobby_size is not None and lobby_size > 0:
        effective_max_teams = lobby_size
    elif payload.lobby_size is not None and payload.lobby_size > 0:
        effective_max_teams = payload.lobby_size
    elif payload.max_teams_per_match is not None and payload.max_teams_per_match > 0:
        effective_max_teams = payload.max_teams_per_match
    else:
        effective_max_teams = calc["max_teams_per_match"]

    effective_required_matches = math.ceil(len(teams_list) / max(1, effective_max_teams)) if effective_max_teams else calc["required_matches"]

    teams_by_id = {t["id"]: t for t in teams_list}

    if payload.custom_distribution:
        match_partitions: List[List[Dict[str, Any]]] = []
        for custom_m in payload.custom_distribution:
            part: List[Dict[str, Any]] = []
            for t_id in custom_m.get("team_ids", []):
                if t_id in teams_by_id:
                    part.append(teams_by_id[t_id])
            if part:
                match_partitions.append(part)
        if not match_partitions:
            match_partitions = distribute_teams_into_matches(
                teams=teams_list,
                max_teams_per_match=effective_max_teams,
                required_matches=effective_required_matches,
                balanced=payload.balanced,
            )
    else:
        match_partitions = distribute_teams_into_matches(
            teams=teams_list,
            max_teams_per_match=effective_max_teams,
            required_matches=effective_required_matches,
            balanced=payload.balanced,
        )

    stage_name = payload.stage_name or (f"Round {effective_round_num}")
    round_name = payload.round_name or (f"Round {effective_round_num}")

    # Delete existing scheduled Battle Royale matches for this round before recreating
    await db.matches.delete_many({
        "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
        "round_name": round_name,
        "status": "SCHEDULED",
        "format": "Battle Royale",
    })

    created_matches: List[Dict[str, Any]] = []
    for idx, chunk in enumerate(match_partitions):
        match_num = idx + 1
        m_id = generate_uuid()
        team_ids = [t["id"] for t in chunk]

        match_doc = Match(
            id=m_id,
            tournament_id=tid,
            stage_name=stage_name,
            round_name=round_name,
            stage=round_name,
            match_type="BATTLE_ROYALE",
            round_number=effective_round_num,
            match_number=match_num,
            format="Battle Royale",
            series_format="BO1",
            status="SCHEDULED",
            room_capacity=calc["room_capacity"],
            players_per_team=calc["players_per_team"],
            max_teams=calc["max_teams_per_match"],
            teams=chunk,
            team_ids=team_ids,
            team_a_id=None,
            team_a_name=None,
            team_b_id=None,
            team_b_name=None,
            scheduled_time="TBD",
            server_region=tourn.get("server_region", "AWS Mumbai (ap-south-1)"),
        )
        await db.matches.insert_one(match_doc.to_mongo())
        created_matches.append(match_doc.model_dump())

    # Update tournament format
    await db.tournaments.update_one(
        {"_id": tourn["_id"]},
        {"$set": {
            "match_format": "Battle Royale",
            "tournament_type": "Battle Royale",
            "format": "Battle Royale",
            "room_capacity": calc["room_capacity"],
            "players_per_team": calc["players_per_team"],
        }}
    )

    try:
        from app.services.live_match_service import sync_live_match_for_tournament
        await sync_live_match_for_tournament(tid, db)
    except Exception as e:
        pass

    return {
        "status": "success",
        "message": f"Successfully generated {len(created_matches)} Battle Royale match lobbies across {len(teams_list)} confirmed teams.",
        "total_matches": len(created_matches),
        "matches": created_matches,
    }

@router.post("/tournaments/{id}/rebalance-battle-royale-matches")
async def rebalance_battle_royale_matches_admin(
    id: str,
    payload: RebalanceBRMatchesPayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Moves a confirmed team between Battle Royale match lobbies.
    """
    source_match = await db.matches.find_one({"$or": [{"_id": payload.from_match_id}, {"id": payload.from_match_id}]})
    dest_match = await db.matches.find_one({"$or": [{"_id": payload.to_match_id}, {"id": payload.to_match_id}]})

    if not source_match or not dest_match:
        raise HTTPException(status_code=404, detail="Source or destination match not found")

    src_teams = source_match.get("teams", [])
    team_to_move = next((t for t in src_teams if str(t.get("id")) == payload.team_id or str(t.get("team_id")) == payload.team_id), None)
    if not team_to_move:
        raise HTTPException(status_code=404, detail="Team not found in source match")

    # Remove from source
    updated_src_teams = [t for t in src_teams if str(t.get("id")) != payload.team_id and str(t.get("team_id")) != payload.team_id]
    updated_src_ids = [t.get("id") for t in updated_src_teams]

    # Add to destination
    dest_teams = dest_match.get("teams", [])
    if not any(str(t.get("id")) == payload.team_id for t in dest_teams):
        dest_teams.append(team_to_move)
    updated_dest_ids = [t.get("id") for t in dest_teams]

    await db.matches.update_one(
        {"_id": source_match["_id"]},
        {"$set": {
            "teams": updated_src_teams,
            "team_ids": updated_src_ids,
            "team_a_id": updated_src_teams[0]["id"] if len(updated_src_teams) > 0 else None,
            "team_a_name": updated_src_teams[0]["name"] if len(updated_src_teams) > 0 else "Lobby",
            "team_b_id": updated_src_teams[1]["id"] if len(updated_src_teams) > 1 else None,
            "team_b_name": f"+{len(updated_src_teams) - 1} Teams" if len(updated_src_teams) > 1 else None,
        }}
    )
    await db.matches.update_one(
        {"_id": dest_match["_id"]},
        {"$set": {
            "teams": dest_teams,
            "team_ids": updated_dest_ids,
            "team_a_id": dest_teams[0]["id"] if len(dest_teams) > 0 else None,
            "team_a_name": dest_teams[0]["name"] if len(dest_teams) > 0 else "Lobby",
            "team_b_id": dest_teams[1]["id"] if len(dest_teams) > 1 else None,
            "team_b_name": f"+{len(dest_teams) - 1} Teams" if len(dest_teams) > 1 else None,
        }}
    )

    return {
        "status": "success",
        "message": f"Team {team_to_move.get('name', 'Team')} moved to Match #{dest_match.get('match_number', '')} successfully",
    }

@router.post("/tournaments/{id}/generate-match-structure")
async def generate_match_structure_admin(
    id: str,
    payload: Dict[str, Any],
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Generates complete tournament match structure based on selected competition format.
    Returns both the bracket hierarchy and a flat list of rich match items ready for bulk review and scheduling.
    Tournament remains REGISTRATION_CLOSED.
    """
    from app.services.bracket_engine import generate_tournament_bracket

    tourn_doc = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn_doc:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn_doc.get("_id", tourn_doc.get("id")))
    selected_format = payload.get("match_format") or payload.get("tournament_type") or tourn_doc.get("match_format") or "Single Elimination"
    selected_series = payload.get("series_format") or tourn_doc.get("series_format") or "BO1"

    # Update tournament format fields
    await db.tournaments.update_one(
        {"_id": tourn_doc["_id"]},
        {"$set": {
            "match_format": selected_format,
            "tournament_type": selected_format,
            "series_format": selected_series,
            "status": "REGISTRATION_CLOSED"
        }}
    )

    tourn_doc["match_format"] = selected_format
    tourn_doc["tournament_type"] = selected_format
    tourn_doc["series_format"] = selected_series
    tourn_obj = Tournament.from_mongo(tourn_doc)

    bracket_res = await generate_tournament_bracket(tourn_obj, db)

    # Fetch all newly generated matches with details
    saved_matches = await db.matches.find({
        "$or": [{"tournament_id": tid}, {"tournament_id": tourn_doc.get("slug")}]
    }).sort([("round_number", 1), ("match_number", 1)]).to_list(length=200)

    # Map team names
    team_ids_needed = set()
    for m in saved_matches:
        if m.get("team_a_id"):
            team_ids_needed.add(str(m["team_a_id"]))
        if m.get("team_b_id"):
            team_ids_needed.add(str(m["team_b_id"]))

    team_map = {}
    if team_ids_needed:
        team_docs = await db.teams.find({"$or": [{"_id": {"$in": list(team_ids_needed)}}, {"id": {"$in": list(team_ids_needed)}}]}).to_list(length=200)
        for td in team_docs:
            name = td.get("name") or td.get("team_name", "Team")
            team_map[str(td.get("_id"))] = name
            if td.get("id"):
                team_map[str(td.get("id"))] = name
        user_docs = await db.users.find({"$or": [{"_id": {"$in": list(team_ids_needed)}}, {"id": {"$in": list(team_ids_needed)}}]}).to_list(length=200)
        for ud in user_docs:
            u_name = ud.get("display_name") or ud.get("username", "Contender")
            team_map[str(ud.get("_id"))] = u_name
            if ud.get("id"):
                team_map[str(ud.get("id"))] = u_name

    matches_rich = []
    for m in saved_matches:
        t_a_id = m.get("team_a_id")
        t_b_id = m.get("team_b_id")
        t_a_name = team_map.get(str(t_a_id), f"Team ({t_a_id})") if t_a_id else "TBD"
        t_b_name = team_map.get(str(t_b_id), f"Team ({t_b_id})") if t_b_id else "TBD"

        matches_rich.append({
            "id": str(m.get("_id", m.get("id"))),
            "round_number": m.get("round_number", 1),
            "match_number": m.get("match_number", 1),
            "stage": m.get("stage", f"Round {m.get('round_number', 1)} Match {m.get('match_number', 1)}"),
            "match_type": m.get("match_type", "QUALIFIER"),
            "series_format": m.get("series_format") or selected_series,
            "team_a_id": t_a_id,
            "team_b_id": t_b_id,
            "team_a_name": t_a_name,
            "team_b_name": t_b_name,
            "scheduled_date": m.get("scheduled_date") or "",
            "scheduled_time": m.get("scheduled_time") or "",
            "scheduled_at": m.get("scheduled_at") or "",
            "youtube_live_url": m.get("youtube_live_url") or "",
            "status": m.get("status", "SCHEDULED"),
        })

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="GENERATE_MATCH_STRUCTURE",
        target=f"Tournament {tourn_obj.title}",
        details=f"Format: {selected_format} | Series: {selected_series} | Matches: {len(matches_rich)}",
    )

    return {
        "status": "success",
        "message": f'Successfully generated match structure for "{tourn_obj.title}" ({selected_format}) with {len(matches_rich)} matches!',
        "bracket": bracket_res.get("bracket", []),
        "matches": matches_rich,
    }

@router.post("/tournaments/{id}/save-live-structure")
async def save_live_match_structure_admin(
    id: str,
    payload: AdminSaveLiveStructurePayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Saves the complete live match setup in one atomic operation.
    Persists all matches as SCHEDULED with per-match YouTube URLs and schedules.
    Tournament remains REGISTRATION_CLOSED.
    """
    import uuid
    import secrets

    tourn_doc = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn_doc:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn_doc.get("_id", tourn_doc.get("id")))
    tslug = tourn_doc.get("slug", tid)

    # Fetch existing matches so completed or in-progress matches and their winners/results are NEVER lost
    existing_cursor = db.matches.find({"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]})
    existing_list = await existing_cursor.to_list(length=500)
    existing_by_id = {}
    existing_by_round_match = {}
    for em in existing_list:
        em_id = str(em.get("_id", em.get("id")))
        existing_by_id[em_id] = em
        if em.get("id"):
            existing_by_id[str(em["id"])] = em
        r_num = em.get("round_number", 1)
        m_num = em.get("match_number") or em.get("order") or 1
        existing_by_round_match[(r_num, m_num)] = em

    locked_statuses = {"COMPLETED", "FINALIZED", "LIVE", "ENDED", "RESULTS_SAVED", "RESULTS_PENDING"}
    locked_ids = {str(em.get("_id", em.get("id"))) for em in existing_list if str(em.get("status", "")).upper() in locked_statuses}

    # Delete only unplayed draft or scheduled matches that are removed from payload
    payload_match_ids = {str(item.id) for item in payload.matches if item.id}
    deletable_ids = []
    for em in existing_list:
        em_id = str(em.get("_id", em.get("id")))
        em_stat = str(em.get("status", "SCHEDULED")).upper()
        if em_id not in locked_ids and em_id not in payload_match_ids and em_stat not in locked_statuses:
            deletable_ids.append(em["_id"])
    if deletable_ids:
        await db.matches.delete_many({"_id": {"$in": deletable_ids}})

    bracket_rounds_map = {}
    saved_matches = []

    for idx, item in enumerate(payload.matches, start=1):
        match_id = item.id or f"match_{uuid.uuid4().hex[:12]}"
        r_num = item.round_number or 1
        m_num = item.match_number or idx
        stage = item.stage or f"Round {r_num} — Match {m_num}"
        m_type = item.match_type or "QUALIFIER"
        s_fmt = item.series_format or payload.series_format or "BO1"

        sched_date = item.scheduled_date or ""
        sched_time = item.scheduled_time or ""
        sched_combined = f"{sched_date} {sched_time}".strip() or (str(item.scheduled_at) if item.scheduled_at else "") or f"Round {r_num} Match {m_num}"

        parsed_dt = None
        if item.scheduled_at:
            try:
                from datetime import datetime
                if isinstance(item.scheduled_at, str):
                    parsed_dt = datetime.fromisoformat(item.scheduled_at.replace("Z", "+00:00"))
            except Exception:
                pass
        if not parsed_dt and sched_date and sched_time:
            from datetime import datetime
            for fmt_str in ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p", "%d %B %Y %I:%M %p"):
                try:
                    parsed_dt = datetime.strptime(f"{sched_date} {sched_time}".strip(), fmt_str)
                    break
                except Exception:
                    pass

        # Check if match already exists and has results or locked status
        existing_match = existing_by_id.get(str(match_id)) or existing_by_round_match.get((r_num, m_num))
        is_existing_locked = existing_match and str(existing_match.get("status", "")).upper() in locked_statuses

        if is_existing_locked:
            # Preserve status, score, winner, results, and credentials
            upd_data: Dict[str, Any] = {
                "round_name": item.round_name or stage,
                "stage": stage,
                "scheduled_time": sched_combined,
                "scheduled_date": sched_date,
            }
            if parsed_dt:
                upd_data["scheduled_at"] = parsed_dt
            if item.youtube_live_url:
                upd_data["youtube_live_url"] = item.youtube_live_url
                upd_data["live_link"] = item.youtube_live_url
            await db.matches.update_one({"_id": existing_match["_id"]}, {"$set": upd_data})
            saved_matches.append(str(existing_match.get("_id", existing_match.get("id"))))
            effective_status = existing_match.get("status", "COMPLETED")
            effective_winner = existing_match.get("winner_team_id")
            effective_team_a = existing_match.get("team_a_id", item.team_a_id)
            effective_team_b = existing_match.get("team_b_id", item.team_b_id)
            effective_teams = existing_match.get("teams", item.teams or [])
            effective_team_ids = existing_match.get("team_ids", item.team_ids or [])
        else:
            room_code = f"AMT-R{r_num}M{m_num}"
            room_pass = f"AMT-{secrets.token_hex(3).upper()}"
            db_match = Match(
                id=match_id,
                tournament_id=tid,
                stage=stage,
                round_name=item.round_name or stage,
                match_type=m_type,
                round_number=r_num,
                match_number=m_num,
                format=item.format or s_fmt,
                series_format=s_fmt,
                scheduled_time=sched_combined,
                scheduled_at=parsed_dt,
                status="SCHEDULED",
                roster_status=item.roster_status or "DRAFT",
                team_a_id=item.team_a_id,
                team_b_id=item.team_b_id,
                team_a_name=item.team_a_name,
                team_b_name=item.team_b_name,
                team_a_placeholder=item.team_a_placeholder,
                team_b_placeholder=item.team_b_placeholder,
                team_ids=item.team_ids or [],
                teams=item.teams or [],
                next_match_id=item.next_match_id,
                next_match_slot=item.next_match_slot,
                advancement_label=item.advancement_label,
                join_info_enabled=item.join_info_enabled or False,
                join_info=item.join_info,
                match_message=item.match_message,
                youtube_live_url=item.youtube_live_url or None,
                live_link=item.youtube_live_url or None,
                server_region=tourn_doc.get("server_region", "Asia (Mumbai)"),
                room_id=room_code,
                room_password=room_pass,
                lobby_code=room_code,
                lobby_password=room_pass,
            )
            m_mongo = db_match.to_mongo()
            if existing_match:
                await db.matches.update_one({"_id": existing_match["_id"]}, {"$set": m_mongo})
            else:
                await db.matches.insert_one(m_mongo)
            saved_matches.append(db_match.id)
            effective_status = "SCHEDULED"
            effective_winner = None
            effective_team_a = item.team_a_id
            effective_team_b = item.team_b_id
            effective_teams = item.teams or []
            effective_team_ids = item.team_ids or []

        if r_num not in bracket_rounds_map:
            bracket_rounds_map[r_num] = {
                "round_number": r_num,
                "round_name": item.round_name or f"Round {r_num}",
                "matches": []
            }
        bracket_rounds_map[r_num]["matches"].append({
            "id": db_match.id,
            "stage": stage,
            "round_name": item.round_name or stage,
            "match_type": m_type,
            "format": item.format or s_fmt,
            "series_format": s_fmt,
            "match_number": m_num,
            "team_a_id": item.team_a_id,
            "team_b_id": item.team_b_id,
            "team_ids": item.team_ids or [],
            "teams": item.teams or [],
            "youtube_live_url": item.youtube_live_url,
            "scheduled_time": sched_combined,
            "status": "SCHEDULED",
            "roster_status": item.roster_status or "DRAFT",
        })

    bracket_tree = [bracket_rounds_map[k] for k in sorted(bracket_rounds_map.keys())]

    # Save to tournament
    custom_round_names = []
    if payload.rounds:
        for r in payload.rounds:
            rn = r.get("round_name") or r.get("name")
            if rn and rn not in custom_round_names:
                custom_round_names.append(rn)
    if not custom_round_names:
        for m in payload.matches:
            rn = m.round_name or m.stage
            if rn and rn not in custom_round_names:
                custom_round_names.append(rn)

    await db.tournaments.update_one(
        {"_id": tourn_doc["_id"]},
        {"$set": {
            "match_format": payload.match_format,
            "tournament_type": payload.match_format,
            "series_format": payload.series_format,
            "bracket_rounds": bracket_tree,
            "formation_status": payload.formation_status or "DRAFT",
            "custom_rounds": custom_round_names,
            "status": "REGISTRATION_CLOSED",
        }}
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="SAVE_LIVE_MATCH_STRUCTURE",
        target=f"Tournament {tourn_doc.get('title')}",
        details=f"Saved {len(saved_matches)} matches. Format: {payload.match_format}, Series: {payload.series_format}",
    )

    return {
        "status": "success",
        "message": f"Successfully saved {len(saved_matches)} live matches for {tourn_doc.get('title')}!",
        "total_matches": len(saved_matches),
        "bracket_rounds": bracket_tree,
    }


@router.post("/matches")
async def create_match_admin(
    payload: AdminMatchCreatePayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Matchup Builder endpoint.
    Admin manually adds a match between two registered teams or previous round winners.
    Validates against:
    - Same team on both sides
    - Overlapping schedule conflicts for either team (when scheduled)
    """
    import uuid
    from datetime import datetime, timezone, timedelta

    if payload.team_a_id and payload.team_b_id and payload.team_a_id == payload.team_b_id:
        raise HTTPException(status_code=400, detail="Cannot pair a team against itself. Please select two different teams.")

    tourn = await db.tournaments.find_one({
        "$or": [{"_id": payload.tournament_id}, {"id": payload.tournament_id}, {"slug": payload.tournament_id}]
    })
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    t_status = str(tourn.get("status") or "").upper()
    t_reg_status = str(tourn.get("registration_status") or "").upper()
    is_closed = (
        t_reg_status == "CLOSED"
        or t_status == "REGISTRATION_CLOSED"
        or bool(tourn.get("is_registration_closed"))
        or t_status in ["COMPLETED", "IN_PROGRESS"]
    )
    if not is_closed:
        raise HTTPException(
            status_code=400,
            detail="Match creation is only allowed for tournaments whose registration is CLOSED. Please close registration before creating matches.",
        )

    tid = str(tourn.get("_id", tourn.get("id")))

    # Resolve Team A
    name_a = payload.team_a_placeholder or "TBD"
    if payload.team_a_id:
        team_a = await db.teams.find_one({"$or": [{"_id": payload.team_a_id}, {"id": payload.team_a_id}]})
        if not team_a:
            team_a = await db.users.find_one({"$or": [{"_id": payload.team_a_id}, {"id": payload.team_a_id}]})
        if team_a:
            name_a = team_a.get("name") or team_a.get("username") or "Team A"
        else:
            name_a = payload.team_a_name or payload.team_a_placeholder or "Team A"
    elif payload.team_a_name:
        name_a = payload.team_a_name
    elif not payload.team_a_placeholder:
        raise HTTPException(status_code=400, detail="Team A ID, name, or placeholder is required.")

    # Resolve Team B
    name_b = payload.team_b_placeholder or "TBD"
    if payload.team_b_id:
        team_b = await db.teams.find_one({"$or": [{"_id": payload.team_b_id}, {"id": payload.team_b_id}]})
        if not team_b:
            team_b = await db.users.find_one({"$or": [{"_id": payload.team_b_id}, {"id": payload.team_b_id}]})
        if team_b:
            name_b = team_b.get("name") or team_b.get("username") or "Team B"
        else:
            name_b = payload.team_b_name or payload.team_b_placeholder or "Team B"
    elif payload.team_b_name:
        name_b = payload.team_b_name
    elif not payload.team_b_placeholder:
        raise HTTPException(status_code=400, detail="Team B ID, name, or placeholder is required.")

    # Schedule parsing and conflict check
    scheduled_at_dt = None
    scheduled_time_str = payload.scheduled_time or "TBD"

    if payload.scheduled_at:
        try:
            scheduled_at_dt = datetime.fromisoformat(payload.scheduled_at.replace("Z", "+00:00"))
            scheduled_time_str = scheduled_at_dt.strftime("%b %d, %I:%M %p")
        except Exception:
            pass
    elif payload.scheduled_date and payload.scheduled_time:
        scheduled_time_str = f"{payload.scheduled_date} — {payload.scheduled_time}"

    # Conflict check: warn if either team already has an active or scheduled match at the same time
    if scheduled_at_dt and payload.team_a_id and payload.team_b_id:
        window_start = scheduled_at_dt - timedelta(minutes=30)
        window_end = scheduled_at_dt + timedelta(minutes=30)
        conflict = await db.matches.find_one({
            "tournament_id": tid,
            "status": {"$in": ["SCHEDULED", "LIVE"]},
            "scheduled_at": {"$gte": window_start, "$lte": window_end},
            "$or": [
                {"team_a_id": payload.team_a_id},
                {"team_b_id": payload.team_a_id},
                {"team_a_id": payload.team_b_id},
                {"team_b_id": payload.team_b_id},
            ]
        })
        if conflict:
            raise HTTPException(
                status_code=400,
                detail=f"Scheduling conflict: One of the teams ({name_a} or {name_b}) is already scheduled for another match within 30 minutes of this time slot.",
            )

    # Strictly BO1 or BO3
    raw_fmt = (payload.series_format or payload.match_format or "BO1").upper()
    series_fmt = "BO3" if raw_fmt in ["BO3", "BEST OF 3"] else "BO1"

    round_name = payload.round_name or payload.stage or f"Round {payload.round_number or 1}"
    round_num = payload.round_number or 1
    match_num = payload.match_number or 1

    match = Match(
        tournament_id=tid,
        round_name=round_name,
        round_number=round_num,
        match_number=match_num,
        stage=round_name,
        match_type=payload.match_type or "MATCH",
        format=series_fmt,
        series_format=series_fmt,
        scheduled_at=scheduled_at_dt,
        scheduled_time=scheduled_time_str,
        scheduled_date=payload.scheduled_date,
        roster_status=(payload.roster_status or "PUBLISHED").upper(),
        live_link=payload.youtube_live_url,
        youtube_live_url=payload.youtube_live_url,
        status=payload.status or "SCHEDULED",
        team_a_id=payload.team_a_id,
        team_b_id=payload.team_b_id,
        team_a_name=name_a,
        team_b_name=name_b,
        team_a_placeholder=payload.team_a_placeholder,
        team_b_placeholder=payload.team_b_placeholder,
        team_ids=payload.team_ids or [],
        teams=payload.teams or [],
        next_match_id=payload.next_match_id,
        next_match_slot=payload.next_match_slot,
        advancement_label=payload.advancement_label,
        join_info_enabled=payload.join_info_enabled or False,
        join_info=payload.join_info,
        match_message=payload.match_message,
        score_a=payload.score_a if payload.score_a is not None else 0,
        score_b=payload.score_b if payload.score_b is not None else 0,
        winner_team_id=payload.winner_team_id,
        games=payload.games or [],
        server_region=tourn.get("server_region", "AWS Mumbai (ap-south-1)"),
        lobby_code=f"AMT-{uuid.uuid4().hex[:6].upper()}",
        lobby_password=f"PASS-{uuid.uuid4().hex[:6].upper()}",
        check_in_status={"team_a": False, "team_b": False},
    )
    await db.matches.insert_one(match.to_mongo())

    if match.roster_status == "PUBLISHED":
        try:
            from app.services.match_notification_service import notify_roster_published
            await notify_roster_published(match.id, db)
        except Exception as e:
            print(f"[Roster Publication Notification Warning]: {e}")

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="CREATE_MATCH",
        target=f"Match {match.id} ({name_a} vs {name_b})",
        details=f"Round: {round_name} | Series: {series_fmt} | Status: {match.roster_status}",
    )

    return {
        "status": "success",
        "message": f"Successfully created match: {name_a} vs {name_b}!",
        "match": match.model_dump(),
    }

@router.patch("/matches/{id}/update")
async def update_match_details_admin(
    id: str,
    payload: AdminMatchUpdatePayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Allows admin to modify match parameters: swap teams, placeholders, change stage/round, series format, schedule, scores, or YouTube URL.
    """
    from bson import ObjectId
    m_query = {"$or": [{"_id": id}, {"id": id}]}
    if ObjectId.is_valid(id):
        m_query["$or"].append({"_id": ObjectId(id)})
    match = await db.matches.find_one(m_query)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    update_fields = {}
    if payload.team_a_id is not None:
        update_fields["team_a_id"] = payload.team_a_id
        if payload.team_a_name:
            update_fields["team_a_name"] = payload.team_a_name
        else:
            team_a = await db.teams.find_one({"$or": [{"_id": payload.team_a_id}, {"id": payload.team_a_id}]})
            if team_a:
                update_fields["team_a_name"] = team_a.get("name")
    elif payload.team_a_name is not None:
        update_fields["team_a_name"] = payload.team_a_name

    if payload.team_b_id is not None:
        update_fields["team_b_id"] = payload.team_b_id
        if payload.team_b_name:
            update_fields["team_b_name"] = payload.team_b_name
        else:
            team_b = await db.teams.find_one({"$or": [{"_id": payload.team_b_id}, {"id": payload.team_b_id}]})
            if team_b:
                update_fields["team_b_name"] = team_b.get("name")
    elif payload.team_b_name is not None:
        update_fields["team_b_name"] = payload.team_b_name

    if payload.team_a_placeholder is not None:
        update_fields["team_a_placeholder"] = payload.team_a_placeholder
    if payload.team_b_placeholder is not None:
        update_fields["team_b_placeholder"] = payload.team_b_placeholder
    if payload.next_match_id is not None:
        update_fields["next_match_id"] = payload.next_match_id
    if payload.next_match_slot is not None:
        update_fields["next_match_slot"] = payload.next_match_slot
    if payload.advancement_label is not None:
        update_fields["advancement_label"] = payload.advancement_label
    if payload.join_info_enabled is not None:
        update_fields["join_info_enabled"] = payload.join_info_enabled
    if payload.join_info is not None:
        update_fields["join_info"] = payload.join_info
    if payload.match_message is not None:
        update_fields["match_message"] = payload.match_message
    if payload.round_name is not None:
        update_fields["round_name"] = payload.round_name
        update_fields["stage"] = payload.round_name
    if payload.round_number is not None:
        update_fields["round_number"] = payload.round_number
    if payload.match_number is not None:
        update_fields["match_number"] = payload.match_number
    if payload.match_type is not None:
        update_fields["match_type"] = payload.match_type
    if payload.stage is not None and "stage" not in update_fields:
        update_fields["stage"] = payload.stage
    if payload.series_format is not None:
        s_fmt = "BO3" if payload.series_format.upper() in ["BO3", "BEST OF 3"] else "BO1"
        update_fields["series_format"] = s_fmt
        update_fields["format"] = s_fmt
    if payload.youtube_live_url is not None:
        update_fields["youtube_live_url"] = payload.youtube_live_url
        update_fields["live_link"] = payload.youtube_live_url
    if payload.scheduled_time is not None:
        update_fields["scheduled_time"] = payload.scheduled_time
    if payload.scheduled_date is not None:
        update_fields["scheduled_date"] = payload.scheduled_date
    if payload.roster_status is not None:
        update_fields["roster_status"] = payload.roster_status.upper()
    if payload.status is not None:
        update_fields["status"] = payload.status
    if payload.score_a is not None:
        update_fields["score_a"] = payload.score_a
    if payload.score_b is not None:
        update_fields["score_b"] = payload.score_b
    if payload.winner_team_id is not None:
        update_fields["winner_team_id"] = payload.winner_team_id
    if payload.games is not None:
        update_fields["games"] = payload.games
    if payload.scheduled_at is not None:
        from datetime import datetime
        try:
            dt = datetime.fromisoformat(payload.scheduled_at.replace("Z", "+00:00"))
            update_fields["scheduled_at"] = dt
            update_fields["scheduled_time"] = dt.strftime("%b %d, %I:%M %p")
        except Exception:
            pass
    if payload.lobby_code is not None or payload.room_id is not None:
        val = (payload.room_id if payload.room_id is not None else payload.lobby_code or "").strip()
        update_fields["room_id"] = val if val else None
        update_fields["lobby_code"] = val if val else None
    if payload.lobby_password is not None or payload.room_password is not None:
        val = (payload.room_password if payload.room_password is not None else payload.lobby_password or "").strip()
        update_fields["room_password"] = val if val else None
        update_fields["lobby_password"] = val if val else None
    if payload.server_region is not None:
        update_fields["server_region"] = payload.server_region.strip()
    if payload.custom_instructions is not None:
        c_inst = payload.custom_instructions.strip()
        update_fields["custom_instructions"] = c_inst if c_inst else None
        update_fields["match_message"] = c_inst if c_inst else None

    curr_or_new_stat = (update_fields.get("status") or match.get("status", "")).upper()
    if curr_or_new_stat == "LIVE" or getattr(payload, "credentials_released", False):
        update_fields["credentials_released"] = True
        update_fields["credentials_released_at"] = get_utc_now()
        update_fields["credentials_released_by"] = admin_user.username
        update_fields["join_info_enabled"] = True

    if update_fields:
        old_roster_status = match.get("roster_status", "PUBLISHED")
        new_roster_status = update_fields.get("roster_status", old_roster_status)

        await db.matches.update_one({"_id": match["_id"]}, {"$set": update_fields})

        # Notification handling (Sections 34, 35, 37, 39)
        if old_roster_status == "DRAFT" and new_roster_status == "PUBLISHED":
            try:
                from app.services.match_notification_service import notify_roster_published
                await notify_roster_published(id, db, force=True)
            except Exception as e:
                print(f"[Roster Publication on Update Warning]: {e}")
        elif old_roster_status in ["PUBLISHED", "UPDATED"] and (
            "team_a_id" in update_fields or "team_b_id" in update_fields or
            "scheduled_time" in update_fields or "scheduled_date" in update_fields or "scheduled_at" in update_fields
        ):
            try:
                from app.services.match_notification_service import notify_roster_updated
                prev_details = {
                    "opponent": match.get("team_b_name") or "TBD",
                    "time": match.get("scheduled_time") or "TBD",
                }
                await notify_roster_updated(id, prev_details, db)
            except Exception as e:
                print(f"[Roster Update Notification Warning]: {e}")

        if "scheduled_time" in update_fields or "scheduled_at" in update_fields:
            try:
                from app.services.match_notification_service import invalidate_reminders_on_schedule_change
                new_time_str = update_fields.get("scheduled_time") or str(update_fields.get("scheduled_at", "New Time"))
                await invalidate_reminders_on_schedule_change(id, new_time_str, update_fields.get("scheduled_at"), db)
            except Exception as e:
                print(f"[Match Schedule Invalidation Error]: {e}")

        # If winner is set and match status is COMPLETED, advance bracket
        if update_fields.get("status") == "COMPLETED" or update_fields.get("winner_team_id"):
            winner_id = update_fields.get("winner_team_id") or match.get("winner_team_id")
            if winner_id:
                try:
                    from app.services.bracket_engine import advance_bracket_match
                    refreshed_match_doc = await db.matches.find_one({"_id": match["_id"]})
                    refreshed_match = Match.from_mongo(refreshed_match_doc)
                    await advance_bracket_match(
                        tournament_id=refreshed_match.tournament_id,
                        completed_match=refreshed_match,
                        winner_team_id=winner_id,
                        db=db,
                    )
                except HTTPException:
                    raise
                except Exception as e:
                    print(f"[Bracket Advance Error in update_match_details]: {e}")

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="UPDATE_MATCH_DETAILS",
        target=f"Match {id}",
        details=str(update_fields),
    )

    try:
        from app.services.live_match_service import sync_live_match_for_tournament
        await sync_live_match_for_tournament(str(match.get("tournament_id")), db)
    except Exception as e:
        pass

    return {"status": "success", "message": "Match updated successfully", "updated": update_fields}

@router.delete("/matches/{id}")
async def delete_match_admin(
    id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Removes a match from the tournament.
    """
    match = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    await db.matches.delete_one({"_id": match["_id"]})

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="DELETE_MATCH",
        target=f"Match {id}",
        details=f"Deleted match {match.get('stage')} ({match.get('team_a_id')} vs {match.get('team_b_id')})",
    )

    return {"status": "success", "message": "Match removed successfully"}

@router.post("/notifications/send")
async def send_admin_notification(
    payload: AdminSendNotificationRequest,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Send admin-directed announcements/notifications to targeted users.
    """
    from app.services.notification import create_notification
    from app.core.ws import ws_manager

    sent_count = 0
    for uid in set(payload.user_ids):
        user = await db.users.find_one({"$or": [{"_id": uid}, {"id": uid}]})
        if not user:
            user = await db.users.find_one({"_id": str(uid)})
        if user:
            notif_title = payload.title
            if notif_title.strip().lower() == "response":
                notif_title = "Admin Response"
            await create_notification(
                db=db,
                user_id=u_id_str,
                title=notif_title,
                message=payload.message,
                notification_type=payload.notification_type or "ADMIN",
                category=payload.category,
                sender_role=admin_user.role,
                sender_name=admin_user.username,
                label="Admin Response",
                link=payload.link,
            )
            await ws_manager.broadcast({
                "type": "NOTIFICATION_RECEIVED",
                "user_id": u_id_str,
                "title": notif_title,
                "message": payload.message,
                "link": payload.link,
                "sender_role": admin_user.role,
                "label": "Admin Response",
            })
            sent_count += 1

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="SEND_ADMIN_NOTIFICATION",
        target=f"Targeted {len(payload.user_ids)} users ({sent_count} notified)",
        details=f"Title: {payload.title} | Type: {payload.notification_type}",
    )

    return {
        "status": "success",
        "message": f"Successfully sent notifications to {sent_count} recipient(s)",
        "sent_count": sent_count,
    }

@router.patch("/tournaments/{id}/format")
async def update_tournament_format_admin(
    id: str,
    payload: TournamentFormatUpdatePayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Update tournament format mid-tournament without deleting existing completed matches or registrations.
    Allowed formats: Knockout, Round Robin, Combination, Swiss System, Ladder Tournament.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    fmt_clean = payload.format.strip()
    s = fmt_clean.lower()
    if "battle" in s or "royale" in s:
        match_fmt = "Battle Royale"
    elif "robin" in s:
        match_fmt = "Round Robin"
    else:
        match_fmt = "Knockout"

    await db.tournaments.update_one(
        {"_id": tourn["_id"]},
        {"$set": {
            "format": match_fmt,
            "tournament_type": match_fmt,
            "match_format": match_fmt,
        }}
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="UPDATE_TOURNAMENT_FORMAT",
        target=f"Tournament {tourn.get('title')}",
        details=f"Format changed from {tourn.get('match_format') or tourn.get('format')} to {match_fmt}. Matches preserved.",
    )

    return {
        "status": "success",
        "message": f"Tournament format updated to {match_fmt}. Existing matches preserved.",
        "format": match_fmt,
    }

@router.post("/matches/{id}/mark-live")
async def mark_match_live_admin(
    id: str,
    payload: AdminMarkMatchLivePayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Rules 14 & 15: Admin marks individual match as LIVE.
    - Sets match status = LIVE
    - Stores YouTube Live URL against this specific match
    - Tournament status remains REGISTRATION_CLOSED (Rule 15: Tournament NEVER becomes Live!)
    - Broadcasts live status change to WebSocket ticker
    """
    from app.core.ws import ws_manager

    from bson import ObjectId
    m_query = {"$or": [{"_id": id}, {"id": id}]}
    if ObjectId.is_valid(id):
        m_query["$or"].append({"_id": ObjectId(id)})
    match = await db.matches.find_one(m_query)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    if not payload.youtube_live_url or not payload.youtube_live_url.strip():
        raise HTTPException(status_code=400, detail="A valid YouTube Live URL is required to mark this match as LIVE.")

    yt_url = payload.youtube_live_url.strip()

    await db.matches.update_one(
        {"_id": match["_id"]},
        {"$set": {
            "status": "LIVE",
            "youtube_live_url": yt_url,
            "live_link": yt_url,
        }}
    )

    # Broadcast event
    await ws_manager.broadcast({
        "type": "MATCH_STATUS_CHANGED",
        "match_id": str(match.get("_id", match.get("id"))),
        "tournament_id": match.get("tournament_id"),
        "status": "LIVE",
        "youtube_live_url": yt_url,
    })

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="MARK_MATCH_LIVE",
        target=f"Match {id}",
        details=f"Match is now LIVE on YouTube: {yt_url}",
    )

    return {
        "status": "success",
        "message": "Match marked as LIVE! Stream is now active.",
        "match_id": id,
        "youtube_live_url": yt_url,
    }

@router.post("/matches/{id}/complete")
async def complete_match_admin(
    id: str,
    payload: AdminCompleteMatchPayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Admin completes match, records scores and winner.
    Supports both {winner_team_id, score_a, score_b} and {winner_id, team1_score, team2_score}.
    Resolves custom winner names/placeholders (e.g. "Desert") and verifies registration state.
    """
    from app.services.bracket_engine import advance_bracket_match
    from app.services.match_notification_service import send_match_reminder
    from app.core.ws import ws_manager

    from bson import ObjectId
    m_query = {"$or": [{"_id": id}, {"id": id}]}
    if ObjectId.is_valid(id):
        m_query["$or"].append({"_id": ObjectId(id)})

    match_doc = await db.matches.find_one(m_query)
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")

    match = Match.from_mongo(match_doc)

    # 1. Resolve scores & winner from either payload format
    winner_input = payload.winner_team_id or payload.winner_id
    score_a = payload.score_a if payload.score_a != 0 else (payload.team1_score if payload.team1_score is not None else 0)
    score_b = payload.score_b if payload.score_b != 0 else (payload.team2_score if payload.team2_score is not None else 0)

    # 2. Check payment registration eligibility:
    # Neither competing team should have a REJECTED or CANCELLED payment
    for tid in [match.team_a_id, match.team_b_id]:
        if tid:
            rej_reg = await db.tournament_registrations.find_one({
                "$or": [{"tournament_id": match.tournament_id}, {"tournament_id": str(match.tournament_id)}],
                "$or": [{"team_id": tid}, {"player_id": tid}],
                "payment_status": {"$in": ["REJECTED", "CANCELLED", "REFUNDED"]},
            })
            if rej_reg:
                raise HTTPException(
                    status_code=400,
                    detail=f"Registration or payment for contender {tid} was rejected or cancelled. Cannot finalize match score until payment is resolved.",
                )

    # 3. Resolve winner against IDs, names, or placeholders (e.g. "Desert")
    team_a_name = match.team_a_name or match_doc.get("team_a_name")
    team_b_name = match.team_b_name or match_doc.get("team_b_name")
    team_a_ph = match.team_a_placeholder or match_doc.get("team_a_placeholder")
    team_b_ph = match.team_b_placeholder or match_doc.get("team_b_placeholder")

    winner_id = winner_input
    if winner_input:
        if winner_input in [team_a_name, team_a_ph] and match.team_a_id:
            winner_id = str(match.team_a_id)
        elif winner_input in [team_b_name, team_b_ph] and match.team_b_id:
            winner_id = str(match.team_b_id)

    # Fallback to score comparison if winner not explicitly chosen
    if not winner_id and (score_a != 0 or score_b != 0):
        winner_id = match.team_a_id if score_a >= score_b else match.team_b_id

    # Gather all valid participant target IDs and names
    valid_targets = set()
    if match.team_a_id:
        valid_targets.add(str(match.team_a_id))
    if match.team_b_id:
        valid_targets.add(str(match.team_b_id))
    for t in (match_doc.get("teams") or []):
        if isinstance(t, dict):
            tid = str(t.get("id") or t.get("_id") or t.get("team_id") or "")
            if tid:
                valid_targets.add(tid)
            if t.get("name"):
                valid_targets.add(t["name"])
    for tid in (match_doc.get("team_ids") or []):
        if tid:
            valid_targets.add(str(tid))
    for pid in (match_doc.get("registered_participant_ids") or []):
        if pid:
            valid_targets.add(str(pid))
    for name in [team_a_name, team_b_name, team_a_ph, team_b_ph]:
        if name:
            valid_targets.add(name)

    if winner_id and str(winner_id) not in valid_targets:
        # Check if winner_id is a team ID in db whose name matches team_a or team_b
        winner_team = await db.teams.find_one({"$or": [{"_id": winner_id}, {"id": winner_id}]})
        if winner_team:
            w_name = winner_team.get("name")
            if w_name in valid_targets:
                winner_id = str(winner_team.get("_id", winner_team.get("id")))

    if not winner_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot complete match: a valid winning participant must be declared or determined by scores.",
        )

    winner_id = str(winner_id)
    if valid_targets and winner_id not in valid_targets:
        raise HTTPException(
            status_code=400,
            detail=f"Selected winner '{winner_id}' does not belong to the participants of Match {match.match_number or id}.",
        )

    await db.matches.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {
            "status": "COMPLETED",
            "winner_team_id": winner_id,
            "score_a": score_a,
            "score_b": score_b,
            "captain_notes": payload.captain_notes,
        }}
    )

    match.winner_team_id = winner_id
    match.score_a = score_a
    match.score_b = score_b
    match.status = "COMPLETED"

    # Advance bracket
    try:
        await advance_bracket_match(
            tournament_id=match.tournament_id,
            completed_match=match,
            winner_team_id=winner_id,
            db=db,
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"[Bracket Advance Error]: {e}")

    await ws_manager.broadcast({
        "type": "MATCH_COMPLETED",
        "match_id": match.id,
        "tournament_id": match.tournament_id,
        "winner_team_id": winner_id,
        "score_a": score_a,
        "score_b": score_b,
    })

    # Send match completed notification to participants
    try:
        await send_match_reminder(match.id, "STARTED", db, custom_message=f"Match completed! Final Score: {score_a} - {score_b}.")
    except Exception:
        pass

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="COMPLETE_MATCH",
        target=f"Match {match.id}",
        details=f"Scores: {score_a}-{score_b} | Winner: {winner_id}",
    )

    try:
        from app.services.live_match_service import sync_live_match_for_tournament
        await sync_live_match_for_tournament(str(match.tournament_id), db)
    except Exception as e:
        pass

    return {
        "status": "success",
        "message": f"Match completed successfully! Final score: {score_a} - {score_b}.",
        "match_id": match.id,
        "score_a": score_a,
        "score_b": score_b,
        "winner_id": winner_id,
        "winner_team_id": winner_id,
    }

# Helper to fetch confirmed entrants for Round 1
async def get_tournament_confirmed_entrants(db: AsyncIOMotorDatabase, tourn: dict) -> List[Dict[str, Any]]:
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)
    raw_p_mode = str(tourn.get("participation_mode") or "").upper().strip()
    is_solo = raw_p_mode == "SOLO" or bool(tourn.get("is_solo")) or tourn.get("registration_type") == "SOLO"

    reg_cursor = db.tournament_registrations.find({
        "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
        "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
    })
    regs = await reg_cursor.to_list(length=300)

    contenders: List[Dict[str, Any]] = []
    seen_ids = set()

    for r in regs:
        if is_solo:
            uid = r.get("player_id") or r.get("user_id") or r.get("captain_id")
            if uid and str(uid) not in seen_ids:
                seen_ids.add(str(uid))
                u = await db.users.find_one({"$or": [{"_id": uid}, {"id": uid}]})
                if u:
                    name = u.get("display_name") or u.get("username", "Contender")
                    contenders.append({
                        "id": str(u.get("_id", u.get("id"))),
                        "name": name,
                        "tag": "SOLO",
                        "type": "SOLO",
                        "captain_name": name,
                        "avatar_url": u.get("avatar_url"),
                    })
        else:
            team_id = r.get("team_id")
            if team_id and str(team_id) not in seen_ids:
                seen_ids.add(str(team_id))
                t = await db.teams.find_one({"$or": [{"_id": team_id}, {"id": team_id}]})
                if t:
                    cap_user = await db.users.find_one({"$or": [{"_id": t.get("captain_id")}, {"id": t.get("captain_id")}]}) if t.get("captain_id") else None
                    contenders.append({
                        "id": str(t.get("_id", t.get("id"))),
                        "name": t.get("name"),
                        "tag": t.get("tag", "TEAM"),
                        "type": "TEAM",
                        "captain_name": (cap_user.get("full_name") or cap_user.get("username")) if cap_user else (t.get("captain_handle") or "Captain"),
                        "member_count": len(t.get("members") or []),
                        "logo_url": t.get("logo_url"),
                    })
    return contenders

@router.post("/tournaments/{id}/rounds")
async def add_custom_tournament_round(
    id: str,
    payload: Dict[str, Any],
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Manual Round Creation (Strictly 3 fields: Stage Name, Competition Format, Final Round toggle).
    Inherits Game Mode from tournament.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if tourn.get("status") == "COMPLETED":
        raise HTTPException(status_code=400, detail="Tournament has already been finalized as COMPLETED. No further rounds can be created.")

    clean_name = str(payload.get("stage_name") or payload.get("round_stage_name") or payload.get("round_name") or payload.get("name") or "").strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Round stage name cannot be empty.")

    raw_format = str(payload.get("competition_format") or payload.get("format") or tourn.get("format") or "BATTLE_ROYALE").strip()
    if "robin" in raw_format.lower():
        raise HTTPException(status_code=400, detail="Round Robin format is not supported. Competition format must be either Battle Royale or Knockout.")

    if "battle" in raw_format.lower() or "br" in raw_format.lower() or "royale" in raw_format.lower():
        norm_format = "BATTLE_ROYALE"
    else:
        norm_format = "KNOCKOUT"

    is_final = bool(payload.get("is_final_round", False))
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    # Compute round number
    existing_rounds = await db.tournament_rounds.find({"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]}).sort("round_number", 1).to_list(length=100)
    round_number = len(existing_rounds) + 1

    # Check for duplicate round name in this tournament
    if any(r.get("round_name", "").lower() == clean_name.lower() or r.get("stage_name", "").lower() == clean_name.lower() for r in existing_rounds):
        raise HTTPException(status_code=400, detail=f'A round named "{clean_name}" already exists in this tournament.')

    if round_number == 1:
        eligible_contenders = await get_tournament_confirmed_entrants(db, tourn)
    else:
        prev_round = existing_rounds[-1]
        prev_quals = prev_round.get("qualifier_ids") or []
        if not prev_quals:
            raise HTTPException(status_code=400, detail=f'Cannot create "{clean_name}": preceding round "{prev_round.get("round_name")}" does not have confirmed qualifiers.')
        prev_quals_set = set(str(q) for q in prev_quals)
        eligible_contenders = [c for c in (prev_round.get("eligible_contenders") or []) if str(c.get("id")) in prev_quals_set]

    new_round = TournamentRound(
        tournament_id=tid,
        round_number=round_number,
        round_name=clean_name,
        stage_name=clean_name,
        format=norm_format,
        is_final_round=is_final,
        status="UPCOMING",
        eligible_contenders=eligible_contenders,
    )
    round_doc = new_round.to_mongo()
    await db.tournament_rounds.insert_one(round_doc)

    await db.tournaments.update_one(
        {"_id": tourn["_id"]},
        {"$addToSet": {"custom_rounds": clean_name}}
    )

    round_doc["id"] = str(round_doc.get("_id", round_doc.get("id")))
    existing_custom = list(tourn.get("custom_rounds") or [])
    if clean_name not in existing_custom:
        existing_custom.append(clean_name)

    return {
        "status": "success",
        "message": f'Round "{clean_name}" created successfully.',
        "round": round_doc,
        "round_name": clean_name,
        "custom_rounds": existing_custom,
    }

@router.get("/tournaments/{id}/rounds")
async def get_tournament_rounds_admin(
    id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns all rounds for this tournament with eligible contenders, qualifiers, MVP, and matches.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    rounds_cursor = db.tournament_rounds.find({
        "$or": [{"tournament_id": tid}, {"tournament_id": tslug}]
    }).sort("round_number", 1)
    rounds = await rounds_cursor.to_list(length=100)

    # Enrich each round with matches if format is KNOCKOUT
    enriched_rounds = []
    for r in rounds:
        r_id = str(r.get("_id", r.get("id")))
        r["id"] = r_id
        match_ids = r.get("match_ids") or []
        if match_ids:
            matches_cursor = db.matches.find({"$or": [{"_id": {"$in": match_ids}}, {"id": {"$in": match_ids}}]})
            r["matches"] = await matches_cursor.to_list(length=100)
            for m in r["matches"]:
                m["id"] = str(m.get("_id", m.get("id")))
        else:
            r["matches"] = []
        enriched_rounds.append(r)

    return {
        "status": "success",
        "tournament_id": tid,
        "rounds": enriched_rounds,
        "total_rounds": len(enriched_rounds),
    }

@router.get("/tournaments/{id}/rounds/{round_id}")
async def get_tournament_round_detail_admin(
    id: str,
    round_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns single round detail with its matches and contenders.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    r = await db.tournament_rounds.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [{"_id": round_id}, {"id": round_id}, {"round_name": round_id}, {"stage_name": round_id}]},
        ]
    })
    if not r:
        raise HTTPException(status_code=404, detail="Round not found")

    r_id = str(r.get("_id", r.get("id")))
    r["id"] = r_id
    match_ids = r.get("match_ids") or []
    if match_ids:
        matches_cursor = db.matches.find({"$or": [{"_id": {"$in": match_ids}}, {"id": {"$in": match_ids}}]})
        r["matches"] = await matches_cursor.to_list(length=100)
        for m in r["matches"]:
            m["id"] = str(m.get("_id", m.get("id")))
    else:
        r["matches"] = []

    return {"status": "success", "round": r}

@router.patch("/tournaments/{id}/rounds/{round_id}/credentials")
async def save_round_credentials_admin(
    id: str,
    round_id: str,
    payload: Dict[str, Any],
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Save round credentials (Room ID and optional Room Password).
    Does NOT release credentials (credentials_released stays False).
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    r = await db.tournament_rounds.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [{"_id": round_id}, {"id": round_id}, {"round_name": round_id}, {"stage_name": round_id}]},
        ]
    })
    if not r:
        raise HTTPException(status_code=404, detail="Round not found")

    room_id = str(payload.get("room_id") or "").strip()
    if not room_id:
        raise HTTPException(status_code=400, detail="Room ID is required.")

    room_password = str(payload.get("room_password") or "").strip() or None
    custom_instructions = str(payload.get("custom_instructions") or "").strip() or None

    update_fields = {
        "room_id": room_id,
        "room_password": room_password,
        "custom_instructions": custom_instructions,
        "updated_at": datetime.now(timezone.utc),
    }

    await db.tournament_rounds.update_one({"_id": r["_id"]}, {"$set": update_fields})

    # Sync room_id and room_password to matches in this round if Knockout
    match_ids = r.get("match_ids") or []
    if match_ids:
        await db.matches.update_many(
            {"$or": [{"_id": {"$in": match_ids}}, {"id": {"$in": match_ids}}]},
            {"$set": {"room_id": room_id, "room_password": room_password}}
        )

    return {
        "status": "success",
        "message": "Round credentials saved successfully.",
        "room_id": room_id,
        "room_password": room_password,
        "credentials_released": bool(r.get("credentials_released", False)),
    }

@router.post("/tournaments/{id}/rounds/{round_id}/release-credentials")
async def release_round_credentials_admin(
    id: str,
    round_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Send credentials to all currently eligible participants in this round.
    Sets credentials_released to True.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    r = await db.tournament_rounds.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [{"_id": round_id}, {"id": round_id}, {"round_name": round_id}, {"stage_name": round_id}]},
        ]
    })
    if not r:
        raise HTTPException(status_code=404, detail="Round not found")

    if not r.get("room_id"):
        raise HTTPException(status_code=400, detail="Cannot send credentials: Room ID has not been saved yet.")

    now = datetime.now(timezone.utc)
    await db.tournament_rounds.update_one(
        {"_id": r["_id"]},
        {"$set": {"credentials_released": True, "credentials_released_at": now, "updated_at": now}}
    )

    # Also mark matches in this round
    match_ids = r.get("match_ids") or []
    if match_ids:
        await db.matches.update_many(
            {"$or": [{"_id": {"$in": match_ids}}, {"id": {"$in": match_ids}}]},
            {"$set": {"credentials_released": True, "credentials_released_at": now}}
        )

    # Deliver notifications to all eligible participants
    eligible = r.get("eligible_contenders") or []
    target_user_ids = []
    for c in eligible:
        if c.get("type") == "SOLO":
            target_user_ids.append(c["id"])
        else:
            team_doc = await db.teams.find_one({"$or": [{"_id": c["id"]}, {"id": c["id"]}]})
            if team_doc:
                if team_doc.get("captain_id"):
                    target_user_ids.append(team_doc["captain_id"])
                for m in team_doc.get("members") or []:
                    if m.get("user_id"):
                        target_user_ids.append(m["user_id"])

    target_user_ids = list(set(str(u) for u in target_user_ids if u))
    if target_user_ids:
        notifs = [{
            "user_id": uid,
            "title": f'Match Credentials Released — {r.get("round_name")}',
            "message": f'Room ID and Password are now live for {tourn.get("title")}: {r.get("round_name")}. Enter match room promptly.',
            "type": "MATCH_CREDENTIALS",
            "category": "TOURNAMENT",
            "link": f'/tournaments/{tslug}',
            "is_read": False,
            "created_at": now,
        } for uid in target_user_ids]
        await db.notifications.insert_many(notifs)

        # Dispatch email delivery to participants
        try:
            from app.services.email_service import send_credentials_release_email
            user_docs = await db.users.find({"$or": [{"_id": {"$in": target_user_ids}}, {"id": {"$in": target_user_ids}}]}).to_list(length=100)
            for u in user_docs:
                u_email = u.get("email")
                if u_email:
                    u_name = u.get("display_name") or u.get("username") or "Contender"
                    try:
                        await send_credentials_release_email(
                            to_email=u_email,
                            recipient_name=u_name,
                            tournament_title=tourn.get("title", "Tournament"),
                            round_or_match_name=r.get("round_name", "Match Round"),
                            room_id=r.get("room_id", ""),
                            room_password=r.get("room_password"),
                            custom_instructions=r.get("custom_instructions"),
                            tournament_slug=tslug,
                        )
                    except Exception as email_err:
                        logger.warning(f"Failed to deliver credentials email to {u_email}: {email_err}")
        except Exception as e:
            logger.warning(f"Credentials release email batch notice: {e}")

    # Synchronize tournament broadcast credentials
    await db.tournaments.update_one(
        {"_id": tourn["_id"]},
        {"$set": {
            "custom_room_id": r.get("room_id"),
            "custom_room_password": r.get("room_password"),
            "custom_instructions": r.get("custom_instructions"),
            "credentials_released": True,
            "credentials_released_at": now,
        }}
    )

    from app.services.live_match_service import sync_live_match_for_tournament
    await sync_live_match_for_tournament(tid, db)

    return {
        "status": "success",
        "message": f'Credentials successfully released to {len(eligible)} contenders in round "{r.get("round_name")}".',
        "credentials_released": True,
        "credentials_released_at": now.isoformat(),
        "delivered_to_count": len(target_user_ids),
    }

@router.post("/tournaments/{id}/rounds/{round_id}/go-live")
async def start_round_live_admin(
    id: str,
    round_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Go Live for this round. Hides/disables once live.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    r = await db.tournament_rounds.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [{"_id": round_id}, {"id": round_id}, {"round_name": round_id}, {"stage_name": round_id}]},
        ]
    })
    if not r:
        raise HTTPException(status_code=404, detail="Round not found")

    now = datetime.now(timezone.utc)
    await db.tournament_rounds.update_one(
        {"_id": r["_id"]},
        {"$set": {"status": "LIVE", "started_at": now, "updated_at": now}}
    )

    if tourn.get("status") != "LIVE":
        await db.tournaments.update_one(
            {"_id": tourn["_id"]},
            {"$set": {"status": "LIVE", "updated_at": now}}
        )

    # Sync live status to matches in this round if Knockout
    match_ids = r.get("match_ids") or []
    if match_ids:
        await db.matches.update_many(
            {"$or": [{"_id": {"$in": match_ids}}, {"id": {"$in": match_ids}}]},
            {"$set": {"status": "LIVE"}}
        )

    from app.services.live_match_service import sync_live_match_for_tournament
    await sync_live_match_for_tournament(tid, db)

    return {
        "status": "success",
        "message": f'Round "{r.get("round_name")}" is now LIVE.',
        "round_status": "LIVE",
        "tournament_status": "LIVE",
    }

@router.post("/tournaments/{id}/rounds/{round_id}/qualifiers")
async def set_round_qualifiers_admin(
    id: str,
    round_id: str,
    payload: Dict[str, Any],
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Manual qualifiers selection for Battle Royale.
    Advances saved qualifiers to next round; non-qualifiers marked eliminated.
    If final round: selects exactly ONE champion.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    r = await db.tournament_rounds.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [{"_id": round_id}, {"id": round_id}, {"round_name": round_id}, {"stage_name": round_id}]},
        ]
    })
    if not r:
        raise HTTPException(status_code=404, detail="Round not found")

    eligible = r.get("eligible_contenders") or []
    eligible_id_map = {str(c["id"]): c for c in eligible}

    raw_qualifiers = payload.get("qualifier_ids") or []
    qualifier_ids = [str(q) for q in raw_qualifiers if str(q) in eligible_id_map]

    is_final = bool(r.get("is_final_round"))
    champion_id = payload.get("champion_id")
    champion_name = payload.get("champion_name")

    if is_final:
        if not champion_id:
            if qualifier_ids:
                champion_id = qualifier_ids[0]
            else:
                raise HTTPException(status_code=400, detail="A single tournament champion must be selected for the final round.")
        champion_id = str(champion_id)
        if champion_id not in eligible_id_map:
            raise HTTPException(status_code=400, detail="Champion must be an eligible contender in this round.")
        if not champion_name:
            champion_name = eligible_id_map[champion_id].get("name")
        qualifier_ids = [champion_id]

    eliminated_ids = [cid for cid in eligible_id_map.keys() if cid not in set(qualifier_ids)]

    now = datetime.now(timezone.utc)
    update_data = {
        "qualifier_ids": qualifier_ids,
        "eliminated_ids": eliminated_ids,
        "updated_at": now,
    }
    if is_final and champion_id:
        update_data["champion_id"] = champion_id
        update_data["champion_name"] = champion_name
        is_solo = tourn.get("participation_mode") == "SOLO" or bool(tourn.get("is_solo"))
        champ_obj = {
            "id": champion_id,
            "name": champion_name,
            "type": "PLAYER" if is_solo else "TEAM",
        }
        await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": {"champion": champ_obj, "updated_at": now}})

    await db.tournament_rounds.update_one({"_id": r["_id"]}, {"$set": update_data})

    return {
        "status": "success",
        "message": "Qualifiers updated successfully.",
        "qualifier_ids": qualifier_ids,
        "eliminated_ids": eliminated_ids,
        "champion_id": champion_id,
        "champion_name": champion_name,
    }

@router.post("/tournaments/{id}/rounds/{round_id}/matches")
async def create_knockout_match_admin(
    id: str,
    round_id: str,
    payload: Dict[str, Any],
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Knockout Match Creation with ONLY 3 fields: Match Number, Contenders (A vs B), and Schedule Time.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    r = await db.tournament_rounds.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [{"_id": round_id}, {"id": round_id}, {"round_name": round_id}, {"stage_name": round_id}]},
        ]
    })
    if not r:
        raise HTTPException(status_code=404, detail="Round not found")

    if r.get("format") == "BATTLE_ROYALE":
        raise HTTPException(status_code=400, detail="Battle Royale is a round-level competition and does not use individual matches.")

    match_number = int(payload.get("match_number") or len(r.get("match_ids") or []) + 1)
    team_a_id = str(payload.get("team_a_id") or "").strip()
    team_b_id = str(payload.get("team_b_id") or "").strip()
    scheduled_time = str(payload.get("scheduled_time") or "TBD").strip()

    if not team_a_id or not team_b_id:
        raise HTTPException(status_code=400, detail="Both Contender A and Contender B must be selected.")
    if team_a_id == team_b_id:
        raise HTTPException(status_code=400, detail="Contender A and Contender B cannot be the same.")

    eligible = r.get("eligible_contenders") or []
    eligible_map = {str(c["id"]): c for c in eligible}

    if team_a_id not in eligible_map or team_b_id not in eligible_map:
        raise HTTPException(status_code=400, detail="Selected contenders must belong to this round's eligible contenders.")

    name_a = eligible_map[team_a_id].get("name", "Contender A")
    name_b = eligible_map[team_b_id].get("name", "Contender B")

    is_solo = tourn.get("participation_mode") == "SOLO" or bool(tourn.get("is_solo"))
    now = datetime.now(timezone.utc)

    new_match = Match(
        tournament_id=tid,
        round_name=r.get("round_name"),
        stage_name=r.get("stage_name"),
        round_number=r.get("round_number", 1),
        match_number=match_number,
        team_a_id=team_a_id,
        team_b_id=team_b_id,
        team_a_name=name_a,
        team_b_name=name_b,
        scheduled_time=scheduled_time,
        format="BO1",
        series_format="BO1",
        status="SCHEDULED",
        is_solo=is_solo,
        server_region=tourn.get("server_region", "AWS Mumbai (ap-south-1)"),
        room_id=r.get("room_id"),
        room_password=r.get("room_password"),
        credentials_released=bool(r.get("credentials_released", False)),
    )
    match_doc = new_match.to_mongo()
    await db.matches.insert_one(match_doc)
    m_id = str(match_doc.get("_id", match_doc.get("id")))

    await db.tournament_rounds.update_one(
        {"_id": r["_id"]},
        {"$addToSet": {"match_ids": m_id}, "$set": {"updated_at": now}}
    )

    return {
        "status": "success",
        "message": f"Match #{match_number} created successfully.",
        "match": Match.from_mongo(match_doc).model_dump(),
    }

@router.post("/tournaments/{id}/rounds/{round_id}/matches/{match_id}/result")
async def record_knockout_match_result_admin(
    id: str,
    round_id: str,
    match_id: str,
    payload: Dict[str, Any],
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Score entry and winner selection for a Knockout match.
    Updates round qualifiers and champion if final round.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    r = await db.tournament_rounds.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [{"_id": round_id}, {"id": round_id}, {"round_name": round_id}, {"stage_name": round_id}]},
        ]
    })
    if not r:
        raise HTTPException(status_code=404, detail="Round not found")

    m = await db.matches.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [{"_id": match_id}, {"id": match_id}]},
        ]
    })
    if not m:
        raise HTTPException(status_code=404, detail="Match not found")

    winner_id = str(payload.get("winner_id") or payload.get("winner_team_id") or "").strip()
    score_a = int(payload.get("score_a", 0))
    score_b = int(payload.get("score_b", 0))

    if winner_id not in [str(m.get("team_a_id")), str(m.get("team_b_id"))]:
        raise HTTPException(status_code=400, detail="Winner must be either Contender A or Contender B.")

    now = datetime.now(timezone.utc)
    await db.matches.update_one(
        {"_id": m["_id"]},
        {"$set": {
            "score_a": score_a,
            "score_b": score_b,
            "winner_team_id": winner_id,
            "winner_id": winner_id,
            "status": "COMPLETED",
            "updated_at": now,
        }}
    )

    # Check all matches in round
    match_ids = r.get("match_ids") or []
    all_matches = await db.matches.find({"$or": [{"_id": {"$in": match_ids}}, {"id": {"$in": match_ids}}]}).to_list(length=100)
    all_completed = len(all_matches) > 0 and all(mat.get("status") == "COMPLETED" for mat in all_matches)

    qualifiers = []
    eliminated = []
    champion_id = None
    champion_name = None

    for mat in all_matches:
        w = mat.get("winner_team_id")
        if w:
            qualifiers.append(str(w))
            loser = mat.get("team_b_id") if w == mat.get("team_a_id") else mat.get("team_a_id")
            if loser:
                eliminated.append(str(loser))

    qualifiers = list(dict.fromkeys(qualifiers))
    eliminated = list(dict.fromkeys(eliminated))

    round_updates: Dict[str, Any] = {
        "qualifier_ids": qualifiers,
        "eliminated_ids": eliminated,
        "updated_at": now,
    }
    if all_completed:
        round_updates["status"] = "COMPLETED"

    if r.get("is_final_round") and len(qualifiers) == 1:
        champion_id = qualifiers[0]
        champ_contender = next((c for c in (r.get("eligible_contenders") or []) if str(c["id"]) == champion_id), None)
        champion_name = champ_contender.get("name") if champ_contender else (m.get("team_a_name") if champion_id == m.get("team_a_id") else m.get("team_b_name"))
        round_updates["champion_id"] = champion_id
        round_updates["champion_name"] = champion_name

        is_solo = tourn.get("participation_mode") == "SOLO" or bool(tourn.get("is_solo"))
        await db.tournaments.update_one(
            {"_id": tourn["_id"]},
            {"$set": {
                "champion": {"id": champion_id, "name": champion_name, "type": "PLAYER" if is_solo else "TEAM"},
                "status": "COMPLETED",
                "completed_at": now,
                "updated_at": now,
            }}
        )

    await db.tournament_rounds.update_one({"_id": r["_id"]}, {"$set": round_updates})

    from app.services.live_match_service import sync_live_match_for_tournament
    await sync_live_match_for_tournament(tid, db)

    return {
        "status": "success",
        "message": "Match result saved.",
        "winner_id": winner_id,
        "round_completed": all_completed,
        "qualifier_ids": qualifiers,
        "eliminated_ids": eliminated,
        "champion_id": champion_id,
    }

@router.post("/tournaments/{id}/rounds/{round_id}/mvp")
async def set_round_mvp_admin(
    id: str,
    round_id: str,
    payload: Dict[str, Any],
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Save round MVP selection for an individual player.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    r = await db.tournament_rounds.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [{"_id": round_id}, {"id": round_id}, {"round_name": round_id}, {"stage_name": round_id}]},
        ]
    })
    if not r:
        raise HTTPException(status_code=404, detail="Round not found")

    player_id = str(payload.get("player_id") or "").strip()
    player_name = str(payload.get("player_name") or "").strip()
    stats = str(payload.get("stats") or "").strip() or None
    notes = str(payload.get("notes") or "").strip() or None

    if not player_id or not player_name:
        raise HTTPException(status_code=400, detail="Player ID and Player Name are required for MVP.")

    now = datetime.now(timezone.utc)
    await db.tournament_rounds.update_one(
        {"_id": r["_id"]},
        {"$set": {
            "mvp_player_id": player_id,
            "mvp_player_name": player_name,
            "mvp_stats": stats,
            "mvp_notes": notes,
            "updated_at": now,
        }}
    )

    return {
        "status": "success",
        "message": f'Round MVP saved: {player_name}',
        "mvp_player_id": player_id,
        "mvp_player_name": player_name,
    }

@router.post("/tournaments/{id}/tournament-mvp")
async def set_tournament_mvp_admin(
    id: str,
    payload: Dict[str, Any],
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Save overall Tournament MVP selection.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    player_id = str(payload.get("player_id") or "").strip()
    player_name = str(payload.get("player_name") or "").strip()
    stats = str(payload.get("stats") or "").strip() or None
    notes = str(payload.get("notes") or "").strip() or None

    if not player_id or not player_name:
        raise HTTPException(status_code=400, detail="Player ID and Player Name are required for Tournament MVP.")

    mvp_obj = {
        "player_id": player_id,
        "player_name": player_name,
        "stats": stats,
        "notes": notes,
    }

    now = datetime.now(timezone.utc)
    await db.tournaments.update_one(
        {"_id": tourn["_id"]},
        {"$set": {"tournament_mvp": mvp_obj, "updated_at": now}}
    )

    return {
        "status": "success",
        "message": f"Tournament MVP saved: {player_name}",
        "tournament_mvp": mvp_obj,
    }

@router.post("/tournaments/{id}/final-results")
async def save_final_tournament_results_admin(
    id: str,
    payload: Dict[str, Any],
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Save configurable final tournament results & award recipients.
    Detects conflict if First Place recipient differs from tournament champion.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    results = payload.get("results") or []
    if not isinstance(results, list):
        raise HTTPException(status_code=400, detail="Results must be a list of award rows.")

    champion_id = payload.get("champion_id") or (tourn.get("champion") or {}).get("id")
    champion_name = payload.get("champion_name") or (tourn.get("champion") or {}).get("name")

    # Conflict detection: If an award is for 1st place / champion, verify recipient == champion_id
    has_champion_conflict = False
    for r in results:
        aw = str(r.get("award_name") or "").lower()
        if any(w in aw for w in ["1st", "first", "champion", "winner"]):
            rec_id = str(r.get("recipient_id") or "")
            if champion_id and rec_id and rec_id != str(champion_id):
                has_champion_conflict = True

    display_duration = int(payload.get("results_display_duration_days") or tourn.get("results_display_duration_days", 30))

    now = datetime.now(timezone.utc)
    update_data = {
        "final_results": results,
        "results_display_duration_days": display_duration,
        "updated_at": now,
    }
    if champion_id and champion_name:
        is_solo = tourn.get("participation_mode") == "SOLO" or bool(tourn.get("is_solo"))
        update_data["champion"] = {
            "id": str(champion_id),
            "name": champion_name,
            "type": "PLAYER" if is_solo else "TEAM",
        }

    await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": update_data})

    return {
        "status": "success",
        "message": "Final tournament results saved successfully.",
        "final_results": results,
        "champion_conflict": has_champion_conflict,
    }

@router.post("/tournaments/{id}/rounds/{round_id}/end-round")
async def end_tournament_round_admin(
    id: str,
    round_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Ends round. If final round, requires champion, MVP (if enabled), and final tournament results,
    then finalizes tournament as COMPLETED.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    r = await db.tournament_rounds.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [{"_id": round_id}, {"id": round_id}, {"round_name": round_id}, {"stage_name": round_id}]},
        ]
    })
    if not r:
        raise HTTPException(status_code=404, detail="Round not found")

    # Validate qualifiers / winners
    if r.get("format") == "BATTLE_ROYALE":
        if not r.get("qualifier_ids"):
            raise HTTPException(status_code=400, detail="Cannot end round: qualifiers have not been selected and saved.")
    else:
        match_ids = r.get("match_ids") or []
        if match_ids:
            matches = await db.matches.find({"$or": [{"_id": {"$in": match_ids}}, {"id": {"$in": match_ids}}]}).to_list(length=100)
            pending = [m for m in matches if m.get("status") != "COMPLETED"]
            if pending:
                raise HTTPException(status_code=400, detail=f"Cannot end round: {len(pending)} match(es) are still not completed.")

    # Validate MVP if enabled
    if tourn.get("enable_mvp") and not r.get("mvp_player_id"):
        raise HTTPException(status_code=400, detail="Round MVP must be selected before ending this round.")

    is_final = bool(r.get("is_final_round"))
    now = datetime.now(timezone.utc)

    if is_final:
        if not r.get("champion_id") and not tourn.get("champion"):
            raise HTTPException(status_code=400, detail="Tournament champion must be selected for the final round.")
        if tourn.get("enable_mvp") and not tourn.get("tournament_mvp"):
            raise HTTPException(status_code=400, detail="Tournament MVP must be selected before completing the tournament.")
        if not tourn.get("final_results"):
            raise HTTPException(status_code=400, detail="Final tournament results and award recipients must be configured before ending the tournament.")

        await db.tournament_rounds.update_one(
            {"_id": r["_id"]},
            {"$set": {"status": "COMPLETED", "completed_at": now, "updated_at": now}}
        )
        await db.tournaments.update_one(
            {"_id": tourn["_id"]},
            {"$set": {"status": "COMPLETED", "completed_at": now, "updated_at": now}}
        )

        return {
            "status": "success",
            "message": "Final round closed and tournament marked COMPLETED.",
            "is_final_round": True,
            "tournament_completed": True,
        }
    else:
        await db.tournament_rounds.update_one(
            {"_id": r["_id"]},
            {"$set": {"status": "COMPLETED", "completed_at": now, "updated_at": now}}
        )

        return {
            "status": "success",
            "message": f'Round "{r.get("round_name")}" closed successfully.',
            "is_final_round": False,
            "tournament_completed": False,
        }

@router.patch("/tournaments/{id}/rounds/{round_id}")
async def update_tournament_round_admin(
    id: str,
    round_id: str,
    payload: TournamentRoundUpdatePayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Restore Edit Round action: Allows organizers to update round stage name, format, and final round flag.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    r = await db.tournament_rounds.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [{"_id": round_id}, {"id": round_id}, {"round_name": round_id}, {"stage_name": round_id}]},
        ]
    })
    if not r:
        raise HTTPException(status_code=404, detail="Round not found")

    old_name = r.get("round_name") or r.get("stage_name")
    upd: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}

    new_name = payload.stage_name or payload.round_name or payload.name
    if new_name is not None and new_name.strip():
        clean_name = new_name.strip()
        upd["round_name"] = clean_name
        upd["stage_name"] = clean_name

        if old_name and old_name != clean_name:
            await db.tournaments.update_one(
                {"_id": tourn["_id"]},
                {"$pull": {"custom_rounds": old_name}}
            )
            await db.tournaments.update_one(
                {"_id": tourn["_id"]},
                {"$addToSet": {"custom_rounds": clean_name}}
            )
            # Sync to matches in this round
            await db.matches.update_many(
                {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}], "$or": [{"round_name": old_name}, {"stage": old_name}]},
                {"$set": {"round_name": clean_name, "stage_name": clean_name}}
            )

    if payload.format or payload.competition_format:
        raw_fmt = str(payload.competition_format or payload.format).strip().lower()
        upd["format"] = "BATTLE_ROYALE" if ("battle" in raw_fmt or "br" in raw_fmt) else "KNOCKOUT"

    if payload.is_final_round is not None:
        upd["is_final_round"] = bool(payload.is_final_round)

    if payload.status is not None:
        upd["status"] = payload.status.upper().strip()

    await db.tournament_rounds.update_one({"_id": r["_id"]}, {"$set": upd})

    from app.services.live_match_service import sync_live_match_for_tournament
    await sync_live_match_for_tournament(tid, db)

    updated_round = await db.tournament_rounds.find_one({"_id": r["_id"]})
    if updated_round:
        updated_round["id"] = str(updated_round.get("_id", updated_round.get("id")))

    return {
        "status": "success",
        "message": "Round updated successfully.",
        "round": updated_round,
    }

@router.delete("/tournaments/{id}/rounds/{round_name}")
async def delete_tournament_round_admin(
    id: str,
    round_name: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Safely removes a custom round and its matches with dependency validation.
    Prevents deletion if downstream rounds depend on its results or if round is LIVE.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    target_round = await db.tournament_rounds.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [{"round_name": round_name}, {"stage_name": round_name}, {"_id": round_name}, {"id": round_name}]},
        ]
    })
    if not target_round:
        raise HTTPException(status_code=404, detail="Round not found")

    # Dependency check: cannot delete if subsequent rounds exist
    target_round_num = target_round.get("round_number", 1)
    downstream_rounds = await db.tournament_rounds.find({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"round_number": {"$gt": target_round_num}}
        ]
    }).to_list(length=10)

    if downstream_rounds:
        raise HTTPException(
            status_code=400,
            detail=f'Cannot delete round "{target_round.get("round_name")}": downstream rounds depend on its results. Please delete subsequent rounds first.'
        )

    # Check if round is LIVE
    if target_round.get("status") == "LIVE":
        raise HTTPException(
            status_code=400,
            detail=f'Cannot delete round "{target_round.get("round_name")}" because it is currently LIVE. Please end or cancel the round first.'
        )

    r_actual_name = target_round.get("round_name") or target_round.get("stage_name") or round_name
    await db.tournaments.update_one(
        {"_id": tourn["_id"]},
        {"$pull": {"custom_rounds": r_actual_name}}
    )

    await db.tournament_rounds.delete_one({"_id": target_round["_id"]})

    # Delete matches belonging to this round
    match_ids = target_round.get("match_ids") or []
    await db.matches.delete_many({
        "$or": [
            {"_id": {"$in": match_ids}},
            {"id": {"$in": match_ids}},
            {
                "$and": [
                    {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
                    {"$or": [{"round_name": r_actual_name}, {"stage": r_actual_name}]}
                ]
            }
        ]
    })

    from app.services.live_match_service import sync_live_match_for_tournament
    await sync_live_match_for_tournament(tid, db)

    updated_tourn = await db.tournaments.find_one({"_id": tourn["_id"]})
    return {
        "status": "success",
        "message": f'Round "{r_actual_name}" deleted safely.',
        "custom_rounds": updated_tourn.get("custom_rounds") or [],
    }

@router.patch("/tournaments/{id}/rounds/{round_id}/matches/{match_id}")
async def update_round_match_admin(
    id: str,
    round_id: str,
    match_id: str,
    payload: KnockoutMatchUpdatePayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Restore Edit Match action: Update knockout match participants, schedule time, and room details.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    r = await db.tournament_rounds.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [{"_id": round_id}, {"id": round_id}, {"round_name": round_id}, {"stage_name": round_id}]},
        ]
    })
    if not r:
        raise HTTPException(status_code=404, detail="Round not found")

    m = await db.matches.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [{"_id": match_id}, {"id": match_id}]},
        ]
    })
    if not m:
        raise HTTPException(status_code=404, detail="Match not found")

    upd: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    if payload.match_number is not None:
        upd["match_number"] = int(payload.match_number)
    if payload.scheduled_time is not None:
        upd["scheduled_time"] = payload.scheduled_time.strip()

    eligible = r.get("eligible_contenders") or []
    eligible_map = {str(c["id"]): c for c in eligible}

    if payload.team_a_id is not None:
        aid = payload.team_a_id.strip()
        upd["team_a_id"] = aid
        if aid in eligible_map:
            upd["team_a_name"] = eligible_map[aid].get("name", "Contender A")
    if payload.team_b_id is not None:
        bid = payload.team_b_id.strip()
        upd["team_b_id"] = bid
        if bid in eligible_map:
            upd["team_b_name"] = eligible_map[bid].get("name", "Contender B")

    if payload.room_id is not None:
        upd["room_id"] = payload.room_id.strip()
    if payload.room_password is not None:
        upd["room_password"] = payload.room_password.strip() or None
    if payload.custom_instructions is not None:
        upd["custom_instructions"] = payload.custom_instructions.strip() or None
    if payload.status is not None:
        upd["status"] = payload.status.upper().strip()

    await db.matches.update_one({"_id": m["_id"]}, {"$set": upd})

    from app.services.live_match_service import sync_live_match_for_tournament
    await sync_live_match_for_tournament(tid, db)

    updated_m = await db.matches.find_one({"_id": m["_id"]})
    if updated_m:
        updated_m["id"] = str(updated_m.get("_id", updated_m.get("id")))

    return {
        "status": "success",
        "message": "Match updated successfully.",
        "match": updated_m,
    }

@router.delete("/tournaments/{id}/rounds/{round_id}/matches/{match_id}")
async def delete_round_match_admin(
    id: str,
    round_id: str,
    match_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Safely deletes an individual match from a round.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    r = await db.tournament_rounds.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [{"_id": round_id}, {"id": round_id}, {"round_name": round_id}, {"stage_name": round_id}]},
        ]
    })
    if not r:
        raise HTTPException(status_code=404, detail="Round not found")

    m = await db.matches.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [{"_id": match_id}, {"id": match_id}]},
        ]
    })
    if not m:
        raise HTTPException(status_code=404, detail="Match not found")

    if m.get("status") == "COMPLETED" and m.get("winner_team_id"):
        w_id = m.get("winner_team_id")
        subsequent_match = await db.matches.find_one({
            "$and": [
                {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
                {"_id": {"$ne": m["_id"]}},
                {"round_number": {"$gt": m.get("round_number", 1)}},
                {"$or": [{"team_a_id": w_id}, {"team_b_id": w_id}]}
            ]
        })
        if subsequent_match:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete this completed match: its winner is already assigned in a downstream round."
            )

    mid_str = str(m.get("_id", m.get("id")))
    await db.tournament_rounds.update_one(
        {"_id": r["_id"]},
        {"$pull": {"match_ids": mid_str}}
    )
    await db.matches.delete_one({"_id": m["_id"]})

    from app.services.live_match_service import sync_live_match_for_tournament
    await sync_live_match_for_tournament(tid, db)

    return {
        "status": "success",
        "message": f"Match #{m.get('match_number')} deleted successfully.",
    }

@router.post("/tournaments/{id}/rounds/{round_id}/matches/{match_id}/complete")
async def complete_round_match_admin(
    id: str,
    round_id: str,
    match_id: str,
    payload: Dict[str, Any],
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Restore Complete Match action across any round (Round 1, intermediate, final).
    """
    return await record_knockout_match_result_admin(id, round_id, match_id, payload, admin_user, db)

@router.patch("/tournaments/{id}/rounds/{round_id}/matches/{match_id}/credentials")
async def save_knockout_match_credentials_admin(
    id: str,
    round_id: str,
    match_id: str,
    payload: Dict[str, Any],
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Requirement 11: Organizer enters room credentials for a specific knockout match.
    Persists room_id, room_password, custom_instructions in confidential state.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    from bson import ObjectId
    match_query = [{"_id": match_id}, {"id": match_id}]
    if ObjectId.is_valid(match_id):
        match_query.append({"_id": ObjectId(match_id)})

    m = await db.matches.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": match_query},
        ]
    })
    if not m:
        raise HTTPException(status_code=404, detail="Match not found")

    room_id = str(payload.get("room_id") or "").strip()
    if not room_id:
        raise HTTPException(status_code=400, detail="Room ID is required to save credentials.")

    room_password = str(payload.get("room_password") or "").strip() or None
    custom_instructions = str(payload.get("custom_instructions") or "").strip() or None

    await db.matches.update_one(
        {"_id": m["_id"]},
        {"$set": {
            "room_id": room_id,
            "room_password": room_password,
            "custom_instructions": custom_instructions,
            "updated_at": datetime.now(timezone.utc),
        }}
    )

    from app.services.live_match_service import sync_live_match_for_tournament
    await sync_live_match_for_tournament(tid, db)

    return {
        "status": "success",
        "message": f"Credentials saved for Match #{m.get('match_number')}.",
        "match_id": match_id,
        "room_id": room_id,
        "room_password": room_password,
        "credentials_released": bool(m.get("credentials_released", False)),
    }

@router.post("/tournaments/{id}/rounds/{round_id}/matches/{match_id}/release-credentials")
async def release_knockout_match_credentials_admin(
    id: str,
    round_id: str,
    match_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Requirement 11 & 7: Release room credentials for a knockout match.
    Persists release status, creates in-app notifications, and dispatches email to contenders.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    from bson import ObjectId
    match_query = [{"_id": match_id}, {"id": match_id}]
    if ObjectId.is_valid(match_id):
        match_query.append({"_id": ObjectId(match_id)})

    m = await db.matches.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": match_query},
        ]
    })
    if not m:
        raise HTTPException(status_code=404, detail="Match not found")

    if not m.get("room_id"):
        raise HTTPException(status_code=400, detail="Cannot send credentials: Room ID has not been entered.")

    now = datetime.now(timezone.utc)
    await db.matches.update_one(
        {"_id": m["_id"]},
        {"$set": {
            "credentials_released": True,
            "credentials_released_at": now,
            "credentials_released_by": admin_user.username,
            "updated_at": now,
        }}
    )

    # Deliver notifications and email to both contenders
    target_user_ids = []
    contender_ids = [m.get("team_a_id"), m.get("team_b_id")]
    for cid in contender_ids:
        if not cid:
            continue
        c_str = str(cid)
        # Check if user directly (solo)
        user_doc = await db.users.find_one({"$or": [{"_id": c_str}, {"id": c_str}]})
        if user_doc:
            target_user_ids.append(c_str)
        else:
            team_doc = await db.teams.find_one({"$or": [{"_id": c_str}, {"id": c_str}]})
            if team_doc:
                if team_doc.get("captain_id"):
                    target_user_ids.append(team_doc["captain_id"])
                for member in team_doc.get("members") or []:
                    if member.get("user_id"):
                        target_user_ids.append(member["user_id"])

    target_user_ids = list(set(str(u) for u in target_user_ids if u))
    if target_user_ids:
        notifs = [{
            "user_id": uid,
            "title": f'Match Credentials Released — Match #{m.get("match_number")}',
            "message": f'Room ID and Password are now live for {tourn.get("title")} (Match #{m.get("match_number")}). Enter match room promptly.',
            "type": "MATCH_CREDENTIALS",
            "category": "TOURNAMENT",
            "link": f'/tournaments/{tslug}',
            "is_read": False,
            "created_at": now,
        } for uid in target_user_ids]
        await db.notifications.insert_many(notifs)

        try:
            from app.services.email_service import send_credentials_release_email
            user_docs = await db.users.find({"$or": [{"_id": {"$in": target_user_ids}}, {"id": {"$in": target_user_ids}}]}).to_list(length=20)
            for u in user_docs:
                u_email = u.get("email")
                if u_email:
                    u_name = u.get("display_name") or u.get("username") or "Contender"
                    try:
                        await send_credentials_release_email(
                            to_email=u_email,
                            recipient_name=u_name,
                            tournament_title=tourn.get("title", "Tournament"),
                            round_or_match_name=f'Match #{m.get("match_number")}',
                            room_id=m.get("room_id", ""),
                            room_password=m.get("room_password"),
                            custom_instructions=m.get("custom_instructions"),
                            tournament_slug=tslug,
                        )
                    except Exception as email_err:
                        logger.warning(f"Failed to deliver match credentials email to {u_email}: {email_err}")
        except Exception as e:
            logger.warning(f"Match credentials email batch notice: {e}")

    from app.services.live_match_service import sync_live_match_for_tournament
    await sync_live_match_for_tournament(tid, db)

    return {
        "status": "success",
        "message": f"Credentials released for Match #{m.get('match_number')}.",
        "credentials_released": True,
        "credentials_released_at": now.isoformat(),
        "delivered_to_count": len(target_user_ids),
    }

@router.patch("/tournaments/{id}/community")
async def update_tournament_community(
    id: str,
    payload: TournamentCommunityUpdatePayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Rule 7: Admin configures one optional community platform (Discord OR WhatsApp) and 1 invite link.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    upd_fields: Dict[str, Any] = {}
    if payload.whatsapp_group_url is not None:
        upd_fields["whatsapp_group_url"] = payload.whatsapp_group_url.strip() or None
    if payload.discord_url is not None:
        upd_fields["discord_url"] = payload.discord_url.strip() or None

    platform = payload.community_platform
    if platform and platform.upper() in ["DISCORD", "WHATSAPP"]:
        platform = platform.upper()
    else:
        platform = None

    if payload.community_link is not None:
        link = payload.community_link.strip() or None
        upd_fields["community_link"] = link
        if platform == "WHATSAPP" or "whatsapp" in (link or "").lower():
            if "whatsapp_group_url" not in upd_fields:
                upd_fields["whatsapp_group_url"] = link
        elif platform == "DISCORD" or "discord" in (link or "").lower():
            if "discord_url" not in upd_fields:
                upd_fields["discord_url"] = link
    if platform is not None:
        upd_fields["community_platform"] = platform

    if upd_fields:
        await db.tournaments.update_one(
            {"_id": tourn["_id"]},
            {"$set": upd_fields}
        )

    updated_t = await db.tournaments.find_one({"_id": tourn["_id"]})
    return {
        "status": "success",
        "message": "Tournament community settings updated successfully.",
        "community_platform": updated_t.get("community_platform"),
        "community_link": updated_t.get("community_link"),
        "whatsapp_group_url": updated_t.get("whatsapp_group_url"),
        "discord_url": updated_t.get("discord_url"),
    }

@router.patch("/matches/{id}/join-info")
async def update_match_join_info(
    id: str,
    payload: MatchJoinInfoUpdatePayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Rules 10, 11, 12, 17: Admin configures optional game-specific join credentials
    (Join Code, Party ID, Room ID, Lobby ID, Password, Join Link) and match instructions.
    """
    from app.services.match_notification_service import notify_join_info_updated

    match_doc = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")

    await db.matches.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {
            "join_info_enabled": payload.join_info_enabled,
            "join_info": payload.join_info,
            "match_message": payload.match_message,
        }}
    )

    if payload.join_info_enabled and payload.join_info:
        await notify_join_info_updated(id, db)

    return {
        "status": "success",
        "message": "Match join information updated successfully.",
        "match_id": id,
        "join_info_enabled": payload.join_info_enabled,
    }

@router.post("/matches/{id}/send-reminder")
async def trigger_match_reminder_admin(
    id: str,
    payload: MatchReminderRequest,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Rules 13, 14, 15, 16: Admin triggers or schedules countdown reminder for match participants.
    """
    from app.services.match_notification_service import send_match_reminder

    res = await send_match_reminder(
        match_id=id,
        reminder_type=payload.reminder_type,
        db=db,
        custom_message=payload.custom_message,
        force=False,
    )
    if res.get("status") == "skipped":
        res["skipped"] = True
    return res


# ----------------- WALLET AUDIT LOGS -----------------

@router.get("/wallet-audit-logs")
async def get_wallet_audit_logs(
    search: Optional[str] = None,
    txn_type: Optional[str] = None,
    is_archived: bool = False,
    limit: int = 100,
    skip: int = 0,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns aggregate wallet system metrics and searchable/filterable transaction audit ledger.
    Handles negative net values correctly without truncating.
    """
    from bson import ObjectId

    # Aggregate total wallet balances across all users
    user_cursor = db.users.find({"is_dummy": {"$ne": True}})
    all_users = await user_cursor.to_list(length=2000)

    total_system_balance_paise = 0
    total_users_count = len(all_users)
    user_map = {}
    for u in all_users:
        u_id = str(u.get("_id", u.get("id")))
        user_map[u_id] = u
        bal_paise = int(u.get("inr_balance_paise", 0))
        if bal_paise == 0 and u.get("inr_balance"):
            try:
                bal_paise = int(float(u["inr_balance"]) * 100)
            except Exception:
                bal_paise = 0
        total_system_balance_paise += bal_paise

    # Build ledger query
    ledger_query: Dict[str, Any] = {"is_dummy": {"$ne": True}}
    if is_archived:
        ledger_query["is_archived"] = True
    else:
        ledger_query["is_archived"] = {"$ne": True}

    if txn_type and txn_type != "ALL":
        ledger_query["txn_type"] = txn_type

    if search and search.strip():
        s = search.strip()
        matched_user_ids = []
        for u_id, u in user_map.items():
            uname = (u.get("username") or "").lower()
            email = (u.get("email") or "").lower()
            dname = (u.get("display_name") or "").lower()
            if s.lower() in uname or s.lower() in email or s.lower() in dname or s in u_id:
                matched_user_ids.append(u_id)

        search_or: List[Dict[str, Any]] = [
            {"description": {"$regex": s, "$options": "i"}},
            {"reference_id": {"$regex": s, "$options": "i"}},
            {"admin_id": {"$regex": s, "$options": "i"}},
            {"user_id": {"$regex": s, "$options": "i"}},
        ]
        if matched_user_ids:
            search_or.append({"user_id": {"$in": matched_user_ids}})
        ledger_query["$or"] = search_or

    total_txns = await db.wallet_ledger.count_documents(ledger_query)
    cursor = db.wallet_ledger.find(ledger_query).sort("created_at", -1).skip(skip).limit(limit)
    raw_txns = await cursor.to_list(length=limit)

    txns = []
    total_credited_paise = 0
    total_debited_paise = 0

    for t in raw_txns:
        t_id = str(t.get("_id", t.get("id")))
        u_id = str(t.get("user_id"))
        u_info = user_map.get(u_id)
        amt_paise = t.get("amount_paise", 0)
        if amt_paise > 0:
            total_credited_paise += amt_paise
        else:
            total_debited_paise += abs(amt_paise)

        txns.append({
            "id": t_id,
            "user_id": u_id,
            "username": u_info.get("username", "Unknown") if u_info else "Unknown",
            "user_email": u_info.get("email", "") if u_info else "",
            "txn_type": t.get("txn_type"),
            "amount_paise": amt_paise,
            "amount_inr": amt_paise / 100,
            "balance_before_paise": t.get("balance_before_paise", 0),
            "balance_before_inr": (t.get("balance_before_paise", 0)) / 100,
            "balance_after_paise": t.get("balance_after_paise", 0),
            "balance_after_inr": (t.get("balance_after_paise", 0)) / 100,
            "reference_id": t.get("reference_id"),
            "description": t.get("description", ""),
            "admin_id": t.get("admin_id"),
            "is_archived": bool(t.get("is_archived", False)),
            "archived_at": t.get("archived_at").isoformat() if hasattr(t.get("archived_at"), "isoformat") else str(t.get("archived_at") or ""),
            "archived_by": t.get("archived_by"),
            "created_at": str(t.get("created_at")),
        })

    return {
        "status": "success",
        "system_aggregate_balance": total_system_balance_paise / 100,
        "total_users": total_users_count,
        "total_transactions": total_txns,
        "total_credited_inr": total_credited_paise / 100,
        "total_debited_inr": total_debited_paise / 100,
        "transactions": txns,
    }


# ----------------- DYNAMIC STAGES & ROUNDS MANAGEMENT -----------------

@router.get("/tournaments/{tournament_id}/stages")
async def get_tournament_stages(
    tournament_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from bson import ObjectId
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})
    t_doc = await db.tournaments.find_one(t_query)
    if not t_doc:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tournament = Tournament.from_mongo(t_doc)
    stages = [s.model_dump() for s in (tournament.stages or [])]
    stages = sorted(stages, key=lambda s: s.get("stage_order", 0))
    return {
        "status": "success",
        "stages": stages,
        "live_stream_url": tournament.live_stream_url,
    }


@router.post("/tournaments/{tournament_id}/stages")
async def create_tournament_stage(
    tournament_id: str,
    payload: TournamentStageCreate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    import uuid
    from bson import ObjectId
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})
    t_doc = await db.tournaments.find_one(t_query)
    if not t_doc:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tournament = Tournament.from_mongo(t_doc)
    stages = tournament.stages or []
    stage_id = f"stage_{uuid.uuid4().hex[:8]}"
    stage_order = len(stages) + 1

    new_stage = TournamentStage(
        stage_id=stage_id,
        stage_order=stage_order,
        stage_name=payload.name,
        stage_format=payload.format,
        series_format=payload.series_format,
        advancing_teams_count=payload.advancing_teams_count,
        status="UPCOMING",
        rounds=[],
    )

    await db.tournaments.update_one(
        {"_id": t_doc["_id"]},
        {"$push": {"stages": new_stage.to_mongo()}}
    )

    return {"status": "success", "message": "Stage created successfully", "stage": new_stage.model_dump()}


@router.patch("/tournaments/{tournament_id}/stages/{stage_id}")
async def update_tournament_stage(
    tournament_id: str,
    stage_id: str,
    payload: TournamentStageUpdate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from bson import ObjectId
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})
    t_doc = await db.tournaments.find_one(t_query)
    if not t_doc:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tournament = Tournament.from_mongo(t_doc)
    stages = tournament.stages or []
    found = False
    for s in stages:
        if s.stage_id == stage_id:
            found = True
            if payload.name is not None:
                s.stage_name = payload.name
            if payload.format is not None:
                s.stage_format = payload.format
            if payload.series_format is not None:
                s.series_format = payload.series_format
            if payload.advancing_teams_count is not None:
                s.advancing_teams_count = payload.advancing_teams_count
            if payload.status is not None:
                s.status = payload.status
            break

    if not found:
        raise HTTPException(status_code=404, detail="Stage not found")

    await db.tournaments.update_one(
        {"_id": t_doc["_id"]},
        {"$set": {"stages": [s.to_mongo() for s in stages]}}
    )

    return {"status": "success", "message": "Stage updated successfully"}


@router.delete("/tournaments/{tournament_id}/stages/{stage_id}")
async def delete_tournament_stage(
    tournament_id: str,
    stage_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from bson import ObjectId
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})
    t_doc = await db.tournaments.find_one(t_query)
    if not t_doc:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tournament = Tournament.from_mongo(t_doc)
    stages = [s for s in (tournament.stages or []) if s.stage_id != stage_id]
    for idx, s in enumerate(stages):
        s.stage_order = idx + 1

    await db.tournaments.update_one(
        {"_id": t_doc["_id"]},
        {"$set": {"stages": [s.to_mongo() for s in stages]}}
    )

    await db.matches.delete_many({
        "tournament_id": tournament_id,
        "stage_id": stage_id,
    })

    return {"status": "success", "message": "Stage and associated matches deleted"}


@router.post("/tournaments/{tournament_id}/stages/reorder")
async def reorder_tournament_stages(
    tournament_id: str,
    payload: StageReorderPayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from bson import ObjectId
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})
    t_doc = await db.tournaments.find_one(t_query)
    if not t_doc:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tournament = Tournament.from_mongo(t_doc)
    existing_stages = {s.stage_id: s for s in (tournament.stages or [])}

    reordered: List[TournamentStage] = []
    for idx, s_id in enumerate(payload.stage_ids):
        if s_id in existing_stages:
            st = existing_stages[s_id]
            st.stage_order = idx + 1
            reordered.append(st)

    for s_id, st in existing_stages.items():
        if s_id not in payload.stage_ids:
            st.stage_order = len(reordered) + 1
            reordered.append(st)

    await db.tournaments.update_one(
        {"_id": t_doc["_id"]},
        {"$set": {"stages": [s.to_mongo() for s in reordered]}}
    )

    return {"status": "success", "message": "Stages reordered successfully"}


@router.post("/tournaments/{tournament_id}/stages/{stage_id}/rounds")
async def create_stage_round(
    tournament_id: str,
    stage_id: str,
    payload: StageRoundCreate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    import uuid
    from bson import ObjectId
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})
    t_doc = await db.tournaments.find_one(t_query)
    if not t_doc:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tournament = Tournament.from_mongo(t_doc)
    stages = tournament.stages or []
    target_stage = next((s for s in stages if s.stage_id == stage_id), None)
    if not target_stage:
        raise HTTPException(status_code=404, detail="Stage not found")

    rounds = target_stage.rounds or []
    round_id = f"round_{uuid.uuid4().hex[:8]}"
    round_order = payload.round_number or (len(rounds) + 1)

    new_round = StageRound(
        round_id=round_id,
        round_order=round_order,
        round_name=payload.name,
        series_format=payload.series_format or target_stage.series_format or "BO1",
        scheduled_date=payload.scheduled_date,
        scheduled_time=payload.scheduled_time,
        status="UPCOMING",
    )
    rounds.append(new_round)
    target_stage.rounds = rounds

    await db.tournaments.update_one(
        {"_id": t_doc["_id"]},
        {"$set": {"stages": [s.to_mongo() for s in stages]}}
    )

    return {"status": "success", "message": "Round created successfully", "round": new_round.model_dump()}


@router.patch("/tournaments/{tournament_id}/stages/{stage_id}/rounds/{round_id}")
async def update_stage_round(
    tournament_id: str,
    stage_id: str,
    round_id: str,
    payload: StageRoundUpdate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from bson import ObjectId
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})
    t_doc = await db.tournaments.find_one(t_query)
    if not t_doc:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tournament = Tournament.from_mongo(t_doc)
    stages = tournament.stages or []
    target_stage = next((s for s in stages if s.stage_id == stage_id), None)
    if not target_stage:
        raise HTTPException(status_code=404, detail="Stage not found")

    target_round = next((r for r in (target_stage.rounds or []) if r.round_id == round_id), None)
    if not target_round:
        raise HTTPException(status_code=404, detail="Round not found")

    if payload.name is not None:
        target_round.round_name = payload.name
    if payload.status is not None:
        target_round.status = payload.status
    if payload.series_format is not None:
        target_round.series_format = payload.series_format
    if payload.scheduled_date is not None:
        target_round.scheduled_date = payload.scheduled_date
    if payload.scheduled_time is not None:
        target_round.scheduled_time = payload.scheduled_time

    await db.tournaments.update_one(
        {"_id": t_doc["_id"]},
        {"$set": {"stages": [s.to_mongo() for s in stages]}}
    )

    return {"status": "success", "message": "Round updated successfully"}


@router.delete("/tournaments/{tournament_id}/stages/{stage_id}/rounds/{round_id}")
async def delete_stage_round(
    tournament_id: str,
    stage_id: str,
    round_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from bson import ObjectId
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})
    t_doc = await db.tournaments.find_one(t_query)
    if not t_doc:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tournament = Tournament.from_mongo(t_doc)
    stages = tournament.stages or []
    target_stage = next((s for s in stages if s.stage_id == stage_id), None)
    if not target_stage:
        raise HTTPException(status_code=404, detail="Stage not found")

    target_stage.rounds = [r for r in (target_stage.rounds or []) if r.round_id != round_id]

    await db.tournaments.update_one(
        {"_id": t_doc["_id"]},
        {"$set": {"stages": [s.to_mongo() for s in stages]}}
    )

    await db.matches.delete_many({
        "tournament_id": tournament_id,
        "stage_id": stage_id,
        "round_id": round_id,
    })

    return {"status": "success", "message": "Round and its matches deleted"}


@router.post("/tournaments/{tournament_id}/stages/{stage_id}/rounds/{round_id}/matches")
async def create_stage_match(
    tournament_id: str,
    stage_id: str,
    round_id: str,
    payload: StageMatchCreate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from bson import ObjectId
    import secrets
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})
    t_doc = await db.tournaments.find_one(t_query)
    if not t_doc:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tournament = Tournament.from_mongo(t_doc)
    target_stage = next((s for s in (tournament.stages or []) if s.stage_id == stage_id), None)
    stage_name = target_stage.stage_name if target_stage else "Stage"
    target_round = next((r for r in (target_stage.rounds or []) if r.round_id == round_id), None) if target_stage else None
    round_name = target_round.round_name if target_round else "Round"
    round_order = target_round.round_order if target_round else 1

    match_count = await db.matches.count_documents({
        "tournament_id": tournament_id,
        "stage_id": stage_id,
        "round_id": round_id,
    })
    match_number = payload.match_number or (match_count + 1)

    team_a_name = payload.team_a_name
    if payload.team_a_id and not team_a_name:
        ta_doc = await db.teams.find_one({"$or": [{"_id": payload.team_a_id}, {"id": payload.team_a_id}]})
        if ta_doc:
            team_a_name = ta_doc.get("name")

    team_b_name = payload.team_b_name
    if payload.team_b_id and not team_b_name:
        tb_doc = await db.teams.find_one({"$or": [{"_id": payload.team_b_id}, {"id": payload.team_b_id}]})
        if tb_doc:
            team_b_name = tb_doc.get("name")

    match_stream = payload.youtube_live_url or tournament.live_stream_url

    new_match = Match(
        tournament_id=tournament.id,
        stage_id=stage_id,
        stage_name=stage_name,
        round_id=round_id,
        round_name=round_name,
        round_number=round_order,
        match_number=match_number,
        team_a_id=payload.team_a_id,
        team_b_id=payload.team_b_id,
        team_a_name=team_a_name,
        team_b_name=team_b_name,
        format=payload.series_format,
        series_format=payload.series_format,
        scheduled_date=payload.scheduled_date,
        scheduled_time=payload.scheduled_time or f"{payload.scheduled_date or ''}",
        youtube_live_url=match_stream,
        status="SCHEDULED",
        server_region=tournament.server_region,
        lobby_code=f"AMT-M{match_number}",
        lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
    )

    await db.matches.insert_one(new_match.to_mongo())
    return {"status": "success", "message": "Match created", "match": new_match.model_dump()}


@router.post("/matches/{match_id}/swap")
async def swap_teams(
    match_id: str,
    payload: Optional[MatchSwapPayload] = None,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    result = await swap_match_teams(match_id, db)
    return {"status": "success", "message": "Teams swapped successfully", "match": result}


@router.get("/matches/{match_id}/advancement-options")
async def get_match_advancement_options(
    match_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    return await calculate_advancement_suggestion(match_id, db)


@router.post("/matches/{match_id}/advance")
async def advance_match_winner_endpoint(
    match_id: str,
    payload: MatchAdvancementPayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    return await advance_match_winner(match_id, payload.model_dump(), db)


@router.post("/matches/{match_id}/revert-advancement")
async def revert_match_advancement_endpoint(
    match_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    return await revert_match_advancement(match_id, db)


@router.post("/matches/{id}/publish-roster")
async def publish_match_roster_endpoint(
    id: str,
    payload: Optional[AdminPublishRosterPayload] = None,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Sections 38 & 39:
    Admin finalizes and publishes a match roster.
    Sets roster_status = 'PUBLISHED', makes it available to live matches,
    and notifies affected Team Leaders via in-app notification & branded email.
    """
    from app.services.match_notification_service import notify_roster_published
    from bson import ObjectId
    m_query = {"$or": [{"_id": id}, {"id": id}]}
    if ObjectId.is_valid(id):
        m_query["$or"].append({"_id": ObjectId(id)})
    match = await db.matches.find_one(m_query)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    await db.matches.update_one(
        {"_id": match["_id"]},
        {"$set": {"roster_status": "PUBLISHED"}}
    )

    result = await notify_roster_published(id, db, force=True)

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="PUBLISH_ROSTER",
        target=f"Match {id}",
        details=f"Notified {result.get('notified_count', 0)} captains",
    )

    return {
        "status": "success",
        "message": f"Roster published and {result.get('notified_count', 0)} team leaders notified.",
        "match_id": id,
        "roster_status": "PUBLISHED",
        "notified_count": result.get("notified_count", 0),
    }


@router.post("/tournaments/{tournament_id}/publish-all-rosters")
async def publish_all_tournament_rosters_endpoint(
    tournament_id: str,
    round_name: Optional[str] = None,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Sections 38 & 39:
    Batch publishes all draft match rosters for a given tournament or round.
    """
    from app.services.match_notification_service import notify_roster_published
    tourn = await db.tournaments.find_one({"$or": [{"_id": tournament_id}, {"id": tournament_id}, {"slug": tournament_id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))

    q: Dict[str, Any] = {
        "$or": [{"tournament_id": tid}, {"tournament_id": tourn.get("slug")}],
        "roster_status": "DRAFT",
    }
    if round_name and round_name != "ALL":
        q["$or"] = [
            {"round_name": round_name},
            {"stage": round_name},
        ]
        q["tournament_id"] = {"$in": [tid, tourn.get("slug")]}

    cursor = db.matches.find(q)
    matches = await cursor.to_list(length=100)
    published_count = 0
    notified_total = 0

    for m in matches:
        mid = str(m.get("_id", m.get("id")))
        await db.matches.update_one({"_id": m["_id"]}, {"$set": {"roster_status": "PUBLISHED"}})
        res = await notify_roster_published(mid, db, force=True)
        published_count += 1
        notified_total += res.get("notified_count", 0)

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="PUBLISH_ALL_ROSTERS",
        target=f"Tournament {tid}",
        details=f"Published {published_count} matches, notified {notified_total} leaders",
    )

    return {
        "status": "success",
        "message": f"Successfully published {published_count} match rosters! Delivered {notified_total} notifications.",
        "published_matches": published_count,
        "notified_leaders": notified_total,
    }


@router.get("/tournaments/{tournament_id}/rounds-rosters")
async def get_tournament_rounds_rosters(
    tournament_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Sections 26, 27, 38, 39:
    Admin Roster & Match Formation Engine endpoint.
    Returns:
    - Tournament details
    - Registered rosters (confirmed teams/solo entrants)
    - All matches grouped by round
    - For each match: participants, order, schedule, draft/published status, and advancement links
    """
    from bson import ObjectId
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}, {"slug": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})
    tourn = await db.tournaments.find_one(t_query)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug")

    # 1. Fetch all confirmed registered rosters for this tournament
    reg_cursor = db.tournament_registrations.find({
        "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
        "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
    })
    regs = await reg_cursor.to_list(length=250)

    registered_teams = []
    registered_ids = set()
    for r in regs:
        team_id = r.get("team_id")
        user_id = r.get("player_id") or r.get("user_id") or r.get("captain_id")
        if team_id:
            registered_ids.add(str(team_id))
            t_doc = await db.teams.find_one({"$or": [{"_id": team_id}, {"id": team_id}]})
            if t_doc:
                cap_user = await db.users.find_one({"$or": [{"_id": t_doc.get("captain_id")}, {"id": t_doc.get("captain_id")}]}) if t_doc.get("captain_id") else None
                registered_teams.append({
                    "id": str(t_doc.get("_id", t_doc.get("id"))),
                    "name": t_doc.get("name"),
                    "tag": t_doc.get("tag", "TEAM"),
                    "captain_name": (cap_user.get("full_name") or cap_user.get("username")) if cap_user else (t_doc.get("captain_handle") or "Captain"),
                    "captain_handle": cap_user.get("in_game_name") or t_doc.get("captain_handle") or "Captain",
                    "member_count": len(t_doc.get("members") or []),
                    "logo_url": t_doc.get("logo_url"),
                })
        elif user_id:
            registered_ids.add(str(user_id))
            u_doc = await db.users.find_one({"$or": [{"_id": user_id}, {"id": user_id}]})
            if u_doc:
                registered_teams.append({
                    "id": str(u_doc.get("_id", u_doc.get("id"))),
                    "name": u_doc.get("username"),
                    "tag": "SOLO",
                    "captain_name": u_doc.get("full_name") or u_doc.get("username"),
                    "captain_handle": u_doc.get("in_game_name") or u_doc.get("username"),
                    "member_count": 1,
                    "logo_url": u_doc.get("avatar_url"),
                })

    # 2. Fetch all matches for this tournament
    m_cursor = db.matches.find({
        "$or": [{"tournament_id": tid}, {"tournament_id": tslug}]
    }).sort([("round_number", 1), ("match_number", 1), ("order", 1)])
    raw_matches = await m_cursor.to_list(length=300)

    paired_team_ids = set()
    rounds_map: Dict[str, Any] = {}
    round_streams = tourn.get("round_streams") or {}

    # Initialize rounds from stages if present
    for stg in (tourn.get("stages") or []):
        for sr in (stg.get("rounds") or []):
            r_name = sr.get("round_name") or sr.get("name")
            r_num = sr.get("round_number") or sr.get("round_order") or 1
            s_url = sr.get("stream_url") or sr.get("live_stream_url") or round_streams.get(r_name)
            if r_name and r_name not in rounds_map:
                rounds_map[r_name] = {
                    "round_number": r_num,
                    "round_name": r_name,
                    "stream_url": s_url,
                    "live_stream_url": s_url,
                    "format": sr.get("format") or tourn.get("format", "Knockout"),
                    "series_format": sr.get("series_format", "BO1"),
                    "matches": [],
                }

    # Initialize rounds from custom_rounds if present
    for idx, cr_name in enumerate(tourn.get("custom_rounds") or [], start=1):
        s_url = round_streams.get(cr_name)
        if cr_name not in rounds_map:
            rounds_map[cr_name] = {
                "round_number": idx,
                "round_name": cr_name,
                "stream_url": s_url,
                "live_stream_url": s_url,
                "format": tourn.get("format", "Knockout"),
                "series_format": tourn.get("series_format", "BO1"),
                "matches": [],
            }
        elif s_url and not rounds_map[cr_name].get("stream_url"):
            rounds_map[cr_name]["stream_url"] = s_url
            rounds_map[cr_name]["live_stream_url"] = s_url

    # Populate matches into rounds_map
    for m_doc in raw_matches:
        m = Match.from_mongo(m_doc)
        r_num = m.round_number or 1
        r_name = m.round_name or m.stage or f"Round {r_num}"

        if m.team_a_id:
            paired_team_ids.add(str(m.team_a_id))
        if m.team_b_id:
            paired_team_ids.add(str(m.team_b_id))
        for tid_extra in (m.team_ids or []):
            paired_team_ids.add(str(tid_extra))

        r_stream = round_streams.get(r_name)
        if r_name not in rounds_map:
            rounds_map[r_name] = {
                "round_number": r_num,
                "round_name": r_name,
                "stream_url": r_stream,
                "live_stream_url": r_stream,
                "format": m.format or tourn.get("format", "Knockout"),
                "series_format": m.series_format or "BO1",
                "matches": [],
            }
        elif r_stream and not rounds_map[r_name].get("stream_url"):
            rounds_map[r_name]["stream_url"] = r_stream
            rounds_map[r_name]["live_stream_url"] = r_stream

        # Inherit round stream if not explicitly overridden on match
        if rounds_map[r_name].get("stream_url") and not m.youtube_live_url:
            m.youtube_live_url = rounds_map[r_name]["stream_url"]
            m.live_link = rounds_map[r_name]["stream_url"]

        rounds_map[r_name]["matches"].append(m.model_dump())

    # Build rounds array sorted by round_number
    rounds_list = sorted(list(rounds_map.values()), key=lambda x: x["round_number"])

    # Unpaired registered teams
    unpaired_teams = [t for t in registered_teams if t["id"] not in paired_team_ids]

    return {
        "status": "success",
        "tournament": {
            "id": tid,
            "title": tourn.get("title", "Tournament"),
            "slug": tslug,
            "format": tourn.get("match_format") or tourn.get("format", "Single Elimination"),
            "status": tourn.get("status", "UPCOMING"),
            "registration_status": tourn.get("registration_status", "OPEN"),
            "live_stream_url": tourn.get("live_stream_url"),
            "community_platform": tourn.get("community_platform"),
            "community_link": tourn.get("community_link"),
            "server_region": tourn.get("server_region", "AWS Mumbai (ap-south-1)"),
            "custom_rounds": tourn.get("custom_rounds") or [],
            "round_streams": round_streams,
        },
        "registered_teams": registered_teams,
        "unpaired_teams": unpaired_teams,
        "rounds": rounds_list,
        "total_matches": len(raw_matches),
    }


@router.patch("/tournaments/{tournament_id}/stream")
async def update_tournament_stream(
    tournament_id: str,
    payload: TournamentStreamUpdatePayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from bson import ObjectId
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}, {"slug": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})

    url = payload.get_url()
    if url:
        url = url.strip()
        if url and not (url.startswith("http://") or url.startswith("https://")):
            url = f"https://{url}"
    else:
        url = None

    res = await db.tournaments.update_one(
        t_query,
        {"$set": {"live_stream_url": url}}
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Tournament not found")

    return {
        "status": "success",
        "message": "Tournament live stream URL updated",
        "live_stream_url": url,
        "stream_url": url,
    }


@router.patch("/tournaments/{tournament_id}/rounds/{round_name}/stream")
async def update_round_stream_admin(
    tournament_id: str,
    round_name: str,
    payload: TournamentStreamUpdatePayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Section 5: Round-level broadcast management.
    A broadcast URL belongs to a round, not to every individual match.
    Updates round_streams[round_name] on the tournament and cascades to all matches in this round.
    """
    from bson import ObjectId
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}, {"slug": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})

    tourn = await db.tournaments.find_one(t_query)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    raw_url = payload.get_url()
    url = None
    if raw_url:
        trimmed = raw_url.strip()
        if trimmed:
            if not (trimmed.startswith("http://") or trimmed.startswith("https://")):
                url = f"https://{trimmed}"
            else:
                url = trimmed

    # 1. Update round_streams dict on tournament
    await db.tournaments.update_one(
        {"_id": tourn["_id"]},
        {"$set": {f"round_streams.{round_name}": url}}
    )

    # 2. Update matching stage rounds if present
    stages = tourn.get("stages") or []
    updated_stages = False
    for stg in stages:
        for sr in (stg.get("rounds") or []):
            if (sr.get("round_name") or sr.get("name")) == round_name:
                sr["stream_url"] = url
                sr["live_stream_url"] = url
                updated_stages = True
    if updated_stages:
        await db.tournaments.update_one(
            {"_id": tourn["_id"]},
            {"$set": {"stages": stages}}
        )

    # 3. Cascade round-level stream URL to all matches in this round
    await db.matches.update_many(
        {
            "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
            "$or": [{"round_name": round_name}, {"stage": round_name}],
        },
        {"$set": {"live_link": url, "youtube_live_url": url}}
    )

    return {
        "status": "success",
        "message": f'Broadcast URL for round "{round_name}" updated successfully.',
        "round_name": round_name,
        "stream_url": url,
    }


@router.patch("/matches/{id}/credentials")
@router.put("/matches/{id}/credentials")
async def update_match_credentials_admin(
    id: str,
    payload: MatchCredentialsUpdatePayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Section 7: Organizer enters or edits match room credentials.
    Credentials remain unreleased until explicit release action.
    """
    from bson import ObjectId
    m_query = {"$or": [{"_id": id}, {"id": id}]}
    if ObjectId.is_valid(id):
        m_query["$or"].append({"_id": ObjectId(id)})

    match = await db.matches.find_one(m_query)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    upd: Dict[str, Any] = {}
    if payload.room_id is not None:
        upd["room_id"] = payload.room_id.strip()
        upd["lobby_code"] = payload.room_id.strip()
    if payload.lobby_code is not None:
        upd["lobby_code"] = payload.lobby_code.strip()
        if "room_id" not in upd:
            upd["room_id"] = payload.lobby_code.strip()
    if payload.room_password is not None:
        upd["room_password"] = payload.room_password.strip()
        upd["lobby_password"] = payload.room_password.strip()
    if payload.lobby_password is not None:
        upd["lobby_password"] = payload.lobby_password.strip()
        if "room_password" not in upd:
            upd["room_password"] = payload.lobby_password.strip()
    if payload.custom_instructions is not None:
        upd["custom_instructions"] = payload.custom_instructions.strip()
        upd["match_message"] = payload.custom_instructions.strip()
    if payload.server_region is not None:
        upd["server_region"] = payload.server_region.strip()

    if str(match.get("status", "")).upper() == "LIVE" or getattr(payload, "credentials_released", False):
        from app.models.base import get_utc_now
        upd["credentials_released"] = True
        upd["credentials_released_at"] = get_utc_now()
        upd["credentials_released_by"] = admin_user.username
        upd["join_info_enabled"] = True

    if upd:
        await db.matches.update_one(m_query, {"$set": upd})
        try:
            from app.services.live_match_service import sync_live_match_for_tournament
            await sync_live_match_for_tournament(str(match.get("tournament_id")), db)
        except Exception:
            pass

    return {
        "status": "success",
        "message": "Match credentials saved (unreleased).",
        "match_id": id,
        "credentials_released": match.get("credentials_released", False),
    }


@router.post("/matches/{id}/release-credentials")
async def release_match_credentials_admin(
    id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Releases credentials for eligible match participants.
    Credentials become immediately accessible; no check-in required.
    Match status remains SCHEDULED until the organizer explicitly goes live.
    """
    from bson import ObjectId
    from app.models.base import get_utc_now
    from app.services.match_notification_service import send_match_reminder

    m_query = {"$or": [{"_id": id}, {"id": id}]}
    if ObjectId.is_valid(id):
        m_query["$or"].append({"_id": ObjectId(id)})

    match = await db.matches.find_one(m_query)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    mid = str(match.get("_id", match.get("id")))
    now = get_utc_now()

    room_val = match.get("room_id") or match.get("lobby_code") or match.get("custom_room_id") or ""
    pass_val = match.get("room_password") or match.get("lobby_password") or match.get("custom_room_password") or ""

    upd: Dict[str, Any] = {
        "credentials_released": True,
        "credentials_released_at": now,
        "credentials_released_by": admin_user.username,
        "join_info_enabled": True,
        "room_id": room_val,
        "room_password": pass_val,
        "lobby_code": room_val,
        "lobby_password": pass_val,
        "join_info": {
            "room_id": room_val,
            "room_password": pass_val,
            "lobby_code": room_val,
            "password": pass_val,
        },
    }
    # NOTE: Do NOT change status to CHECK_IN_OPEN. Check-in system is removed.
    # Status remains as-is (SCHEDULED). The organizer explicitly uses Go Live to transition.

    await db.matches.update_one(m_query, {"$set": upd})

    # Notify participants
    try:
        await send_match_reminder(
            match_id=mid,
            reminder_type="FIFTEEN_MINUTES",
            db=db,
            custom_message="Match credentials have been released! Check your Competitor Hub to view room details.",
        )
    except Exception as e:
        print(f"[Release Credentials Notification]: {e}")

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="RELEASE_CREDENTIALS",
        target=f"Match {mid}",
        details="Match credentials released for participants. Status unchanged.",
    )

    try:
        from app.services.live_match_service import sync_live_match_for_tournament
        await sync_live_match_for_tournament(str(match.get("tournament_id")), db)
    except Exception:
        pass

    return {
        "status": "success",
        "message": "Credentials released successfully.",
        "match_id": mid,
        "credentials_released": True,
        "current_status": match.get("status"),
    }


@router.get("/tournaments/{tournament_id}/qualified-contenders")
async def get_qualified_contenders_admin(
    tournament_id: str,
    source_round: Optional[str] = Query(None),
    round_number: Optional[int] = Query(None),
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Section 10 & Critical Repair: Returns eligible qualified contenders from earlier rounds for manual next-round formation.
    - If round_number > 1: retrieves ONLY the actual winners of round_number - 1.
    - Excludes all eliminated participants.
    - Verifies whether all matches in round_number - 1 are completed and reports any pending matches.
    """
    from bson import ObjectId
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}, {"slug": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})

    tourn = await db.tournaments.find_one(t_query)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    is_previous_round_complete = True
    pending_matches = []
    winner_ids = set()
    eliminated_ids = set()
    winner_details = {}

    target_prev_round = (round_number - 1) if (round_number is not None and round_number > 1) else None

    if target_prev_round:
        # 1. Fetch all matches in preceding round for this tournament
        prev_m_cursor = db.matches.find({
            "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
            "round_number": target_prev_round,
        })
        prev_matches = await prev_m_cursor.to_list(length=200)

        for pm in prev_matches:
            pm_stat = str(pm.get("status", "SCHEDULED")).upper()
            if pm_stat != "COMPLETED":
                is_previous_round_complete = False
                pending_matches.append({
                    "id": str(pm.get("_id", pm.get("id"))),
                    "match_number": pm.get("match_number") or pm.get("order") or 1,
                    "round_name": pm.get("round_name") or f"Round {target_prev_round}",
                    "status": pm_stat,
                })
            else:
                w_id = pm.get("winner_team_id")
                if w_id:
                    w_str = str(w_id)
                    winner_ids.add(w_str)
                    w_name = None
                    if w_str == str(pm.get("team_a_id")):
                        w_name = pm.get("team_a_name")
                        if pm.get("team_b_id"):
                            eliminated_ids.add(str(pm.get("team_b_id")))
                    elif w_str == str(pm.get("team_b_id")):
                        w_name = pm.get("team_b_name")
                        if pm.get("team_a_id"):
                            eliminated_ids.add(str(pm.get("team_a_id")))
                    winner_details[w_str] = {
                        "name": w_name or pm.get("winner_team_name") or w_str,
                        "source_match_id": str(pm.get("_id", pm.get("id"))),
                        "source_round": pm.get("round_name") or f"Round {target_prev_round}",
                        "source_round_number": target_prev_round,
                    }

                # Also handle Battle Royale qualifiers list
                for q_id in (pm.get("qualifier_team_ids") or []):
                    winner_ids.add(str(q_id))
                for pl in (pm.get("placements") or []):
                    pl_tid = str(pl.get("team_id") or "")
                    if pl_tid:
                        if pl.get("qualified"):
                            winner_ids.add(pl_tid)
                            winner_details[pl_tid] = {
                                "name": pl.get("team_name") or pl_tid,
                                "source_match_id": str(pm.get("_id", pm.get("id"))),
                                "source_round": pm.get("round_name") or f"Round {target_prev_round}",
                                "source_round_number": target_prev_round,
                            }
                        else:
                            eliminated_ids.add(pl_tid)

    # 2. Query tournament_qualifications table
    q_filter: Dict[str, Any] = {
        "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
    }
    if source_round:
        q_filter["source_round"] = source_round
    if target_prev_round:
        q_filter["source_round_number"] = target_prev_round

    cursor = db.tournament_qualifications.find(q_filter)
    docs = await cursor.to_list(length=300)

    for d in docs:
        pid = str(d.get("participant_id") or "")
        if pid:
            if d.get("status") == "QUALIFIED":
                winner_ids.add(pid)
                if pid not in winner_details:
                    winner_details[pid] = {
                        "name": d.get("participant_name") or pid,
                        "source_match_id": d.get("source_match_id"),
                        "source_round": d.get("source_round"),
                        "source_round_number": d.get("source_round_number", target_prev_round or 1),
                        "placement": d.get("placement"),
                        "points": d.get("points"),
                    }
            elif d.get("status") == "ELIMINATED":
                eliminated_ids.add(pid)

    # Winner IDs take precedence over eliminated
    active_contender_ids = [cid for cid in winner_ids if cid not in eliminated_ids]
    if not target_prev_round and not active_contender_ids:
        # Round 1 or no specific round: return all tournament qualifications
        active_contender_ids = list(winner_ids)

    # Resolve team docs for enriched display in frontend
    res = []
    seen = set()
    for pid in active_contender_ids:
        if pid in seen:
            continue
        seen.add(pid)
        det = winner_details.get(pid, {})
        team_doc = await db.teams.find_one({"$or": [{"_id": pid}, {"id": pid}]})
        if team_doc:
            p_name = team_doc.get("name") or det.get("name") or pid
            p_tag = team_doc.get("tag", "TEAM")
        else:
            user_doc = await db.users.find_one({"$or": [{"_id": pid}, {"id": pid}]})
            if user_doc:
                p_name = user_doc.get("display_name") or user_doc.get("username") or pid
                p_tag = "SOLO"
            else:
                p_name = det.get("name") or pid
                p_tag = "TEAM"

        res.append({
            "participant_id": pid,
            "participant_name": p_name,
            "team_id": pid,
            "team_name": p_name,
            "team_tag": p_tag,
            "source_round": det.get("source_round"),
            "source_round_number": det.get("source_round_number", target_prev_round or 1),
            "source_match_id": det.get("source_match_id"),
            "status": "QUALIFIED",
            "points": det.get("points"),
            "placement": det.get("placement"),
        })

    return {
        "status": "success",
        "tournament_id": tid,
        "round_number": round_number,
        "is_previous_round_complete": is_previous_round_complete,
        "has_pending_matches": not is_previous_round_complete,
        "pending_matches": pending_matches,
        "qualified_contenders": res,
        "contenders": res,
        "eliminated_contenders": list(eliminated_ids),
        "total_qualified": len(res),
        "count": len(res),
    }


@router.patch("/matches/{id}/alive-status")
async def update_match_alive_status_admin(
    id: str,
    payload: Dict[str, Any],
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Section 11: Real-time operational tracking of alive vs eliminated teams in Battle Royale.
    """
    from bson import ObjectId
    m_query = {"$or": [{"_id": id}, {"id": id}]}
    if ObjectId.is_valid(id):
        m_query["$or"].append({"_id": ObjectId(id)})

    match = await db.matches.find_one(m_query)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    team_id = payload.get("team_id")
    is_alive = payload.get("is_alive", True)
    if not team_id:
        raise HTTPException(status_code=400, detail="team_id is required")

    alive_map = dict(match.get("alive_status") or {})
    alive_map[str(team_id)] = bool(is_alive)

    await db.matches.update_one(m_query, {"$set": {"alive_status": alive_map}})

    return {
        "status": "success",
        "team_id": str(team_id),
        "is_alive": bool(is_alive),
        "alive_status": alive_map,
    }


@router.post("/matches/{id}/battle-royale-results")
async def submit_battle_royale_results_admin(
    id: str,
    payload: BattleRoyaleSubmitResultsPayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Section 11 & Overhaul: Organizer submits official Battle Royale placements, kills, scoring,
    and multi-team round qualifiers.
    - Decouples match completion from tournament completion.
    - Accurately advances confirmed qualifying teams into Round N+1.
    - Idempotent and validates that selected qualifiers belong to this match.
    """
    from bson import ObjectId
    from app.models.base import generate_uuid, get_utc_now

    m_query = {"$or": [{"_id": id}, {"id": id}]}
    if ObjectId.is_valid(id):
        m_query["$or"].append({"_id": ObjectId(id)})

    match = await db.matches.find_one(m_query)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    tid = str(match.get("tournament_id"))
    tourn_query = {"$or": [{"_id": tid}, {"id": tid}, {"slug": tid}]}
    if ObjectId.is_valid(tid):
        tourn_query["$or"].append({"_id": ObjectId(tid)})
    tourn = await db.tournaments.find_one(tourn_query)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    # Verify authorization: Must be super admin, admin, or tournament organizer
    if admin_user.role not in ["SUPER_ADMIN", "ADMIN"]:
        is_creator = str(tourn.get("created_by") or tourn.get("organizer_id")) == str(admin_user.id)
        if not is_creator:
            raise HTTPException(status_code=403, detail="Unauthorized: Only the tournament organizer or super admin can submit match results.")

    current_status = str(match.get("status", "SCHEDULED")).upper()
    if current_status in ["SCHEDULED", "LIVE", "CHECK_IN_OPEN", "DRAFT"]:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot enter or submit match results: match must be ended first (current status: {current_status})."
        )

    # 1. Gather all participating teams in this match
    match_teams = list(match.get("teams") or [])
    match_team_ids = set()
    for t in match_teams:
        if isinstance(t, dict):
            t_id = str(t.get("id") or t.get("_id") or t.get("team_id"))
            if t_id:
                match_team_ids.add(t_id)
    for tid_item in match.get("team_ids") or []:
        if tid_item:
            match_team_ids.add(str(tid_item))

    # Fallback to team_a_id / team_b_id if match_teams is empty
    if not match_team_ids:
        for fallback_id in [match.get("team_a_id"), match.get("team_b_id")]:
            if fallback_id:
                match_team_ids.add(str(fallback_id))

    # 2. Resolve qualifier team IDs
    raw_qualifier_ids = list(payload.qualifier_team_ids or [])
    if not raw_qualifier_ids:
        raw_qualifier_ids = [str(r.team_id) for r in payload.get_results() if r.qualified]

    # Validate no duplicate qualifiers
    if len(raw_qualifier_ids) != len(set(raw_qualifier_ids)):
        raise HTTPException(status_code=400, detail="Duplicate team entries in qualifier selection.")

    # Validate qualifiers belong to this match
    if match_team_ids:
        for q_id in raw_qualifier_ids:
            if str(q_id) not in match_team_ids:
                raise HTTPException(
                    status_code=400,
                    detail=f"Team '{q_id}' does not belong to this match."
                )

    # Validate qualifier count matches if explicitly requested
    if payload.qualifiers_count is not None and payload.qualifiers_count >= 0:
        if len(raw_qualifier_ids) != payload.qualifiers_count:
            raise HTTPException(
                status_code=400,
                detail=f"Expected exactly {payload.qualifiers_count} qualifiers, but {len(raw_qualifier_ids)} were selected."
            )

    qualifier_set = set(str(q) for q in raw_qualifier_ids)

    # 3. Process placements & scoring
    results_list = payload.get_results()

    # Standard Battle Royale Placement Matrix (String keys required for BSON/MongoDB):
    default_placement_points = {
        "1": 10, "2": 6, "3": 5, "4": 4, "5": 3, "6": 2, "7": 1, "8": 1
    }
    pts_per_kill = 1

    processed_results = []
    seen_result_teams = set()
    winner_team_id = None

    if results_list:
        for r in results_list:
            r_tid = str(r.team_id)
            if r_tid in seen_result_teams:
                raise HTTPException(status_code=400, detail=f"Duplicate team result for team '{r_tid}'.")
            if match_team_ids and r_tid not in match_team_ids:
                raise HTTPException(status_code=400, detail=f"Team '{r_tid}' does not belong to this match.")
            seen_result_teams.add(r_tid)

            place = r.placement or (len(processed_results) + 1)
            kills = r.kills or 0
            calc_place_pts = default_placement_points.get(str(place), default_placement_points.get(place, 0))
            calc_kill_pts = kills * pts_per_kill

            p_pts = r.placement_points if r.placement_points is not None else calc_place_pts
            e_pts = r.elimination_points if r.elimination_points is not None else calc_kill_pts
            t_pts = r.total_points if r.total_points is not None else (p_pts + e_pts)

            is_qual = r_tid in qualifier_set or bool(r.qualified)

            if place == 1:
                winner_team_id = r_tid

            processed_results.append({
                "team_id": r_tid,
                "team_name": r.team_name or str(r.team_id),
                "placement": place,
                "kills": kills,
                "placement_points": p_pts,
                "elimination_points": e_pts,
                "total_points": t_pts,
                "is_alive": False,
                "qualified": is_qual,
            })
    else:
        # Build results from match_teams if results_list omitted
        for idx, t in enumerate(match_teams, start=1):
            t_id = str(t.get("id") or t.get("_id") or t.get("team_id"))
            is_qual = t_id in qualifier_set
            processed_results.append({
                "team_id": t_id,
                "team_name": t.get("name") or t.get("team_name") or str(t_id),
                "placement": idx,
                "kills": 0,
                "placement_points": default_placement_points.get(str(idx), 0),
                "elimination_points": 0,
                "total_points": default_placement_points.get(str(idx), 0),
                "is_alive": False,
                "qualified": is_qual,
            })
            if idx == 1:
                winner_team_id = t_id

    # If winner is not set and we have qualifiers
    if not winner_team_id and raw_qualifier_ids:
        winner_team_id = raw_qualifier_ids[0]

    # Validate that on final round (if finalizing), winner is certified
    if not payload.draft and payload.is_final_round and not winner_team_id:
        if processed_results:
            winner_team_id = processed_results[0]["team_id"]
        elif raw_qualifier_ids:
            winner_team_id = raw_qualifier_ids[0]
        else:
            raise HTTPException(status_code=400, detail="Cannot finalize tournament without certifying a winning champion.")

    # If this is a draft save (Save Results):
    if payload.draft:
        clean_scoring_matrix = None
        if payload.scoring_matrix and isinstance(payload.scoring_matrix, dict):
            clean_scoring_matrix = {}
            for k, v in payload.scoring_matrix.items():
                if isinstance(v, dict):
                    clean_scoring_matrix[str(k)] = {str(sub_k): sub_v for sub_k, sub_v in v.items()}
                else:
                    clean_scoring_matrix[str(k)] = v
        else:
            clean_scoring_matrix = {"placement": default_placement_points, "kill": pts_per_kill}

        match_update = {
            "status": "RESULTS_SAVED",
            "placements": processed_results,
            "winner_team_id": winner_team_id,
            "qualifier_team_ids": raw_qualifier_ids,
            "qualifiers_count": len(raw_qualifier_ids),
            "is_final_round": payload.is_final_round,
            "scoring_matrix": clean_scoring_matrix,
            "updated_at": get_utc_now(),
        }
        await db.matches.update_one(m_query, {"$set": match_update})

        await record_audit_log(
            db=db,
            operator=admin_user.username,
            role=admin_user.role,
            action="SAVE_BATTLE_ROYALE_DRAFT_RESULTS",
            target=f"Match {id}",
            details=f"Draft BR results saved. Placements: {len(processed_results)}. Status: RESULTS_SAVED.",
        )

        try:
            from app.services.live_match_service import sync_live_match_for_tournament
            await sync_live_match_for_tournament(tid, db)
        except Exception:
            pass

        return {
            "status": "success",
            "message": "Battle Royale results saved as draft. Review and finalize when ready.",
            "draft": True,
            "match_id": id,
            "winner_team_id": winner_team_id,
            "qualifiers_count": len(raw_qualifier_ids),
            "qualifier_team_ids": raw_qualifier_ids,
            "is_final_round": payload.is_final_round,
            "placements": processed_results,
        }

    # 4. Upsert records into db.tournament_qualifications
    curr_round_num = match.get("round_number", 1)
    curr_round_name = match.get("round_name") or f"Round {curr_round_num}"
    match_id_str = str(match.get("_id", match.get("id")))

    for entry in processed_results:
        t_id = entry["team_id"]
        is_qual = entry["qualified"] or (t_id in qualifier_set)
        qual_status = "QUALIFIED" if is_qual else "ELIMINATED"

        qual_doc = {
            "tournament_id": tid,
            "source_round": curr_round_name,
            "source_round_number": curr_round_num,
            "source_match_id": match_id_str,
            "participant_id": t_id,
            "participant_name": entry["team_name"],
            "participant_type": "TEAM",
            "status": qual_status,
            "placement": entry["placement"],
            "points": entry["total_points"],
            "result_reference": entry,
            "updated_at": get_utc_now(),
        }
        await db.tournament_qualifications.update_one(
            {
                "tournament_id": tid,
                "source_match_id": match_id_str,
                "participant_id": t_id,
            },
            {"$set": qual_doc, "$setOnInsert": {"id": generate_uuid(), "created_at": get_utc_now()}},
            upsert=True,
        )

    # 5. Multi-Round Progression (Advancing confirmed qualifiers into next round)
    next_match_id = None
    next_round_name = None

    if payload.is_final_round:
        # Final stage reached: Finalize tournament champion!
        await db.tournaments.update_one(
            {"$or": [{"_id": tid}, {"id": tid}, {"slug": tid}]},
            {"$set": {
                "status": "COMPLETED",
                "winner_team_id": winner_team_id,
                "updated_at": get_utc_now(),
            }}
        )
    else:
        # Intermediate round completed: Tournament MUST REMAIN LIVE!
        await db.tournaments.update_one(
            {"$or": [{"_id": tid}, {"id": tid}, {"slug": tid}]},
            {"$set": {"status": "LIVE", "updated_at": get_utc_now()}}
        )

        # Generate or update downstream round ONLY if explicitly requested by organizer
        if payload.advance_to_next_round:
            next_round_num = curr_round_num + 1
            next_round_name = payload.next_round_name or f"Round {next_round_num}"

            # Gather qualifying team objects with original metadata
            teams_by_id = {str(t.get("id") or t.get("_id") or t.get("team_id")): t for t in match_teams if isinstance(t, dict)}
            advancing_teams = []
            for qid in raw_qualifier_ids:
                if qid in teams_by_id:
                    advancing_teams.append(teams_by_id[qid])
                else:
                    t_doc = await db.teams.find_one({"$or": [{"_id": qid}, {"id": qid}]})
                    if t_doc:
                        advancing_teams.append({
                            "id": str(t_doc.get("_id", t_doc.get("id"))),
                            "name": t_doc.get("name", f"Team {qid}"),
                            "captain_id": t_doc.get("captain_id"),
                            "captain_username": t_doc.get("captain_username", "Captain"),
                        })
                    else:
                        advancing_teams.append({
                            "id": qid,
                            "name": next((r["team_name"] for r in processed_results if r["team_id"] == qid), f"Team {qid}"),
                        })

            # Check if an existing downstream match was explicitly created by the organizer (Idempotency)
            existing_next = await db.matches.find_one({
                "$or": [{"tournament_id": tid}, {"tournament_id": match.get("tournament_id")}],
                "round_number": next_round_num,
                "previous_match_ids": match_id_str,
            })
            if not existing_next:
                existing_next = await db.matches.find_one({
                    "$or": [{"tournament_id": tid}, {"tournament_id": match.get("tournament_id")}],
                    "round_number": next_round_num,
                    "format": "Battle Royale",
                    "status": {"$in": ["SCHEDULED", "DRAFT"]},
                })

            if existing_next:
                if existing_next.get("status") in ["LIVE", "COMPLETED"]:
                    raise HTTPException(
                        status_code=400,
                        detail="Cannot modify qualifiers: downstream round match is already in progress or completed."
                    )
                next_match_id = str(existing_next.get("_id", existing_next.get("id")))
                await db.matches.update_one(
                    {"_id": existing_next["_id"]},
                    {"$set": {
                        "teams": advancing_teams,
                        "team_ids": raw_qualifier_ids,
                        "max_teams": len(advancing_teams),
                        "stage_name": next_round_name,
                        "round_name": next_round_name,
                        "stage": next_round_name,
                        "updated_at": get_utc_now(),
                    }}
                )
            else:
                next_match_id = generate_uuid()
                new_match = Match(
                    id=next_match_id,
                    tournament_id=tid,
                    stage_name=next_round_name,
                    round_name=next_round_name,
                    stage=next_round_name,
                    round_number=next_round_num,
                    match_number=1,
                    format="Battle Royale",
                    status="SCHEDULED",
                    teams=advancing_teams,
                    team_ids=raw_qualifier_ids,
                    max_teams=len(advancing_teams),
                    previous_match_ids=[match_id_str],
                    created_at=get_utc_now(),
                    updated_at=get_utc_now(),
                )
                await db.matches.insert_one(new_match.to_mongo())

    # 6. Save current match updates
    clean_scoring_matrix = None
    if payload.scoring_matrix and isinstance(payload.scoring_matrix, dict):
        clean_scoring_matrix = {}
        for k, v in payload.scoring_matrix.items():
            if isinstance(v, dict):
                clean_scoring_matrix[str(k)] = {str(sub_k): sub_v for sub_k, sub_v in v.items()}
            else:
                clean_scoring_matrix[str(k)] = v
    else:
        clean_scoring_matrix = {"placement": default_placement_points, "kill": pts_per_kill}

    match_update = {
        "status": "COMPLETED",
        "placements": processed_results,
        "winner_team_id": winner_team_id,
        "qualifier_team_ids": raw_qualifier_ids,
        "qualifiers_count": len(raw_qualifier_ids),
        "is_final_round": payload.is_final_round,
        "scoring_matrix": clean_scoring_matrix,
        "updated_at": get_utc_now(),
    }
    if next_match_id:
        match_update["next_match_id"] = next_match_id
        match_update["advancement_label"] = f"{next_round_name} Lobby"

    await db.matches.update_one(m_query, {"$set": match_update})

    # Record audit log
    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="COMPLETE_BATTLE_ROYALE_MATCH",
        target=f"Match {id}",
        details=f"BR results submitted. Winner: {winner_team_id}. Qualifiers: {len(raw_qualifier_ids)}. Final round: {payload.is_final_round}.",
    )

    try:
        from app.services.live_match_service import sync_live_match_for_tournament
        await sync_live_match_for_tournament(tid, db)
    except Exception as e:
        pass

    return {
        "status": "success",
        "message": (
            f"Battle Royale results finalized! {len(raw_qualifier_ids)} qualifiers advanced to {next_round_name}."
            if not payload.is_final_round
            else f"Tournament grand final certified! Winner {winner_team_id} declared Champion."
        ),
        "match_id": id,
        "winner_team_id": winner_team_id,
        "qualifiers_count": len(raw_qualifier_ids),
        "qualifier_team_ids": raw_qualifier_ids,
        "is_final_round": payload.is_final_round,
        "next_match_id": next_match_id,
        "placements": processed_results,
    }


@router.get("/matches/{match_id}/ratings")
async def get_match_ratings_admin(
    match_id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Section 14: Returns authentic participants and existing manual post-match ratings.
    Resolves real player identities (user_id, display_name, avatar, current MMR rating).
    """
    from bson import ObjectId
    m_query = {"$or": [{"_id": match_id}, {"id": match_id}]}
    if ObjectId.is_valid(match_id):
        m_query["$or"].append({"_id": ObjectId(match_id)})

    match = await db.matches.find_one(m_query)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    ratings_list = match.get("participant_ratings") or []
    # Index ratings by user_id and participant_id
    rated_map = {}
    for r in ratings_list:
        if r.get("user_id"):
            rated_map[str(r["user_id"])] = r
        if r.get("participant_id"):
            rated_map[str(r["participant_id"])] = r

    # Resolve authentic players
    resolved_players = await resolve_match_players(match, db)

    participants = []
    seen_pids = set()

    for p in resolved_players:
        uid = str(p["user_id"])
        if uid in seen_pids:
            continue
        seen_pids.add(uid)
        existing_r = rated_map.get(uid, {})
        participants.append({
            "participant_id": uid,
            "user_id": uid,
            "player_id": uid,
            "name": p.get("display_name") or p.get("username") or "Player",
            "display_name": p.get("display_name") or p.get("username") or "Player",
            "username": p.get("username", "player"),
            "avatar_url": p.get("avatar_url"),
            "team_id": p.get("team_id"),
            "team_name": p.get("team_name") or "Team",
            "current_rating": p.get("current_rating", 500),
            "is_winner": (p.get("team_id") and p.get("team_id") == match.get("winner_team_id")),
            "rating": existing_r.get("rating"),
            "notes": existing_r.get("notes") or existing_r.get("review"),
            "review": existing_r.get("review") or existing_r.get("notes"),
            "rated": uid in rated_map,
        })

    # If resolved_players was empty (edge case fallback), preserve team fallback so older fixtures don't break
    if not participants:
        for t_key, tname_key in [("team_a_id", "team_a_name"), ("team_b_id", "team_b_name")]:
            tid = match.get(t_key)
            if tid:
                tname = match.get(tname_key) or "Team"
                existing_r = rated_map.get(str(tid), {})
                participants.append({
                    "participant_id": str(tid),
                    "user_id": str(tid),
                    "player_id": str(tid),
                    "name": tname,
                    "display_name": tname,
                    "username": tname,
                    "avatar_url": None,
                    "team_id": str(tid),
                    "team_name": tname,
                    "current_rating": 500,
                    "is_winner": (str(tid) == str(match.get("winner_team_id"))),
                    "rating": existing_r.get("rating"),
                    "notes": existing_r.get("notes") or existing_r.get("review"),
                    "review": existing_r.get("review") or existing_r.get("notes"),
                    "rated": str(tid) in rated_map,
                })

    return {
        "status": "success",
        "match_id": match_id,
        "is_completed": (match.get("status") in ["COMPLETED", "FINALIZED"]),
        "participants": participants,
        "ratings": ratings_list,
        "rating_scale": "1-10",
    }


@router.post("/matches/{match_id}/ratings")
async def submit_match_ratings_admin(
    match_id: str,
    payload: MatchRatingsSubmitPayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Section 14: Manual post-match player ratings assigned by authorized organizers.
    Supports 1-10 performance rating (System 2) and optional skill rating adjustment (+/- MMR System 1).
    Idempotent when submission_id is supplied.
    """
    from bson import ObjectId
    from app.models.base import generate_uuid, get_utc_now

    m_query = {"$or": [{"_id": match_id}, {"id": match_id}]}
    if ObjectId.is_valid(match_id):
        m_query["$or"].append({"_id": ObjectId(match_id)})

    match = await db.matches.find_one(m_query)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    if match.get("status") not in ["COMPLETED", "FINALIZED"]:
        raise HTTPException(
            status_code=400,
            detail="Participant ratings can only be assigned after the match result is finalized/completed.",
        )

    for r in payload.ratings:
        if r.rating < 1 or r.rating > 10:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid rating {r.rating}. Ratings must be between 1 and 10.",
            )

    now = get_utc_now()
    existing_ratings = list(match.get("participant_ratings") or [])
    ratings_dict = {str(r.get("user_id") or r.get("participant_id")): r for r in existing_ratings if r.get("user_id") or r.get("participant_id")}

    game_id = match.get("game_id", "")
    tournament_id = match.get("tournament_id", "")
    round_id = match.get("round_id") or match.get("round_name")

    for item in payload.ratings:
        uid = str(item.player_id or item.user_id or item.participant_id or generate_uuid())
        pid = uid

        entry = {
            "participant_id": pid,
            "user_id": uid,
            "username": item.username or item.display_name,
            "display_name": item.display_name or item.username,
            "team_name": item.team_name,
            "team_id": item.team_id,
            "rating": item.rating,
            "notes": item.notes or item.review,
            "review": item.review or item.notes,
            "rated_by": admin_user.username,
            "rated_at": now.isoformat(),
        }
        if item.skill_adjustment:
            entry["skill_adjustment"] = item.skill_adjustment
        ratings_dict[uid] = entry

        # 1. Update/insert match_ratings collection
        await db.match_ratings.update_one(
            {
                "match_id": match_id,
                "$or": [{"user_id": uid}, {"participant_id": pid}],
            },
            {
                "$set": {
                    "match_id": match_id,
                    "tournament_id": tournament_id,
                    "participant_id": pid,
                    "user_id": uid,
                    "rating": item.rating,
                    "skill_adjustment": item.skill_adjustment,
                    "notes": item.notes or item.review,
                    "review": item.review or item.notes,
                    "rated_by": admin_user.username,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "id": generate_uuid(),
                    "created_at": now,
                },
            },
            upsert=True,
        )

        # 2. If skill_adjustment is specified, apply System 1 MMR adjustment with idempotency
        if item.skill_adjustment and int(item.skill_adjustment) != 0:
            sub_id = f"{payload.submission_id}_{uid}" if payload.submission_id else None
            await apply_manual_rating_adjustment(
                db=db,
                admin_user=admin_user,
                player_id=uid,
                game_id=str(game_id) if game_id else "general",
                adjustment=int(item.skill_adjustment),
                reason=item.adjustment_reason or "Post-Match Performance Evaluation",
                explanation=item.adjustment_explanation or item.review or item.notes or f"Match {match_id} rating adjustment",
                tournament_id=str(tournament_id) if tournament_id else None,
                round_id=str(round_id) if round_id else None,
                match_id=match_id,
                submission_id=sub_id,
            )

    updated_ratings = list(ratings_dict.values())
    await db.matches.update_one(m_query, {"$set": {"participant_ratings": updated_ratings}})

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="SUBMIT_MATCH_RATINGS",
        target=f"Match {match_id}",
        details=f"Assigned ratings for {len(payload.ratings)} participants on scale {payload.rating_scale}.",
    )

    return {
        "status": "success",
        "message": f"Successfully assigned ratings for {len(payload.ratings)} participants.",
        "match_id": match_id,
        "total_rated": len(updated_ratings),
        "ratings": updated_ratings,
    }


@router.get("/tournaments/{tournament_id}/rounds/{round_name}/review")
async def get_round_review_admin(
    tournament_id: str,
    round_name: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Round-level workflow: Audits completion status of all matches in a round,
    gathers all participants across completed matches, aggregates standings/scores,
    computes proposed qualifiers, and provides ratings readiness.
    """
    from bson import ObjectId
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}, {"slug": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})

    tourn = await db.tournaments.find_one(t_query)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))

    # Query matches belonging to this tournament and round
    m_cursor = db.matches.find({
        "$or": [{"tournament_id": tid}, {"tournament_id": tourn.get("slug", tid)}],
        "$and": [
            {"$or": [
                {"round_name": round_name},
                {"stage": round_name},
                {"round_id": round_name},
            ]},
            {"is_dummy": {"$ne": True}},
        ],
    })
    matches = await m_cursor.to_list(100)

    total_matches = len(matches)
    completed_matches = sum(1 for m in matches if m.get("status") in ["COMPLETED", "FINALIZED"])
    all_completed = (total_matches > 0 and completed_matches == total_matches)

    # Gather participants and aggregate standings across all matches in this round
    all_players: List[dict] = []
    seen_uids = set()
    standings_map: Dict[str, dict] = {}

    for m in matches:
        m_id = str(m.get("_id", m.get("id")))
        # Check BR results if available
        br_res = m.get("battle_royale_results") or m.get("placements") or []
        for r in br_res:
            t_id = str(r.get("team_id"))
            if t_id not in standings_map:
                standings_map[t_id] = {
                    "participant_id": t_id,
                    "participant_name": r.get("team_name", t_id),
                    "total_points": 0,
                    "kills": 0,
                    "matches_played": 0,
                    "best_placement": 999,
                    "qualified": bool(r.get("qualified")),
                }
            st = standings_map[t_id]
            st["total_points"] += int(r.get("total_points") or 0)
            st["kills"] += int(r.get("kills") or 0)
            st["matches_played"] += 1
            st["best_placement"] = min(st["best_placement"], int(r.get("placement") or 999))
            if r.get("qualified"):
                st["qualified"] = True

        # Check Head-to-Head winner
        if m.get("winner_team_id"):
            w_id = str(m["winner_team_id"])
            if w_id not in standings_map:
                standings_map[w_id] = {
                    "participant_id": w_id,
                    "participant_name": m.get("team_a_name" if w_id == str(m.get("team_a_id")) else "team_b_name") or w_id,
                    "total_points": 0,
                    "wins": 0,
                    "matches_played": 0,
                    "best_placement": 1,
                    "qualified": True,
                }
            standings_map[w_id]["wins"] = standings_map[w_id].get("wins", 0) + 1
            standings_map[w_id]["matches_played"] += 1

        # Resolve authentic players
        players = await resolve_match_players(m, db)
        # Get existing ratings on match
        rated_lookup = {str(r.get("user_id") or r.get("participant_id")): r for r in (m.get("participant_ratings") or [])}

        for p in players:
            uid = str(p["user_id"])
            if uid not in seen_uids:
                seen_uids.add(uid)
                ex = rated_lookup.get(uid, {})
                tid_str = str(p.get("team_id") or "")
                perf = standings_map.get(tid_str) or standings_map.get(uid) or {}

                place = perf.get("best_placement")
                if place == 999:
                    place = None
                kills = perf.get("kills", 0)
                pts = perf.get("total_points", 0)
                wins = perf.get("wins", 0)

                # Calculate transparent proposed rating adjustment and reason
                curr_r = int(p.get("current_rating", 500))
                proposed_adj = 0
                calc_reason = "Standard Performance"

                if place is not None:
                    if place == 1:
                        proposed_adj = 25
                        calc_reason = f"1st Place Dominant ({pts} pts, {kills} kills) -> +25 MMR"
                    elif place <= 3:
                        proposed_adj = 15
                        calc_reason = f"Top 3 Podium ({pts} pts, {kills} kills) -> +15 MMR"
                    elif place <= 5:
                        proposed_adj = 10
                        calc_reason = f"Top 5 Placement ({pts} pts, {kills} kills) -> +10 MMR"
                    elif place <= 10:
                        proposed_adj = 5
                        calc_reason = f"Top 10 Qualifier ({pts} pts, {kills} kills) -> +5 MMR"
                    else:
                        proposed_adj = -5
                        calc_reason = f"Eliminated outside Top 10 (Rank #{place}) -> -5 MMR"
                elif wins > 0:
                    proposed_adj = 15
                    calc_reason = f"Match Winner ({wins} win(s)) -> +15 MMR"
                elif m.get("winner_team_id") and tid_str and tid_str != str(m.get("winner_team_id")):
                    proposed_adj = -5
                    calc_reason = "Match Defeat -> -5 MMR"
                else:
                    proposed_adj = 0
                    calc_reason = "Neutral Performance -> +0 MMR"

                post_r = max(0, curr_r + proposed_adj)

                all_players.append({
                    "player_id": uid,
                    "user_id": uid,
                    "username": p.get("username", "player"),
                    "display_name": p.get("display_name") or p.get("username", "Player"),
                    "avatar_url": p.get("avatar_url"),
                    "team_id": p.get("team_id"),
                    "team_name": p.get("team_name") or "Team",
                    "current_rating": curr_r,
                    "match_id": m_id,
                    "placement": place,
                    "kills": kills,
                    "points": pts,
                    "wins": wins,
                    "proposed_adjustment": proposed_adj,
                    "post_rating": post_r,
                    "calculation_reason": calc_reason,
                    "existing_rating": ex.get("rating"),
                    "existing_notes": ex.get("notes") or ex.get("review"),
                    "is_rated": uid in rated_lookup,
                })

    # Sort standings by total_points or wins descending
    sorted_standings = sorted(
        standings_map.values(),
        key=lambda x: (x.get("total_points", 0), x.get("wins", 0), -x.get("best_placement", 999)),
        reverse=True,
    )
    for rank, item in enumerate(sorted_standings, 1):
        item["rank"] = rank

    # Check if this round is already finalized
    is_round_finalized = False
    for stg in tourn.get("stages", []):
        for rnd in stg.get("rounds", []):
            if rnd.get("name") == round_name or rnd.get("round_name") == round_name or rnd.get("id") == round_name:
                if rnd.get("status") == "FINALIZED" or rnd.get("is_finalized"):
                    is_round_finalized = True
                    break

    # Also check if qualifications already exist in db.tournament_qualifications for this round
    existing_quals = await db.tournament_qualifications.find({
        "$or": [{"tournament_id": tid}, {"tournament_id": tourn.get("slug", tid)}],
        "source_round": round_name,
        "status": "QUALIFIED",
    }).to_list(100)
    if existing_quals and not is_round_finalized:
        is_round_finalized = True

    return {
        "status": "success",
        "tournament_id": tid,
        "round_name": round_name,
        "total_matches": total_matches,
        "completed_matches": completed_matches,
        "all_matches_completed": all_completed,
        "is_round_finalized": is_round_finalized,
        "standings": sorted_standings,
        "proposed_qualifiers": [s["participant_id"] for s in sorted_standings if s.get("qualified") or s.get("rank", 999) <= 10],
        "confirmed_qualifiers": [q["participant_id"] for q in existing_quals],
        "players": all_players,
        "total_players": len(all_players),
        "total_rated_players": sum(1 for p in all_players if p.get("is_rated")),
    }


@router.post("/tournaments/{tournament_id}/rounds/{round_name}/ratings")
async def submit_round_ratings_admin(
    tournament_id: str,
    round_name: str,
    payload: RoundRatingsSubmitPayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Submits ratings across all participants in a completed round.
    Supports 1-10 performance rating (System 2) and optional skill rating adjustment (+/- MMR System 1).
    Idempotent when submission_id is supplied.
    """
    from bson import ObjectId
    from app.models.base import generate_uuid, get_utc_now

    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}, {"slug": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})

    tourn = await db.tournaments.find_one(t_query)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))
    gid = str(tourn.get("game_id", ""))

    for r in payload.ratings:
        r_val = r.get_rating()
        if r_val < 1 or r_val > 10:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid rating {r_val} for player {r.user_id}. Ratings must be between 1 and 10.",
            )

    now = get_utc_now()
    updated_count = 0

    for item in payload.ratings:
        uid = str(item.player_id or item.user_id)
        mid = item.match_id
        r_val = item.get_rating()
        n_val = item.get_notes()

        # 1. Update match_ratings collection
        rating_doc = {
            "tournament_id": tid,
            "round_name": round_name,
            "match_id": mid,
            "participant_id": uid,
            "user_id": uid,
            "username": item.username or item.display_name,
            "display_name": item.display_name or item.username,
            "team_id": item.team_id,
            "team_name": item.team_name,
            "rating": r_val,
            "skill_adjustment": item.skill_adjustment,
            "notes": n_val,
            "review": n_val,
            "rated_by": admin_user.username,
            "updated_at": now,
        }
        await db.match_ratings.update_one(
            {
                "tournament_id": tid,
                "user_id": uid,
                "match_id": mid,
            },
            {
                "$set": rating_doc,
                "$setOnInsert": {"id": generate_uuid(), "created_at": now},
            },
            upsert=True,
        )

        # 2. Also update match document if mid is present
        if mid:
            m_q = {"$or": [{"_id": mid}, {"id": mid}]}
            if ObjectId.is_valid(mid):
                m_q["$or"].append({"_id": ObjectId(mid)})
            match_doc = await db.matches.find_one(m_q)
            if match_doc:
                existing_m_ratings = list(match_doc.get("participant_ratings") or [])
                m_r_dict = {str(r.get("user_id") or r.get("participant_id")): r for r in existing_m_ratings}
                m_r_dict[uid] = {
                    "participant_id": uid,
                    "user_id": uid,
                    "username": item.username or item.display_name,
                    "team_name": item.team_name,
                    "team_id": item.team_id,
                    "rating": item.rating,
                    "skill_adjustment": item.skill_adjustment,
                    "notes": item.notes or item.review,
                    "review": item.review or item.notes,
                    "rated_by": admin_user.username,
                    "rated_at": now.isoformat(),
                }
                await db.matches.update_one(m_q, {"$set": {"participant_ratings": list(m_r_dict.values())}})

        # 3. If skill_adjustment is specified, apply System 1 MMR adjustment with idempotency
        if item.skill_adjustment and int(item.skill_adjustment) != 0:
            sub_id = f"{payload.submission_id}_{uid}" if payload.submission_id else f"round_ratings_{tid}_{round_name}_{uid}"
            await apply_manual_rating_adjustment(
                db=db,
                admin_user=admin_user,
                player_id=uid,
                game_id=gid or "general",
                adjustment=int(item.skill_adjustment),
                reason=item.adjustment_reason or "Round Performance Evaluation",
                explanation=item.adjustment_explanation or item.review or item.notes or f"Round {round_name} rating adjustment",
                tournament_id=tid,
                round_id=round_name,
                match_id=mid,
                submission_id=sub_id,
            )

        updated_count += 1

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="SUBMIT_ROUND_RATINGS",
        target=f"Tournament {tid} - Round {round_name}",
        details=f"Assigned round-level ratings for {updated_count} players.",
    )

    return {
        "status": "success",
        "message": f"Successfully assigned ratings for {updated_count} players in {round_name}.",
        "tournament_id": tid,
        "round_name": round_name,
        "total_rated": updated_count,
    }


@router.post("/tournaments/{tournament_id}/rounds/{round_name}/finalize")
async def finalize_round_admin(
    tournament_id: str,
    round_name: str,
    payload: RoundFinalizePayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Finalizes a tournament round and persists confirmed/overridden qualifiers to db.tournament_qualifications.
    CRITICAL: Validates that all matches in the round are COMPLETED before finalization.
    CRITICAL: Does NOT automatically create the next round (organizer manually creates next round).
    """
    from bson import ObjectId
    from app.models.base import generate_uuid, get_utc_now

    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}, {"slug": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})

    tourn = await db.tournaments.find_one(t_query)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))

    # 1. Verify all matches in round are COMPLETED or FINALIZED
    m_cursor = db.matches.find({
        "$or": [{"tournament_id": tid}, {"tournament_id": tourn.get("slug", tid)}],
        "$and": [
            {"$or": [
                {"round_name": round_name},
                {"stage": round_name},
                {"round_id": round_name},
            ]},
            {"is_dummy": {"$ne": True}},
        ],
    })
    matches = await m_cursor.to_list(100)

    if not matches:
        raise HTTPException(status_code=400, detail=f"No matches found for round '{round_name}'.")

    incomplete_matches = [m for m in matches if m.get("status") not in ["COMPLETED", "FINALIZED"]]
    if incomplete_matches:
        incomplete_details = [
            f"Match #{m.get('match_number', m.get('order', 1))} (ID: {m.get('_id', m.get('id'))}, Status: {m.get('status', 'SCHEDULED')})"
            for m in incomplete_matches
        ]
        raise HTTPException(
            status_code=400,
            detail=f"Cannot finalize round '{round_name}'. {len(incomplete_matches)} match(es) are not yet COMPLETED: {', '.join(incomplete_details)}",
        )

    # 2. Persist confirmed / overridden qualifiers to db.tournament_qualifications
    now = get_utc_now()
    qualifier_ids = payload.get_qualifiers()
    notes_val = payload.get_notes()
    persisted_count = 0

    # Resolve names for qualifiers (from teams or registrations)
    teams_docs = await db.teams.find({
        "$or": [
            {"_id": {"$in": qualifier_ids}},
            {"id": {"$in": qualifier_ids}},
        ]
    }).to_list(100)
    name_map = {str(tm.get("_id", tm.get("id"))): tm.get("name", "Team") for tm in teams_docs}

    # Also check users for solo contenders
    users_docs = await db.users.find({
        "$or": [
            {"_id": {"$in": qualifier_ids}},
            {"id": {"$in": qualifier_ids}},
        ]
    }).to_list(100)
    for u in users_docs:
        uid = str(u.get("_id", u.get("id")))
        name_map[uid] = u.get("display_name") or u.get("username", "Player")

    for q_id in qualifier_ids:
        s_qid = str(q_id)
        qual_doc = {
            "tournament_id": tid,
            "source_round": round_name,
            "source_round_number": payload.round_number or 1,
            "participant_id": s_qid,
            "participant_name": name_map.get(s_qid, s_qid),
            "status": "QUALIFIED",
            "notes": notes_val,
            "finalized_at": now,
            "finalized_by": admin_user.username,
        }
        await db.tournament_qualifications.update_one(
            {
                "tournament_id": tid,
                "source_round": round_name,
                "participant_id": s_qid,
            },
            {
                "$set": qual_doc,
                "$setOnInsert": {"id": generate_uuid(), "created_at": now},
            },
            upsert=True,
        )
        persisted_count += 1

    # 3. Update tournament stages to mark this round finalized
    stages = list(tourn.get("stages") or [])
    updated_stages = False
    for stg in stages:
        for rnd in stg.get("rounds", []):
            if rnd.get("name") == round_name or rnd.get("round_name") == round_name or rnd.get("id") == round_name:
                rnd["status"] = "FINALIZED"
                rnd["is_finalized"] = True
                rnd["finalized_at"] = now.isoformat()
                rnd["finalized_by"] = admin_user.username
                rnd["qualified_count"] = persisted_count
                updated_stages = True

    update_fields = {"updated_at": now}
    if updated_stages:
        update_fields["stages"] = stages

    await db.tournaments.update_one(t_query, {"$set": update_fields})

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="FINALIZE_TOURNAMENT_ROUND",
        target=f"Tournament {tid} - Round {round_name}",
        details=f"Finalized round {round_name} with {persisted_count} qualified contenders.",
    )

    return {
        "status": "success",
        "message": f"Successfully finalized {round_name} with {persisted_count} qualified contender(s).",
        "tournament_id": tid,
        "round_name": round_name,
        "qualified_count": persisted_count,
        "qualifier_ids": qualifier_ids,
    }


# ----------------- SECTION-SPECIFIC CLEAR / ARCHIVE SYSTEM -----------------

def check_archive_permission(section: str, user: User):
    sec = section.strip().lower()
    if sec in ["payments", "wallet_ledger", "audit_logs", "payouts"]:
        if user.role != "SUPER_ADMIN":
            raise HTTPException(status_code=403, detail=f"Operation on {section} requires SUPER_ADMIN role")
    elif sec in ["notifications", "support_tickets"]:
        if user.role not in ["ORGANIZER", "SUPER_ADMIN"]:
            raise HTTPException(status_code=403, detail=f"Operation on {section} requires ORGANIZER or SUPER_ADMIN role")
    else:
        raise HTTPException(status_code=400, detail=f"Unknown archive section '{section}'")


@router.get("/notifications")
async def list_admin_platform_notifications(
    search: Optional[str] = None,
    notification_type: Optional[str] = None,
    is_read: Optional[bool] = None,
    is_archived: bool = False,
    user_id: Optional[str] = None,
    limit: int = 100,
    skip: int = 0,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query: Dict[str, Any] = {"is_dummy": {"$ne": True}}
    if is_archived:
        query["is_archived"] = True
    else:
        query["is_archived"] = {"$ne": True}

    if notification_type and notification_type.upper() != "ALL":
        query["type"] = notification_type.upper()
    if is_read is not None:
        query["is_read"] = is_read
    if user_id and user_id.strip():
        uid_clean = user_id.strip()
        user_matches = await db.users.find({
            "$or": [
                {"_id": uid_clean},
                {"id": uid_clean},
                {"clerk_user_id": uid_clean},
                {"username": uid_clean},
                {"username_lower": uid_clean.lower()},
                {"email": uid_clean.lower()},
            ]
        }).to_list(length=10)
        user_ids = {uid_clean}
        for u in user_matches:
            if u.get("_id"):
                user_ids.add(str(u["_id"]))
            if u.get("id"):
                user_ids.add(str(u["id"]))
            if u.get("clerk_user_id"):
                user_ids.add(str(u["clerk_user_id"]))
        if len(user_ids) == 1:
            query["user_id"] = uid_clean
        else:
            query["user_id"] = {"$in": list(user_ids)}
    if search and search.strip():
        s = search.strip()
        rgx = {"$regex": re.escape(s), "$options": "i"}
        query["$or"] = [{"title": rgx}, {"message": rgx}, {"user_id": rgx}]

    total = await db.notifications.count_documents(query)
    cursor = db.notifications.find(query).sort("created_at", -1).skip(skip).limit(limit)
    docs = await cursor.to_list(length=limit)

    items = []
    for n in docs:
        items.append({
            "id": str(n.get("_id", n.get("id"))),
            "user_id": n.get("user_id"),
            "type": n.get("type", "SYSTEM"),
            "category": n.get("category") or n.get("type", "SYSTEM"),
            "label": n.get("label"),
            "title": n.get("title", ""),
            "message": n.get("message", ""),
            "is_read": bool(n.get("is_read", False)),
            "link": n.get("link"),
            "is_archived": bool(n.get("is_archived", False)),
            "archived_at": n.get("archived_at").isoformat() if hasattr(n.get("archived_at"), "isoformat") else str(n.get("archived_at") or ""),
            "archived_by": n.get("archived_by"),
            "archive_reason": n.get("archive_reason"),
            "created_at": n.get("created_at").isoformat() if hasattr(n.get("created_at"), "isoformat") else str(n.get("created_at") or ""),
        })

    return {"items": items, "total": total}


@router.post("/archive/{section}/preview", response_model=ArchivePreviewResponse)
async def preview_section_archive_endpoint(
    section: str,
    payload: ArchiveFilterRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    check_archive_permission(section, current_user)
    count = await preview_archive(db, section, payload)
    return ArchivePreviewResponse(
        section=section,
        matching_count=count,
        filters_applied=payload.model_dump(exclude_none=True),
    )


@router.post("/archive/{section}/execute", response_model=ArchiveExecutionResponse)
async def execute_section_archive_endpoint(
    section: str,
    payload: ArchiveFilterRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    check_archive_permission(section, current_user)
    count = await execute_archive(db, section, payload, current_user)
    return ArchiveExecutionResponse(
        success=True,
        section=section,
        archived_count=count,
        message=f"Successfully archived {count} records in {section}.",
    )


@router.post("/archive/{section}/{record_id}", response_model=ArchiveExecutionResponse)
async def archive_single_record_endpoint(
    section: str,
    record_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    check_archive_permission(section, current_user)
    ok = await archive_single_record(db, section, record_id, current_user)
    return ArchiveExecutionResponse(
        success=ok,
        section=section,
        archived_count=1 if ok else 0,
        message=f"Record {record_id} archived successfully.",
    )


@router.post("/archive/{section}/{record_id}/restore", response_model=UnarchiveResponse)
async def unarchive_single_record_endpoint(
    section: str,
    record_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    check_archive_permission(section, current_user)
    ok = await unarchive_record(db, section, record_id, current_user)
    return UnarchiveResponse(
        success=ok,
        section=section,
        unarchived_count=1 if ok else 0,
        message=f"Record {record_id} restored to active view successfully.",
    )


@router.post("/archive/{section}/restore-bulk", response_model=UnarchiveResponse)
async def unarchive_bulk_records_endpoint(
    section: str,
    payload: BulkUnarchiveRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    check_archive_permission(section, current_user)
    count = await unarchive_bulk_records(db, section, payload.target_ids, current_user)
    return UnarchiveResponse(
        success=True,
        section=section,
        unarchived_count=count,
        message=f"Successfully restored {count} records to active view.",
    )


# Section-specific convenience routes
@router.post("/payments/{payment_id}/archive")
async def archive_payment_convenience(
    payment_id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    ok = await archive_single_record(db, "payments", payment_id, admin_user)
    return {"status": "success", "message": "Payment archived", "payment_id": payment_id}


@router.post("/payments/{payment_id}/unarchive")
async def unarchive_payment_convenience(
    payment_id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    ok = await unarchive_record(db, "payments", payment_id, admin_user)
    return {"status": "success", "message": "Payment restored", "payment_id": payment_id}


@router.post("/wallet-ledger/{id}/archive")
async def archive_wallet_ledger_convenience(
    id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    ok = await archive_single_record(db, "wallet_ledger", id, admin_user)
    return {"status": "success", "message": "Wallet transaction archived", "id": id}


@router.post("/wallet-ledger/{id}/unarchive")
async def unarchive_wallet_ledger_convenience(
    id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    ok = await unarchive_record(db, "wallet_ledger", id, admin_user)
    return {"status": "success", "message": "Wallet transaction restored", "id": id}


@router.post("/audit-logs/{id}/archive")
async def archive_audit_log_convenience(
    id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    ok = await archive_single_record(db, "audit_logs", id, admin_user)
    return {"status": "success", "message": "Audit log archived", "id": id}


@router.post("/audit-logs/{id}/unarchive")
async def unarchive_audit_log_convenience(
    id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    ok = await unarchive_record(db, "audit_logs", id, admin_user)
    return {"status": "success", "message": "Audit log restored", "id": id}


# --- RATING MANAGEMENT WORKFLOW ENDPOINTS ---

@router.get("/rating-management/hierarchy")
async def get_rating_hierarchy_endpoint(
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns full hierarchy: Games -> Tournaments -> Rounds -> Matches -> Participants
    for authoritative manual rating adjustments.
    """
    hierarchy = await get_rating_management_hierarchy(db)
    return {"status": "success", "hierarchy": hierarchy}


@router.post("/rating-management/adjust-bulk")
async def adjust_ratings_bulk_endpoint(
    payload: RatingAdjustmentBulkRequest,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Applies manual rating adjustments to multiple players atomically.
    Exact integer adjustments only, with mandatory reason category and explanation.
    """
    if not payload.adjustments:
        raise HTTPException(status_code=400, detail="No adjustments provided")

    audit_logs = []
    for adj in payload.adjustments:
        log_doc = await apply_manual_rating_adjustment(
            db=db,
            admin_user=admin_user,
            player_id=adj.player_id,
            game_id=adj.game_id,
            adjustment=adj.adjustment,
            reason=adj.reason,
            explanation=adj.explanation,
            tournament_id=adj.tournament_id,
            round_id=adj.round_id,
            match_id=adj.match_id,
        )
        audit_logs.append(log_doc)

    return {
        "status": "success",
        "message": f"Successfully applied {len(audit_logs)} rating adjustments",
        "audit_logs": audit_logs,
    }


@router.post("/rating-management/reverse/{log_id}")
async def reverse_rating_endpoint(
    log_id: str,
    payload: Optional[RatingReversalRequest] = None,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Applies a compensating transaction to reverse an earlier manual rating adjustment.
    """
    reversal_log = await reverse_rating_adjustment(
        db=db,
        admin_user=admin_user,
        log_id=log_id,
        reason=payload.reason if payload else None,
    )
    return {
        "status": "success",
        "message": "Rating adjustment successfully reversed",
        "reversal_log": reversal_log,
    }


@router.get("/rating-management/audit-logs")
async def get_rating_audit_logs_endpoint(
    game_id: Optional[str] = None,
    player_id: Optional[str] = None,
    limit: int = 50,
    skip: int = 0,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns immutable audit logs of rating adjustments.
    """
    query: Dict[str, Any] = {}
    if game_id and game_id != "all":
        query["$or"] = [{"game_id": game_id}, {"game_slug": game_id}]
    if player_id:
        query["player_id"] = player_id

    total = await db.rating_audit_logs.count_documents(query)
    cursor = db.rating_audit_logs.find(query).sort("created_at", -1).skip(skip).limit(limit)
    logs = await cursor.to_list(limit)

    return {
        "status": "success",
        "total": total,
        "logs": logs,
    }





