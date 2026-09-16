from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.api.deps import get_db, get_current_user, require_super_admin, RequireTournamentAdmin
from app.models.user import User
from app.models.tournament import Tournament
from app.models.admin import TournamentAdminAssignment, AuditLog
from app.models.match import Match
from app.models.dispute import Dispute
from app.schemas import AdminAssignmentCreate, AuditLogResponse, DisputeResolve, DisputeResponse

router = APIRouter()

# ----------------- SUPER ADMIN ONLY -----------------

@router.post("/tournaments/{id}/assign-admin")
async def assign_tournament_admin(
    id: str,
    payload: AdminAssignmentCreate,
    admin_user: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    # Rule 2.2: Assign tournament admin
    assignment = TournamentAdminAssignment(
        admin_user_id=payload.admin_user_id,
        tournament_id=id,
        assigned_by=admin_user.id,
    )
    db.add(assignment)

    # Record in audit log
    log = AuditLog(
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="ASSIGN_TOURNAMENT_ADMIN",
        target=f"Admin {payload.admin_user_id} -> Tournament {id}",
    )
    db.add(log)
    await db.commit()
    return {"status": "success", "message": "Tournament Admin successfully assigned"}

@router.get("/audit-logs", response_model=List[AuditLogResponse])
async def list_audit_logs(
    admin_user: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AuditLog))
    logs = result.scalars().all()
    return [
        AuditLogResponse(
            id=l.id,
            timestamp=str(l.created_at),
            operator=l.operator,
            role=l.role,
            action=l.action,
            target=l.target,
            ip_address=l.ip_address,
        )
        for l in logs
    ]

# ----------------- TOURNAMENT ADMIN (SCOPED) -----------------

@router.get("/my-tournaments")
async def get_my_assigned_tournaments(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.role == "SUPER_ADMIN":
        result = await db.execute(select(Tournament))
        return result.scalars().all()

    # Query assigned tournaments
    result = await db.execute(
        select(Tournament)
        .join(TournamentAdminAssignment, TournamentAdminAssignment.tournament_id == Tournament.id)
        .where(TournamentAdminAssignment.admin_user_id == current_user.id)
    )
    return result.scalars().all()

@router.post("/matches/{id}/verify-result")
async def verify_match_result(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    m_res = await db.execute(select(Match).where(Match.id == id))
    match = m_res.scalars().first()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    # Verify authorization for this tournament
    if current_user.role != "SUPER_ADMIN":
        assign_check = (
            await db.execute(
                select(TournamentAdminAssignment).where(
                    TournamentAdminAssignment.admin_user_id == current_user.id,
                    TournamentAdminAssignment.tournament_id == match.tournament_id,
                )
            )
        ).scalars().first()
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
    log = AuditLog(
        operator=current_user.username,
        role=current_user.role,
        action="VERIFY_MATCH_RESULT",
        target=f"Match {match.id} (Winner: {winner_team_id})",
    )
    db.add(log)
    await db.commit()
    return {
        "status": "success",
        "message": "Match result verified and winner advanced in bracket",
        "winner_team_id": winner_team_id,
        "next_match_id": next_match.id if next_match else None,
    }


@router.post("/users/{id}/promote-organizer")
async def promote_user_to_organizer(
    id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Promote an existing player account to ORGANIZER (Rule 22).
    """
    res = await db.execute(select(User).where(User.id == id))
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.role = "ORGANIZER"

    log = AuditLog(
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="PROMOTE_ORGANIZER",
        target=f"User {user.username} ({user.id}) -> ORGANIZER",
    )
    db.add(log)
    await db.commit()
    return {"status": "success", "message": f"User {user.username} successfully promoted to ORGANIZER"}

@router.get("/users")
async def list_platform_users(
    admin_user: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    List all platform users with role indicators for Super Admin dashboard.
    """
    res = await db.execute(select(User))
    users = res.scalars().all()
    return [
        {
            "id": u.id,
            "clerk_user_id": u.clerk_user_id,
            "username": u.username,
            "display_name": u.display_name,
            "email": u.email,
            "role": u.role,
            "inr_balance": u.inr_balance,
            "created_at": str(u.created_at),
        }
        for u in users
    ]

@router.get("/disputes", response_model=List[DisputeResponse])
async def list_disputes(
    admin_user: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Dispute))
    disputes = res.scalars().all()
    return [
        DisputeResponse(
            id=d.id,
            tournament_id=d.tournament_id,
            match_id=d.match_id,
            submitted_by=d.submitted_by,
            reason=d.reason,
            description=d.description,
            evidence_url=d.evidence_url,
            status=d.status,
            created_at=str(d.created_at),
        )
        for d in disputes
    ]

@router.post("/disputes/{id}/resolve")
async def resolve_dispute(
    id: str,
    payload: DisputeResolve,
    admin_user: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Dispute).where(Dispute.id == id))
    dispute = res.scalars().first()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")

    dispute.status = payload.decision
    dispute.resolution_notes = payload.resolution_notes
    dispute.resolved_by = admin_user.id

    log = AuditLog(
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="RESOLVE_DISPUTE",
        target=f"Dispute {dispute.id} ({payload.decision})",
    )
    db.add(log)
    await db.commit()
    return {
        "status": "success",
        "message": f"Dispute {dispute.id} marked as {payload.decision} by Super Admin",
    }
