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
from app.schemas import MatchResponse, MatchResultSubmit

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

    return {"status": "success", "message": "Captain successfully checked in"}

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

    match.score_a = payload.score_a
    match.score_b = payload.score_b
    match.evidence_url = payload.evidence_url
    match.captain_notes = payload.captain_notes
    match.status = "PENDING_VERIFICATION"

    await db.commit()
    return {"status": "success", "message": "Match result & proof submitted for admin verification"}
