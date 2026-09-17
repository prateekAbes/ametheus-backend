import json
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.game import Game
from app.models.team import Team, TeamMember
from app.models.tournament import Tournament, TournamentRegistration
from app.schemas import TournamentResponse, TournamentRegisterRequest

router = APIRouter()

def safe_json_loads(val, default):
    if not val:
        return default
    if isinstance(val, (list, dict)):
        return val
    try:
        return json.loads(val)
    except Exception:
        return default

@router.get("", response_model=List[TournamentResponse])
async def list_tournaments(
    game: Optional[str] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(Tournament)
    if status:
        query = query.where(Tournament.status == status)

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

        rules = safe_json_loads(t.rules_json, [])
        schedule = safe_json_loads(t.schedule_json, [])
        prizes = safe_json_loads(t.prizes_json, [])
        bracket = safe_json_loads(t.bracket_json, [])

        response.append(
            TournamentResponse(
                id=t.id,
                slug=t.slug,
                organizer_id=t.organizer_id,
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
                banner_url=t.banner_url or "https://lh3.googleusercontent.com/aida-public/AB6AXuD-g0bK72p2tQe7b3Z3F5WzX8oV0h4vM1bX2m9x_banner",
                badge_color=t.badge_color or "#E56A10",
                description=t.description or "",
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
    result = await db.execute(
        select(Tournament).where(
            (Tournament.slug == slug) | (Tournament.id == slug)
        )
    )
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

    rules = safe_json_loads(t.rules_json, [])
    schedule = safe_json_loads(t.schedule_json, [])
    prizes = safe_json_loads(t.prizes_json, [])
    bracket = safe_json_loads(t.bracket_json, [])

    return TournamentResponse(
        id=t.id,
        slug=t.slug,
        organizer_id=t.organizer_id,
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
        banner_url=t.banner_url or "https://lh3.googleusercontent.com/aida-public/AB6AXuD-g0bK72p2tQe7b3Z3F5WzX8oV0h4vM1bX2m9x_banner",
        badge_color=t.badge_color or "#E56A10",
        description=t.description or "",
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
    """
    Section 39: Complete Tournament Registration Validation:
    - Tournament exists
    - Registration is currently open
    - Tournament capacity has not been reached
    - Team exists
    - Player is captain/authorized
    - Team belongs to correct game discipline
    - Team is not already registered
    """
    t_res = await db.execute(select(Tournament).where(Tournament.id == id))
    tourn = t_res.scalars().first()
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    # 1. Registration open check
    if tourn.status != "REGISTRATION_OPEN":
        raise HTTPException(
            status_code=400,
            detail=f"Registration is not open for this tournament (current status: {tourn.status})",
        )

    # 2. Capacity check
    reg_count = (
        await db.execute(
            select(func.count(TournamentRegistration.id)).where(
                TournamentRegistration.tournament_id == tourn.id
            )
        )
    ).scalar() or 0

    if reg_count >= tourn.max_teams:
        raise HTTPException(
            status_code=400,
            detail=f"Tournament capacity of {tourn.max_teams} squads has been reached",
        )

    # 3. Team check
    team_res = await db.execute(select(Team).where(Team.id == payload.team_id))
    team = team_res.scalars().first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    # 4. Captain authorization
    if team.captain_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the verified squad captain can register this team",
        )

    # 5. Game discipline match
    if team.game_id != tourn.game_id:
        raise HTTPException(
            status_code=400,
            detail="Your squad is registered for a different competitive title than this tournament",
        )

    # 6. Duplicate registration check
    existing_reg = (
        await db.execute(
            select(TournamentRegistration).where(
                TournamentRegistration.tournament_id == tourn.id,
                TournamentRegistration.team_id == team.id,
            )
        )
    ).scalars().first()

    if existing_reg:
        raise HTTPException(status_code=400, detail="Team is already registered for this tournament")

    # 7. Create registration record
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
        "message": f'Squad "{team.name}" successfully registered for {tourn.title}!',
        "order_id": reg.order_id,
        "entry_fee_inr": tourn.entry_fee_inr,
    }
