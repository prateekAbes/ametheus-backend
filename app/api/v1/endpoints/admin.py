from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.api.deps import get_db, get_current_user, require_super_admin, RequireTournamentAdmin
from app.models.user import User
from app.models.tournament import Tournament
from app.models.admin import TournamentAdminAssignment, AuditLog
from app.models.match import Match
from app.schemas import AdminAssignmentCreate, AuditLogResponse

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

    match.status = "COMPLETED"
    if (match.score_a or 0) > (match.score_b or 0):
        match.winner_team_id = match.team_a_id
    else:
        match.winner_team_id = match.team_b_id

    # Add audit log
    log = AuditLog(
        operator=current_user.username,
        role=current_user.role,
        action="VERIFY_MATCH_RESULT",
        target=f"Match {match.id} (Winner: {match.winner_team_id})",
    )
    db.add(log)
    await db.commit()
    return {"status": "success", "message": "Match result verified and winner advanced in bracket"}
