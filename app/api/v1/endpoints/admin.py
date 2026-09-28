from typing import List, Optional, Dict, Any
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, get_current_user, require_super_admin, require_organizer
from app.models.user import User
from app.models.admin import TournamentAdminAssignment, AuditLog
from app.models.match import Match
from app.models.tournament import Tournament, TournamentStage, StageRound
from app.models.dispute import Dispute
from app.schemas import (
    AdminAssignmentCreate,
    AuditLogResponse,
    DisputeResolve,
    DisputeResponse,
    AdminMatchPairRequest,
    AdminMatchCreatePayload,
    AdminMatchUpdatePayload,
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

@router.get("/audit-logs", response_model=List[AuditLogResponse])
async def list_audit_logs(
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    cursor = db.audit_logs.find().sort("created_at", -1).limit(100)
    logs = await cursor.to_list(length=100)
    return [
        AuditLogResponse(
            id=str(l.get("_id", l.get("id"))),
            timestamp=str(l.get("created_at")),
            operator=l["operator"],
            role=l["role"],
            action=l["action"],
            target=l["target"],
            ip_address=l.get("ip_address", "127.0.0.1"),
            prev_hash=l.get("prev_hash"),
            record_hash=l.get("record_hash"),
        )
        for l in logs
    ]

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

@router.get("/payments")
async def list_admin_payments(
    status_filter: Optional[str] = None,
    game_id: Optional[str] = None,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = {"is_dummy": {"$ne": True}}
    if status_filter and status_filter.upper() != "ALL":
        sf = status_filter.upper()
        query["status"] = sf
        if sf == "PENDING":
            query["utr"] = {"$exists": True, "$type": "string", "$ne": ""}
    if game_id:
        query["game_id"] = game_id

    cursor = db.game_payments.find(query).sort("created_at", -1)
    docs = await cursor.to_list(length=200)
    for d in docs:
        d["id"] = str(d.get("_id", d.get("id")))
        d.pop("_id", None)
    return docs

@router.post("/payments/{payment_id}/approve")
async def approve_payment(
    payment_id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from bson import ObjectId
    p_query = {"$or": [{"_id": payment_id}, {"id": payment_id}]}
    if ObjectId.is_valid(payment_id):
        p_query["$or"].append({"_id": ObjectId(payment_id)})

    payment = await db.game_payments.find_one(p_query)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment record not found")

    if payment.get("status") == "APPROVED":
        return {"status": "success", "message": "Payment was already approved"}

    if not payment.get("utr") or not str(payment.get("utr")).strip():
        raise HTTPException(
            status_code=400,
            detail="Cannot approve payment without a valid submitted UTR reference.",
        )

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    # Transition from any valid pending status to APPROVED
    valid_statuses = ["PENDING", "PENDING_VERIFICATION", "SUBMITTED"]
    update_res = await db.game_payments.update_one(
        {"_id": payment["_id"], "status": {"$in": valid_statuses}},
        {
            "$set": {
                "status": "APPROVED",
                "verified_by": admin_user.username,
                "verified_at": now,
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
    await create_notification(
        db=db,
        user_id=payment["user_id"],
        title="Payment Approved",
        message=f"Your payment of ₹{payment.get('amount_inr', 0)} for {payment.get('payment_type', 'Platform Service')} has been verified and approved.",
        notification_type="PAYMENT",
        link="/dashboard",
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="APPROVE_PAYMENT",
        target=f"Payment {payment_id} (₹{payment.get('amount_inr')})",
        details=f"Approved by {admin_user.username} (UTR: {payment.get('utr')})",
    )

    return {"status": "success", "message": "Payment verified and approved successfully."}

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

    # Idempotency: Prevent duplicate rejection
    if payment.get("status") == "REJECTED":
        return {"status": "success", "message": "Payment is already rejected.", "payment_id": payment_id, "new_status": "REJECTED"}

    # Business rule: Cannot reject already approved payments
    if payment.get("status") == "APPROVED":
        raise HTTPException(
            status_code=400,
            detail="Cannot reject an already approved payment. Please issue an administrative adjustment if required.",
        )

    reason = payload.rejection_reason or "Invalid transaction details"
    now_utc = datetime.now(timezone.utc)

    # Atomic transition to REJECTED
    update_res = await db.game_payments.update_one(
        {"_id": payment["_id"], "status": {"$ne": "REJECTED"}},
        {
            "$set": {
                "status": "REJECTED",
                "rejection_reason": reason,
                "verified_by": admin_user.username,
                "verified_at": now_utc,
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
            notification_type="PAYMENT",
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
            details=f"Rejected: {reason}",
        )
    except Exception as e:
        import logging
        logging.getLogger("ametheus.admin").warning(f"Failed to record audit log for payment rejection: {e}")

    return {
        "status": "success",
        "message": "Payment rejected successfully.",
        "payment_id": payment_id,
        "new_status": "REJECTED",
    }


@router.get("/payouts")
async def list_admin_payouts(
    status_filter: Optional[str] = None,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = {"is_dummy": {"$ne": True}}
    if status_filter and status_filter.upper() != "ALL":
        query["status"] = status_filter.upper()

    cursor = db.payouts.find(query).sort("created_at", -1)
    docs = await cursor.to_list(length=200)
    for d in docs:
        d["id"] = str(d.get("_id", d.get("id")))
        d.pop("_id", None)
        d["amount_inr"] = d.get("amount_paise", 0) / 100
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
        notification_type="PAYMENT",
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
        notification_type="PAYMENT",
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

    match = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    valid_statuses = ["DRAFT", "SCHEDULED", "CHECK_IN_OPEN", "LIVE", "COMPLETED", "CANCELLED"]
    stat_upper = payload.status.upper()
    if stat_upper not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid match status: {payload.status}")

    await db.matches.update_one({"_id": match["_id"]}, {"$set": {"status": stat_upper}})

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

    return {"status": "success", "message": f"Match status updated to {stat_upper}"}

@router.post("/matches/{id}/score")
async def update_match_score_admin(
    id: str,
    payload: AdminMatchScoreUpdate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from app.services.bracket_engine import advance_bracket_match
    from app.core.ws import ws_manager

    match_doc = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")

    match = Match.from_mongo(match_doc)
    winner_id = payload.winner_team_id
    if not winner_id and payload.complete_match:
        winner_id = match.team_a_id if payload.score_a >= payload.score_b else match.team_b_id

    update_doc: dict[str, Any] = {
        "score_a": payload.score_a,
        "score_b": payload.score_b,
    }
    if payload.complete_match:
        update_doc["status"] = "COMPLETED"
        update_doc["winner_team_id"] = winner_id

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

    await ws_manager.broadcast({
        "type": "MATCH_SCORE_SUBMITTED",
        "match_id": match.id,
        "score_a": payload.score_a,
        "score_b": payload.score_b,
        "status": "COMPLETED" if payload.complete_match else match.status,
    })

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="ADMIN_UPDATE_MATCH_SCORE",
        target=f"Match {match.id} ({payload.score_a} - {payload.score_b})",
        details=f"Winner: {winner_id or 'In Progress'} | Complete: {payload.complete_match}",
    )

    return {
        "status": "success",
        "message": f"Scores updated to {payload.score_a}-{payload.score_b}. Match {'completed' if payload.complete_match else 'in progress'}.",
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
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Generates Battle Royale match lobbies distributing the actual registered teams.
    Saves to db.matches and marks tournament format as Battle Royale.
    """
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

    calc = calculate_battle_royale_parameters(
        actual_registered_teams=len(teams_list),
        room_capacity=room_capacity,
        players_per_team=players_per_team,
    )

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
                max_teams_per_match=calc["max_teams_per_match"],
                required_matches=calc["required_matches"],
                balanced=payload.balanced,
            )
    else:
        match_partitions = distribute_teams_into_matches(
            teams=teams_list,
            max_teams_per_match=calc["max_teams_per_match"],
            required_matches=calc["required_matches"],
            balanced=payload.balanced,
        )

    stage_name = payload.stage_name or "Round 1"
    round_name = payload.round_name or "Round 1"

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
        team_a_id = chunk[0]["id"] if len(chunk) > 0 else None
        team_a_name = chunk[0]["name"] if len(chunk) > 0 else "Lobby"
        team_b_id = chunk[1]["id"] if len(chunk) > 1 else None
        team_b_name = f"+{len(chunk) - 1} Teams" if len(chunk) > 1 else None

        match_doc = Match(
            id=m_id,
            tournament_id=tid,
            stage_name=stage_name,
            round_name=round_name,
            stage=round_name,
            match_type="BATTLE_ROYALE",
            round_number=1,
            match_number=match_num,
            format="Battle Royale",
            series_format="BO1",
            status="SCHEDULED",
            room_capacity=calc["room_capacity"],
            players_per_team=calc["players_per_team"],
            max_teams=calc["max_teams_per_match"],
            teams=chunk,
            team_ids=team_ids,
            team_a_id=team_a_id,
            team_a_name=team_a_name,
            team_b_id=team_b_id,
            team_b_name=team_b_name,
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

    # Delete existing matches for this tournament to cleanly replace with approved structure
    await db.matches.delete_many({
        "$or": [{"tournament_id": tid}, {"tournament_id": tslug}]
    })

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

        db_match = Match(
            id=match_id,
            tournament_id=tid,
            stage=stage,
            match_type=m_type,
            round_number=r_num,
            match_number=m_num,
            format=s_fmt,
            series_format=s_fmt,
            scheduled_time=sched_combined,
            scheduled_at=parsed_dt,
            status="SCHEDULED",
            team_a_id=item.team_a_id,
            team_b_id=item.team_b_id,
            team_a_name=item.team_a_name,
            team_b_name=item.team_b_name,
            next_match_id=item.next_match_id,
            next_match_slot=item.next_match_slot,
            advancement_label=item.advancement_label,
            join_info_enabled=item.join_info_enabled or False,
            join_info=item.join_info,
            match_message=item.match_message,
            youtube_live_url=item.youtube_live_url or None,
            live_link=item.youtube_live_url or None,
            server_region=tourn_doc.get("server_region", "Asia (Mumbai)"),
            lobby_code=f"AMT-R{r_num}M{m_num}",
            lobby_password=f"AMT-{secrets.token_hex(3).upper()}",
        )
        await db.matches.insert_one(db_match.to_mongo())
        saved_matches.append(db_match.id)

        if r_num not in bracket_rounds_map:
            bracket_rounds_map[r_num] = {
                "round_number": r_num,
                "round_name": f"Round {r_num}",
                "matches": []
            }
        bracket_rounds_map[r_num]["matches"].append({
            "id": db_match.id,
            "stage": stage,
            "match_type": m_type,
            "series_format": s_fmt,
            "match_number": m_num,
            "team_a_id": item.team_a_id,
            "team_b_id": item.team_b_id,
            "youtube_live_url": item.youtube_live_url,
            "scheduled_time": sched_combined,
            "status": "SCHEDULED",
        })

    bracket_tree = [bracket_rounds_map[k] for k in sorted(bracket_rounds_map.keys())]

    # Save to tournament
    await db.tournaments.update_one(
        {"_id": tourn_doc["_id"]},
        {"$set": {
            "match_format": payload.match_format,
            "tournament_type": payload.match_format,
            "series_format": payload.series_format,
            "bracket_rounds": bracket_tree,
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
        live_link=payload.youtube_live_url,
        youtube_live_url=payload.youtube_live_url,
        status=payload.status or "SCHEDULED",
        team_a_id=payload.team_a_id,
        team_b_id=payload.team_b_id,
        team_a_name=name_a,
        team_b_name=name_b,
        team_a_placeholder=payload.team_a_placeholder,
        team_b_placeholder=payload.team_b_placeholder,
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

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="CREATE_MATCH",
        target=f"Match {match.id} ({name_a} vs {name_b})",
        details=f"Round: {round_name} | Series: {series_fmt} | Time: {scheduled_time_str}",
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
    match = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
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

    if update_fields:
        await db.matches.update_one({"_id": match["_id"]}, {"$set": update_fields})
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
            u_id_str = str(user.get("_id", user.get("id")))
            await create_notification(
                db=db,
                user_id=u_id_str,
                title=payload.title,
                message=payload.message,
                notification_type=payload.notification_type or "ADMIN",
                link=payload.link,
            )
            await ws_manager.broadcast({
                "type": "NOTIFICATION_RECEIVED",
                "user_id": u_id_str,
                "title": payload.title,
                "message": payload.message,
                "link": payload.link,
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
    valid_formats = ["Knockout", "Round Robin", "Combination", "Swiss System", "Ladder Tournament"]
    match_fmt = None
    for vf in valid_formats:
        if vf.lower() == fmt_clean.lower():
            match_fmt = vf
            break
    if not match_fmt:
        match_fmt = fmt_clean

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

    match = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
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

    match_doc = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
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

    valid_targets = {
        str(match.team_a_id) if match.team_a_id else None,
        str(match.team_b_id) if match.team_b_id else None,
        team_a_name,
        team_b_name,
        team_a_ph,
        team_b_ph,
    }
    valid_targets = {t for t in valid_targets if t}

    if winner_id and valid_targets and winner_id not in valid_targets:
        # Check if winner_id is a team ID in db whose name matches team_a or team_b
        winner_team = await db.teams.find_one({"$or": [{"_id": winner_id}, {"id": winner_id}]})
        if winner_team:
            w_name = winner_team.get("name")
            if w_name in valid_targets:
                winner_id = str(winner_team.get("_id", winner_team.get("id")))

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

    return {
        "status": "success",
        "message": f"Match completed successfully! Final score: {score_a} - {score_b}.",
        "match_id": match.id,
        "score_a": score_a,
        "score_b": score_b,
        "winner_id": winner_id,
        "winner_team_id": winner_id,
    }

@router.post("/tournaments/{id}/rounds")
async def add_custom_tournament_round(
    id: str,
    payload: CreateRoundPayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Rule 1: Admin dynamically creates and names a new tournament round.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    clean_name = payload.round_name.strip()
    if not clean_name:
        raise HTTPException(status_code=400, detail="Round name cannot be empty")

    await db.tournaments.update_one(
        {"_id": tourn["_id"]},
        {"$addToSet": {"custom_rounds": clean_name}}
    )

    existing_rounds = list(tourn.get("custom_rounds") or [])
    if clean_name not in existing_rounds:
        existing_rounds.append(clean_name)

    return {
        "status": "success",
        "message": f'Round "{clean_name}" created successfully.',
        "round_name": clean_name,
        "custom_rounds": existing_rounds,
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

    platform = payload.community_platform
    if platform and platform.upper() in ["DISCORD", "WHATSAPP"]:
        platform = platform.upper()
    else:
        platform = None

    link = payload.community_link.strip() if payload.community_link else None

    await db.tournaments.update_one(
        {"_id": tourn["_id"]},
        {"$set": {
            "community_platform": platform,
            "community_link": link,
        }}
    )

    return {
        "status": "success",
        "message": "Tournament community settings updated successfully.",
        "community_platform": platform,
        "community_link": link,
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


@router.patch("/tournaments/{tournament_id}/stream")
async def update_tournament_stream(
    tournament_id: str,
    payload: TournamentStreamUpdatePayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from bson import ObjectId
    t_query = {"$or": [{"id": tournament_id}, {"_id": tournament_id}]}
    if ObjectId.is_valid(tournament_id):
        t_query["$or"].append({"_id": ObjectId(tournament_id)})

    res = await db.tournaments.update_one(
        t_query,
        {"$set": {"live_stream_url": payload.live_stream_url}}
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Tournament not found")

    return {
        "status": "success",
        "message": "Tournament live stream URL updated",
        "live_stream_url": payload.live_stream_url
    }



