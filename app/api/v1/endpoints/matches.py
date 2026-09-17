from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.tournament import Tournament
from app.models.game import Game
from app.models.team import Team, TeamMember
from app.models.match import Match
from app.schemas import MatchResponse, MatchResultSubmit, DisputeCreate
from app.models.dispute import Dispute

router = APIRouter()

@router.get("", response_model=List[MatchResponse])
async def list_matches(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Match))
    matches = result.scalars().all()

    response = []
    for m in matches:
        t_res = await db.execute(select(Tournament).where(Tournament.id == m.tournament_id))
        tourn = t_res.scalars().first()
        game_res = await db.execute(select(Game).where(Game.id == tourn.game_id)) if tourn else None
        game = game_res.scalars().first() if game_res else None

        team_a = (await db.execute(select(Team).where(Team.id == m.team_a_id))).scalars().first() if m.team_a_id else None
        team_b = (await db.execute(select(Team).where(Team.id == m.team_b_id))).scalars().first() if m.team_b_id else None

        response.append(
            MatchResponse(
                id=m.id,
                tournament_id=m.tournament_id,
                tournament_title=tourn.title if tourn else "Championship",
                game_slug=game.slug if game else "valorant",
                game_name=game.name if game else "Valorant",
                stage=m.stage,
                format=m.format,
                scheduled_time=m.scheduled_time,
                status=m.status,
                server_region=m.server_region,
                lobby_code=m.lobby_code,
                lobby_password=m.lobby_password,
                team_a={"name": team_a.name, "tag": team_a.tag, "score": m.score_a} if team_a else None,
                team_b={"name": team_b.name, "tag": team_b.tag, "score": m.score_b} if team_b else None,
                evidence_url=m.evidence_url,
                dispute_notes=m.dispute_notes,
            )
        )
    return response

@router.post("/{id}/check-in")
async def captain_check_in(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    m_res = await db.execute(select(Match).where(Match.id == id))
    match = m_res.scalars().first()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    # Verify user is a roster member of either competing team or admin
    if (match.team_a_id or match.team_b_id) and current_user.role not in ["SUPER_ADMIN", "ORGANIZER"]:
        competing_teams = [tid for tid in [match.team_a_id, match.team_b_id] if tid]
        mem_check = await db.execute(
            select(TeamMember).where(
                TeamMember.team_id.in_(competing_teams),
                TeamMember.user_id == current_user.id,
            )
        )
        if not mem_check.scalars().first():
            raise HTTPException(
                status_code=403,
                detail="You are not registered on either competing roster for this match.",
            )

    if match.status == "CHECK_IN_OPEN":
        match.status = "LIVE"

    from app.services.audit import record_audit_log
    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="MATCH_CHECK_IN",
        target=f"Match {match.id}",
        details=f"Player @{current_user.username} checked in. Match status is {match.status}.",
    )
    await db.commit()
    return {"status": "success", "message": f"Check-in verified. Match is {match.status}."}

@router.post("/{id}/submit-result")
async def submit_match_result(
    id: str,
    payload: MatchResultSubmit,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    m_res = await db.execute(select(Match).where(Match.id == id))
    match = m_res.scalars().first()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    # Verify user is a roster member of either competing team or admin
    if (match.team_a_id or match.team_b_id) and current_user.role not in ["SUPER_ADMIN", "ORGANIZER"]:
        competing_teams = [tid for tid in [match.team_a_id, match.team_b_id] if tid]
        mem_check = await db.execute(
            select(TeamMember).where(
                TeamMember.team_id.in_(competing_teams),
                TeamMember.user_id == current_user.id,
            )
        )
        if not mem_check.scalars().first():
            raise HTTPException(
                status_code=403,
                detail="Only participating roster members or platform arbiters can submit scores.",
            )

    match.score_a = payload.score_a
    match.score_b = payload.score_b
    match.evidence_url = payload.evidence_url
    match.captain_notes = payload.captain_notes
    match.status = "PENDING_VERIFICATION"

    from app.services.audit import record_audit_log
    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="SUBMIT_MATCH_RESULT",
        target=f"Match {match.id} (Score: {payload.score_a}-{payload.score_b})",
        details=f"Proof: {payload.evidence_url or 'None'} | Notes: {payload.captain_notes or 'None'}",
    )
    await db.commit()

    from app.core.ws import ws_manager
    await ws_manager.broadcast({
        "type": "MATCH_SCORE_SUBMITTED",
        "match_id": match.id,
        "score_a": payload.score_a,
        "score_b": payload.score_b,
        "status": "PENDING_VERIFICATION",
    })

    return {"status": "success", "message": "Match result & proof submitted for admin verification"}

@router.post("/{id}/dispute")
async def create_match_dispute(
    id: str,
    payload: DisputeCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    m_res = await db.execute(select(Match).where(Match.id == id))
    match = m_res.scalars().first()
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    dispute = Dispute(
        tournament_id=match.tournament_id,
        match_id=match.id,
        submitted_by=current_user.id,
        reason=payload.reason,
        description=payload.description,
        evidence_url=payload.evidence_url,
        status="OPEN",
    )
    db.add(dispute)
    match.status = "DISPUTED"
    match.dispute_notes = payload.description
    await db.commit()
    await db.refresh(dispute)

    return {
        "status": "success",
        "message": "Dispute opened and match frozen for administrative review",
        "dispute_id": dispute.id,
    }
