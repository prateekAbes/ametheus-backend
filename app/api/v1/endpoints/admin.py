from typing import List, Optional, Dict, Any
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, get_current_user, require_super_admin, require_organizer
from app.models.user import User
from app.models.admin import TournamentAdminAssignment, AuditLog
from app.models.match import Match
from app.models.dispute import Dispute
from app.schemas import (
    AdminAssignmentCreate,
    AuditLogResponse,
    DisputeResolve,
    DisputeResponse,
    AdminMatchPairRequest,
    AdminMatchScheduleUpdate,
    AdminMatchStatusUpdate,
    AdminMatchScoreUpdate,
)
from app.services.audit import record_audit_log

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
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    List all platform users with role indicators for Super Admin dashboard.
    """
    cursor = db.users.find()
    users = await cursor.to_list(length=100)
    return [
        {
            "id": str(u.get("_id", u.get("id"))),
            "clerk_user_id": u.get("clerk_user_id"),
            "username": u["username"],
            "display_name": u.get("display_name"),
            "email": u["email"],
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
    payment = await db.game_payments.find_one({"$or": [{"_id": payment_id}, {"id": payment_id}]})
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

    # Atomic condition: only transition if status is PENDING
    update_res = await db.game_payments.update_one(
        {"_id": payment["_id"], "status": "PENDING"},
        {
            "$set": {
                "status": "APPROVED",
                "verified_by": admin_user.username,
                "verified_at": now,
            }
        }
    )

    if update_res.modified_count == 0:
        return {"status": "success", "message": "Payment was already approved or is not in PENDING status"}

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
    payment = await db.game_payments.find_one({"$or": [{"_id": payment_id}, {"id": payment_id}]})
    if not payment:
        raise HTTPException(status_code=404, detail="Payment record not found")

    reason = payload.rejection_reason or "Invalid transaction details"

    await db.game_payments.update_one(
        {"_id": payment["_id"]},
        {
            "$set": {
                "status": "REJECTED",
                "rejection_reason": reason,
                "verified_by": admin_user.username,
            }
        }
    )

    if payment.get("payment_type") == "TOURNAMENT_ENTRY":
        reg_id = payment.get("registration_id")
        reg_query = {"$or": [{"_id": reg_id}, {"id": reg_id}]} if reg_id else {"payment_id": payment_id}
        await db.tournament_registrations.update_one(
            reg_query,
            {"$set": {"payment_status": "REJECTED"}}
        )

    if payment.get("payment_type") == "GAME_ENTRY" and payment.get("team_id"):
        await db.teams.update_one(
            {"$or": [{"_id": payment["team_id"]}, {"id": payment["team_id"]}]},
            {"$set": {"payment_status": "PAYMENT_REJECTED"}}
        )

    from app.services.notification import create_notification
    await create_notification(
        db=db,
        user_id=payment["user_id"],
        title="Payment Rejected",
        message=f"Your payment of ₹{payment.get('amount_inr', 0)} was rejected: {reason}",
        notification_type="PAYMENT",
        link="/dashboard",
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="REJECT_PAYMENT",
        target=f"Payment {payment_id}",
        details=f"Rejected: {reason}",
    )

    return {"status": "success", "message": "Payment rejected."}

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

    tournaments = await db.tournaments.find({}).to_list(length=100)
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
            "status": t.get("status", "REGISTRATION_OPEN"),
            "max_teams": t.get("max_teams", 16),
            "match_format": t.get("match_format", "Best of 1"),
            "stage_formats": t.get("stage_formats") or {},
            "teams": registered_teams_list,
            "unpaired_count": len([x for x in registered_teams_list if not x["is_paired"]]),
            "paired_count": len([x for x in registered_teams_list if x["is_paired"]]),
        })

    async def enrich_match(m: dict) -> dict:
        mid = str(m.get("_id", m.get("id")))
        tourn_id = m.get("tournament_id")
        tourn = await db.tournaments.find_one({"$or": [{"_id": tourn_id}, {"id": tourn_id}, {"slug": tourn_id}]}) if tourn_id else None

        team_a_id = m.get("team_a_id")
        team_b_id = m.get("team_b_id")

        team_a = await db.teams.find_one({"$or": [{"_id": team_a_id}, {"id": team_a_id}]}) if team_a_id else None
        if not team_a and team_a_id:
            user_a = await db.users.find_one({"$or": [{"_id": team_a_id}, {"id": team_a_id}]})
            if user_a:
                team_a = {"name": user_a.get("username") or "Player A", "tag": "SOLO"}

        team_b = await db.teams.find_one({"$or": [{"_id": team_b_id}, {"id": team_b_id}]}) if team_b_id else None
        if not team_b and team_b_id:
            user_b = await db.users.find_one({"$or": [{"_id": team_b_id}, {"id": team_b_id}]})
            if user_b:
                team_b = {"name": user_b.get("username") or "Player B", "tag": "SOLO"}

        scheduled_at = m.get("scheduled_at")
        scheduled_at_iso = scheduled_at.isoformat() if isinstance(scheduled_at, datetime) else scheduled_at

        return {
            "id": mid,
            "tournament_id": tourn_id,
            "tournament_title": tourn.get("title") if tourn else "Tournament",
            "stage": m.get("stage", "Quarterfinals"),
            "format": m.get("format") or m.get("match_format") or "Best of 1",
            "order": m.get("order", 0),
            "status": m.get("status", "SCHEDULED"),
            "scheduled_time": m.get("scheduled_time", "TBD"),
            "scheduled_at": scheduled_at_iso,
            "live_link": m.get("live_link"),
            "team_a": {"id": team_a_id, "name": team_a["name"], "tag": team_a.get("tag", "TEAM"), "score": m.get("score_a", 0)} if team_a else None,
            "team_b": {"id": team_b_id, "name": team_b["name"], "tag": team_b.get("tag", "TEAM"), "score": m.get("score_b", 0)} if team_b else None,
            "winner_team_id": m.get("winner_team_id"),
            "check_in_status": m.get("check_in_status") or {"team_a": False, "team_b": False},
        }

    sched_cursor = db.matches.find({"status": {"$in": ["SCHEDULED", "CHECK_IN_OPEN"]}}).sort("created_at", -1)
    sched_raw = await sched_cursor.to_list(length=100)
    scheduled_matches = [await enrich_match(m) for m in sched_raw]

    live_cursor = db.matches.find({"status": {"$in": ["LIVE", "PENDING_VERIFICATION", "DISPUTED"]}}).sort("created_at", -1)
    live_raw = await live_cursor.to_list(length=100)
    live_matches = [await enrich_match(m) for m in live_raw]

    comp_cursor = db.matches.find({"status": "COMPLETED"}).sort("updated_at", -1).limit(50)
    comp_raw = await comp_cursor.to_list(length=50)
    completed_matches = [await enrich_match(m) for m in comp_raw]

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

# ----------------- DEVELOPMENT DUMMY DATA CONTROLS -----------------
# Removed: Production database contains only authorized competitive entities.



