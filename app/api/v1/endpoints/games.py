from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.api.deps import get_db
from app.models.game import Game
from app.models.tournament import Tournament
from app.schemas import GameResponse

router = APIRouter()

@router.get("", response_model=List[GameResponse])
async def list_games(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Game))
    games = result.scalars().all()
    
    response = []
    for g in games:
        t_res = await db.execute(
            select(func.count(Tournament.id)).where(Tournament.game_id == g.id)
        )
        count = t_res.scalar() or 0
        data = GameResponse(
            id=g.id,
            slug=g.slug,
            name=g.name,
            category=g.category,
            platform=g.platform,
            tag=g.tag,
            is_team_game=g.is_team_game,
            min_team_size=g.min_team_size,
            max_team_size=g.max_team_size,
            banner_url=g.banner_url,
            logo_url=g.logo_url,
            description=g.description,
            server_region=g.server_region,
            anti_cheat=g.anti_cheat,
            match_protocol=g.match_protocol,
            accent_color=g.accent_color,
            active_tournaments_count=count,
        )
        response.append(data)
    return response

@router.get("/{slug}", response_model=GameResponse)
async def get_game_by_slug(slug: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Game).where(Game.slug == slug))
    g = result.scalars().first()
    if not g:
        raise HTTPException(status_code=404, detail="Game not found")
    
    t_res = await db.execute(
        select(func.count(Tournament.id)).where(Tournament.game_id == g.id)
    )
    count = t_res.scalar() or 0
    return GameResponse(
        id=g.id,
        slug=g.slug,
        name=g.name,
        category=g.category,
        platform=g.platform,
        tag=g.tag,
        is_team_game=g.is_team_game,
        min_team_size=g.min_team_size,
        max_team_size=g.max_team_size,
        banner_url=g.banner_url,
        logo_url=g.logo_url,
        description=g.description,
        server_region=g.server_region,
        anti_cheat=g.anti_cheat,
        match_protocol=g.match_protocol,
        accent_color=g.accent_color,
        active_tournaments_count=count,
    )
