import json
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.game import Game
from app.models.team import Team, TeamMember
from app.models.tournament import Tournament, TournamentRegistration
from app.schemas import TournamentResponse, TournamentRegisterRequest

router = APIRouter()

@router.get("", response_model=List[TournamentResponse])
async def list_tournaments(
    game: Optional[str] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(Tournament)
    result = await db.execute(query)
    tournaments = result.scalars().all()

    response = []
    for t in tournaments:
        g_res = await db.execute(select(Game).where(Game.id == t.game_id))
        game_obj = g_res.scalars().first()
        if game and game_obj and game_obj.slug != game:
            continue

        reg_count = (
            await db.execute(
                select(func.count(TournamentRegistration.id)).where(
                    TournamentRegistration.tournament_id == t.id
                )
            )
        ).scalar() or 0

        rules = json.loads(t.rules_json) if t.rules_json else []
        schedule = json.loads(t.schedule_json) if t.schedule_json else []
        prizes = json.loads(t.prizes_json) if t.prizes_json else []
        bracket = json.loads(t.bracket_json) if t.bracket_json else []

        response.append(
            TournamentResponse(
                id=t.id,
                slug=t.slug,
                title=t.title,
                game_slug=game_obj.slug if game_obj else "valorant",
                game_name=game_obj.name if game_obj else "Valorant",
                prize_pool=f"₹{t.prize_pool_inr:,}",
                prize_pool_inr=t.prize_pool_inr,
                entry_fee=f"₹{t.entry_fee_inr} / Team" if t.entry_fee_inr > 0 else "₹0 / Free",
                entry_fee_inr=t.entry_fee_inr,
                status=t.status,
                starts_at=t.starts_at,
                roster_lock_time=t.roster_lock_time,
                server_region=t.server_region,
                registered_teams=reg_count,
                max_teams=t.max_teams,
                rank_requirement=t.rank_requirement,
                format=t.format,
                banner_url=t.banner_url,
                badge_color=t.badge_color,
                description=t.description,
                rules=rules,
                schedule=schedule,
                prizes=prizes,
                bracket_rounds=bracket,
                registered_rosters=[],
            )
        )
    return response

@router.get("/{slug}", response_model=TournamentResponse)
async def get_tournament_by_slug(slug: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Tournament).where(Tournament.slug == slug))
    t = result.scalars().first()
    if not t:
        raise HTTPException(status_code=404, detail="Tournament not found")

    g_res = await db.execute(select(Game).where(Game.id == t.game_id))
    game_obj = g_res.scalars().first()

    reg_count = (
        await db.execute(
            select(func.count(TournamentRegistration.id)).where(
                TournamentRegistration.tournament_id == t.id
            )
        )
    ).scalar() or 0

    rules = json.loads(t.rules_json) if t.rules_json else []
    schedule = json.loads(t.schedule_json) if t.schedule_json else []
    prizes = json.loads(t.prizes_json) if t.prizes_json else []
    bracket = json.loads(t.bracket_json) if t.bracket_json else []

    return TournamentResponse(
        id=t.id,
        slug=t.slug,
        title=t.title,
        game_slug=game_obj.slug if game_obj else "valorant",
        game_name=game_obj.name if game_obj else "Valorant",
        prize_pool=f"₹{t.prize_pool_inr:,}",
        prize_pool_inr=t.prize_pool_inr,
        entry_fee=f"₹{t.entry_fee_inr} / Team" if t.entry_fee_inr > 0 else "₹0 / Free",
        entry_fee_inr=t.entry_fee_inr,
        status=t.status,
        starts_at=t.starts_at,
        roster_lock_time=t.roster_lock_time,
        server_region=t.server_region,
        registered_teams=reg_count,
        max_teams=t.max_teams,
        rank_requirement=t.rank_requirement,
        format=t.format,
        banner_url=t.banner_url,
        badge_color=t.badge_color,
        description=t.description,
        rules=rules,
        schedule=schedule,
        prizes=prizes,
        bracket_rounds=bracket,
        registered_rosters=[],
    )

@router.post("/{id}/register")
async def register_team_for_tournament(
    id: str,
    payload: TournamentRegisterRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t_res = await db.execute(select(Tournament).where(Tournament.id == id))
    tourn = t_res.scalars().first()
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    team_res = await db.execute(select(Team).where(Team.id == payload.team_id))
    team = team_res.scalars().first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    # Check captain authorization
    if team.captain_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only team captain can register team for tournaments")

    # Check already registered
    existing_reg = (
        await db.execute(
            select(TournamentRegistration).where(
                TournamentRegistration.tournament_id == tourn.id,
                TournamentRegistration.team_id == team.id,
            )
        )
    ).scalars().first()
    if existing_reg:
        raise HTTPException(status_code=400, detail="Team already registered for this tournament")

    reg = TournamentRegistration(
        tournament_id=tourn.id,
        team_id=team.id,
        payment_status="PAID" if tourn.entry_fee_inr > 0 else "FREE",
        order_id="order_rzp_mock_123" if tourn.entry_fee_inr > 0 else None,
        check_in_status="PENDING",
    )
    db.add(reg)
    await db.commit()

    return {
        "status": "success",
        "message": "Team successfully registered for tournament",
        "order_id": reg.order_id,
        "entry_fee_inr": tourn.entry_fee_inr,
    }
