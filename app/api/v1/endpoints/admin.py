from typing import List, Optional, Dict, Any
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, get_current_user, require_super_admin, require_organizer
from app.models.user import User
from app.models.admin import TournamentAdminAssignment, AuditLog
from app.models.match import Match
from app.models.dispute import Dispute
from app.schemas import AdminAssignmentCreate, AuditLogResponse, DisputeResolve, DisputeResponse
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
    privileged_roles = ["SUPER_ADMIN", "TOURNAMENT_ADMIN", "MATCH_ARBITER", "ORGANIZER"]
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
    privileged_roles = ["SUPER_ADMIN", "TOURNAMENT_ADMIN", "MATCH_ARBITER", "ORGANIZER"]
    if current_user.role not in privileged_roles:
        raise HTTPException(status_code=403, detail="Forbidden: Match arbiter authority required")

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
    privileged_roles = ["SUPER_ADMIN", "TOURNAMENT_ADMIN", "MATCH_ARBITER", "ORGANIZER"]
    if current_user.role not in privileged_roles:
        raise HTTPException(status_code=403, detail="Forbidden: Match arbiter authority required")

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
    privileged_roles = ["SUPER_ADMIN", "TOURNAMENT_ADMIN", "MATCH_ARBITER", "ORGANIZER"]
    if current_user.role not in privileged_roles:
        raise HTTPException(status_code=403, detail="Forbidden: Match arbiter authority required")

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
        "TOURNAMENT_ADMIN",
        "MATCH_ARBITER",
        "COMMUNITY_MODERATOR",
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
            "inr_balance": u.get("inr_balance", "0"),
            "created_at": str(u.get("created_at")),
        }
        for u in users
    ]

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

    return {
        "total_users": total_users,
        "total_tournaments": total_tournaments,
        "active_tournaments": active_tournaments,
        "total_teams": total_teams,
        "total_games": total_games,
        "pending_disputes": pending_disputes,
        "active_matches": active_matches,
        "platform_escrow_vault": 1245000,
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
        query["status"] = status_filter.upper()
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

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    await db.game_payments.update_one(
        {"_id": payment["_id"]},
        {
            "$set": {
                "status": "APPROVED",
                "verified_by": admin_user.username,
                "verified_at": now,
            }
        }
    )

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

    if payment.get("payment_type") == "GAME_ENTRY" and payment.get("team_id"):
        await db.teams.update_one(
            {"$or": [{"_id": payment["team_id"]}, {"id": payment["team_id"]}]},
            {"$set": {"payment_status": "PAYMENT_REJECTED"}}
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

    await db.payouts.update_one(
        {"_id": payout["_id"]},
        {
            "$set": {
                "status": "PAID",
                "outgoing_utr": out_utr,
                "admin_id": admin_user.id,
                "paid_at": now,
            }
        }
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

    # Release reserved funds back to user's wallet
    if payout.get("payout_type") == "WALLET_WITHDRAWAL":
        await release_reserved_funds(
            db=db,
            user_id=payout["user_id"],
            amount_paise=payout["amount_paise"],
        )

    await db.payouts.update_one(
        {"_id": payout["_id"]},
        {
            "$set": {
                "status": "REJECTED",
                "rejection_reason": payload.rejection_reason,
                "admin_id": admin_user.id,
            }
        }
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

# ----------------- DEVELOPMENT DUMMY DATA CONTROLS -----------------

@router.post("/dev/seed-dummy")
async def seed_dummy_data_endpoint(
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from app.seed_dummy import run_seed_dummy
    result = await run_seed_dummy(db)
    return {"status": "success", "message": "Dummy data generated successfully", "details": result}

@router.post("/dev/remove-dummy")
async def remove_dummy_data_endpoint(
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from app.remove_dummy import run_remove_dummy
    result = await run_remove_dummy(db)
    return {"status": "success", "message": "Dummy data removed successfully", "details": result}



