from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.api.deps import get_db, require_super_admin
from app.models.user import User
from app.models.game import Game
from app.models.tournament import Tournament
from app.models.admin import AuditLog
from app.schemas import GameResponse, GameCreate, GameUpdate
from app.services.audit import record_audit_log

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

@router.post("", response_model=GameResponse, status_code=status.HTTP_201_CREATED)
async def create_game(
    payload: GameCreate,
    admin_user: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Super Admin privilege: Register a new sanctioned competitive title.
    """
    existing = await db.execute(select(Game).where(Game.slug == payload.slug))
    if existing.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Game with slug '{payload.slug}' already exists",
        )

    game = Game(
        slug=payload.slug,
        name=payload.name,
        category=payload.category,
        platform=payload.platform,
        tag=payload.tag,
        is_team_game=payload.is_team_game,
        min_team_size=payload.min_team_size,
        max_team_size=payload.max_team_size,
        banner_url=payload.banner_url,
        logo_url=payload.logo_url,
        description=payload.description,
        server_region=payload.server_region,
        anti_cheat=payload.anti_cheat,
        match_protocol=payload.match_protocol,
        accent_color=payload.accent_color or "#E56A10",
    )
    db.add(game)

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="CREATE_GAME",
        target=f"Game {game.name} ({game.slug})",
        details=f"Added new competitive game title {game.name}",
    )
    await db.commit()
    await db.refresh(game)

    return GameResponse(
        id=game.id,
        slug=game.slug,
        name=game.name,
        category=game.category,
        platform=game.platform,
        tag=game.tag,
        is_team_game=game.is_team_game,
        min_team_size=game.min_team_size,
        max_team_size=game.max_team_size,
        banner_url=game.banner_url,
        logo_url=game.logo_url,
        description=game.description,
        server_region=game.server_region,
        anti_cheat=game.anti_cheat,
        match_protocol=game.match_protocol,
        accent_color=game.accent_color,
        active_tournaments_count=0,
    )

@router.put("/{id}", response_model=GameResponse)
async def update_game(
    id: str,
    payload: GameUpdate,
    admin_user: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Super Admin privilege: Update game parameters.
    """
    res = await db.execute(select(Game).where(Game.id == id))
    game = res.scalars().first()
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    update_data = payload.model_dump(exclude_unset=True)
    for field, val in update_data.items():
        setattr(game, field, val)

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="UPDATE_GAME",
        target=f"Game {game.name} ({game.id})",
        details=f"Updated game fields: {list(update_data.keys())}",
    )
    await db.commit()
    await db.refresh(game)

    t_res = await db.execute(
        select(func.count(Tournament.id)).where(Tournament.game_id == game.id)
    )
    count = t_res.scalar() or 0

    return GameResponse(
        id=game.id,
        slug=game.slug,
        name=game.name,
        category=game.category,
        platform=game.platform,
        tag=game.tag,
        is_team_game=game.is_team_game,
        min_team_size=game.min_team_size,
        max_team_size=game.max_team_size,
        banner_url=game.banner_url,
        logo_url=game.logo_url,
        description=game.description,
        server_region=game.server_region,
        anti_cheat=game.anti_cheat,
        match_protocol=game.match_protocol,
        accent_color=game.accent_color,
        active_tournaments_count=count,
    )

@router.delete("/{id}")
async def delete_game(
    id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Super Admin privilege: Delete game title if no tournaments depend on it.
    """
    res = await db.execute(select(Game).where(Game.id == id))
    game = res.scalars().first()
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    t_res = await db.execute(
        select(func.count(Tournament.id)).where(Tournament.game_id == game.id)
    )
    count = t_res.scalar() or 0
    if count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete game '{game.name}': {count} tournament(s) are linked to it",
        )

    await db.delete(game)
    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="DELETE_GAME",
        target=f"Game {game.name} ({game.id})",
        details=f"Deleted game title {game.name}",
    )
    await db.commit()
    return {"status": "success", "message": f"Game '{game.name}' deleted"}
