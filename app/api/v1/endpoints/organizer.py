import json
import re
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel
from app.api.deps import get_db, require_organizer
from app.models.user import User
from app.models.game import Game
from app.models.tournament import Tournament, TournamentRegistration
from app.models.match import Match
from app.services.audit import record_audit_log
from app.schemas import TournamentResponse

router = APIRouter()

VALID_TRANSITIONS = {
    "DRAFT": ["REGISTRATION_OPEN", "CANCELLED"],
    "REGISTRATION_OPEN": ["REGISTRATION_CLOSED", "LIVE", "CANCELLED"],
    "REGISTRATION_CLOSED": ["UPCOMING", "LIVE", "REGISTRATION_OPEN", "CANCELLED"],
    "UPCOMING": ["LIVE", "CANCELLED"],
    "LIVE": ["COMPLETED", "CANCELLED"],
    "COMPLETED": [],
    "CANCELLED": [],
}

def validate_tournament_transition(current_status: str, next_status: str):
    allowed = VALID_TRANSITIONS.get(current_status, [])
    if next_status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Illegal tournament transition from {current_status} to {next_status}. Allowed next states: {allowed}",
        )

class TournamentCreatePayload(BaseModel):
    game_id: str
    title: str
    slug: Optional[str] = None
    prize_pool_inr: int = 0
    entry_fee_inr: int = 0
    starts_at: str
    roster_lock_time: str
    server_region: str = "AWS Mumbai (ap-south-1)"
    max_teams: int = 16
    rank_requirement: str = "Open to All Ranks"
    format: str = "Single Elimination Bracket"
    banner_url: Optional[str] = None
    description: str
    rules: List[str] = []
    schedule: List[Dict[str, Any]] = []

class TournamentUpdatePayload(BaseModel):
    title: Optional[str] = None
    prize_pool_inr: Optional[int] = None
    entry_fee_inr: Optional[int] = None
    starts_at: Optional[str] = None
    roster_lock_time: Optional[str] = None
    server_region: Optional[str] = None
    max_teams: Optional[int] = None
    rank_requirement: Optional[str] = None
    format: Optional[str] = None
    banner_url: Optional[str] = None
    description: Optional[str] = None
    rules: Optional[List[str]] = None
    schedule: Optional[List[Dict[str, Any]]] = None

def slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s

@router.post("/tournaments", response_model=TournamentResponse, status_code=status.HTTP_201_CREATED)
async def create_tournament(
    payload: TournamentCreatePayload,
    organizer: User = Depends(require_organizer),
    db: AsyncSession = Depends(get_db),
):
    """
    Organizer creates a new tournament (Rule 9). Default state is DRAFT.
    """
    g_res = await db.execute(select(Game).where(Game.id == payload.game_id))
    game = g_res.scalars().first()
    if not game:
        raise HTTPException(status_code=404, detail="Selected game discipline not found")

    base_slug = payload.slug or slugify(payload.title)
    # Ensure unique slug
    existing = await db.execute(select(Tournament).where(Tournament.slug == base_slug))
    slug = base_slug if not existing.scalars().first() else f"{base_slug}-{organizer.id[:6]}"

    banner = payload.banner_url or game.banner_url

    tourn = Tournament(
        slug=slug,
        game_id=game.id,
        organizer_id=organizer.id,
        title=payload.title,
        prize_pool_inr=payload.prize_pool_inr,
        entry_fee_inr=payload.entry_fee_inr,
        status="DRAFT",  # Initial state per Section 30
        starts_at=payload.starts_at,
        roster_lock_time=payload.roster_lock_time,
        server_region=payload.server_region,
        max_teams=payload.max_teams,
        rank_requirement=payload.rank_requirement,
        format=payload.format,
        banner_url=banner,
        badge_color=game.accent_color,
        description=payload.description,
        rules_json=json.dumps(payload.rules),
        schedule_json=json.dumps(payload.schedule),
        prizes_json=json.dumps([{"place": "1st", "amount": f"₹{payload.prize_pool_inr}"}]),
        bracket_json="[]",
    )
    db.add(tourn)
    await db.commit()
    await db.refresh(tourn)

    return TournamentResponse(
        id=tourn.id,
        slug=tourn.slug,
        organizer_id=tourn.organizer_id,
        title=tourn.title,
        game_slug=game.slug,
        game_name=game.name,
        prize_pool=f"₹{tourn.prize_pool_inr:,}",
        prize_pool_inr=tourn.prize_pool_inr,
        entry_fee=f"₹{tourn.entry_fee_inr} / Team" if tourn.entry_fee_inr > 0 else "₹0 / Free",
        entry_fee_inr=tourn.entry_fee_inr,
        status=tourn.status,
        starts_at=tourn.starts_at,
        roster_lock_time=tourn.roster_lock_time,
        server_region=tourn.server_region,
        registered_teams=0,
        max_teams=tourn.max_teams,
        rank_requirement=tourn.rank_requirement,
        format=tourn.format,
        banner_url=tourn.banner_url,
        badge_color=tourn.badge_color,
        description=tourn.description,
        rules=payload.rules,
        schedule=payload.schedule,
        prizes=[{"place": "1st", "amount": f"₹{tourn.prize_pool_inr}"}],
        bracket_rounds=[],
        registered_rosters=[],
    )

@router.put("/tournaments/{id}", response_model=TournamentResponse)
async def update_tournament(
    id: str,
    payload: TournamentUpdatePayload,
    organizer: User = Depends(require_organizer),
    db: AsyncSession = Depends(get_db),
):
    """
    Rule 20: Organizer can modify only their own tournament.
    """
    res = await db.execute(select(Tournament).where(Tournament.id == id))
    tourn = res.scalars().first()
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if organizer.role != "SUPER_ADMIN" and tourn.organizer_id != organizer.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You do not own this tournament (Rule 20)",
        )

    if payload.title is not None:
        tourn.title = payload.title
    if payload.prize_pool_inr is not None:
        tourn.prize_pool_inr = payload.prize_pool_inr
    if payload.entry_fee_inr is not None:
        tourn.entry_fee_inr = payload.entry_fee_inr
    if payload.starts_at is not None:
        tourn.starts_at = payload.starts_at
    if payload.roster_lock_time is not None:
        tourn.roster_lock_time = payload.roster_lock_time
    if payload.server_region is not None:
        tourn.server_region = payload.server_region
    if payload.max_teams is not None:
        tourn.max_teams = payload.max_teams
    if payload.rank_requirement is not None:
        tourn.rank_requirement = payload.rank_requirement
    if payload.format is not None:
        tourn.format = payload.format
    if payload.banner_url is not None:
        tourn.banner_url = payload.banner_url
    if payload.description is not None:
        tourn.description = payload.description
    if payload.rules is not None:
        tourn.rules_json = json.dumps(payload.rules)
    if payload.schedule is not None:
        tourn.schedule_json = json.dumps(payload.schedule)

    await db.commit()
    await db.refresh(tourn)

    g_res = await db.execute(select(Game).where(Game.id == tourn.game_id))
    game = g_res.scalars().first()

    reg_count = (
        await db.execute(
            select(func.count(TournamentRegistration.id)).where(
                TournamentRegistration.tournament_id == tourn.id
            )
        )
    ).scalar() or 0

    return TournamentResponse(
        id=tourn.id,
        slug=tourn.slug,
        organizer_id=tourn.organizer_id,
        title=tourn.title,
        game_slug=game.slug if game else "game",
        game_name=game.name if game else "Game",
        prize_pool=f"₹{tourn.prize_pool_inr:,}",
        prize_pool_inr=tourn.prize_pool_inr,
        entry_fee=f"₹{tourn.entry_fee_inr} / Team" if tourn.entry_fee_inr > 0 else "₹0 / Free",
        entry_fee_inr=tourn.entry_fee_inr,
        status=tourn.status,
        starts_at=tourn.starts_at,
        roster_lock_time=tourn.roster_lock_time,
        server_region=tourn.server_region,
        registered_teams=reg_count,
        max_teams=tourn.max_teams,
        rank_requirement=tourn.rank_requirement,
        format=tourn.format,
        banner_url=tourn.banner_url,
        badge_color=tourn.badge_color,
        description=tourn.description,
        rules=json.loads(tourn.rules_json) if tourn.rules_json else [],
        schedule=json.loads(tourn.schedule_json) if tourn.schedule_json else [],
        prizes=json.loads(tourn.prizes_json) if tourn.prizes_json else [],
        bracket_rounds=json.loads(tourn.bracket_json) if tourn.bracket_json else [],
        registered_rosters=[],
    )

@router.post("/tournaments/{id}/publish")
async def publish_tournament(
    id: str,
    organizer: User = Depends(require_organizer),
    db: AsyncSession = Depends(get_db),
):
    """
    Publish tournament (opens registration): DRAFT/REGISTRATION_CLOSED -> REGISTRATION_OPEN.
    """
    res = await db.execute(select(Tournament).where(Tournament.id == id))
    tourn = res.scalars().first()
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if organizer.role != "SUPER_ADMIN" and tourn.organizer_id != organizer.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You do not own this tournament (Rule 20)",
        )

    validate_tournament_transition(tourn.status, "REGISTRATION_OPEN")
    prev_status = tourn.status
    tourn.status = "REGISTRATION_OPEN"

    await record_audit_log(
        db=db,
        operator=organizer.username,
        role=organizer.role,
        action="TOURNAMENT_STATE_CHANGE",
        target=f"Tournament {tourn.title} ({tourn.id})",
        details=f"Status transitioned: {prev_status} -> REGISTRATION_OPEN",
    )
    await db.commit()
    return {"status": "success", "message": "Tournament published! Registration is now OPEN."}

@router.post("/tournaments/{id}/close-registration")
async def close_tournament_registration(
    id: str,
    organizer: User = Depends(require_organizer),
    db: AsyncSession = Depends(get_db),
):
    """
    Close registrations: REGISTRATION_OPEN -> REGISTRATION_CLOSED.
    """
    res = await db.execute(select(Tournament).where(Tournament.id == id))
    tourn = res.scalars().first()
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if organizer.role != "SUPER_ADMIN" and tourn.organizer_id != organizer.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You do not own this tournament (Rule 20)",
        )

    validate_tournament_transition(tourn.status, "REGISTRATION_CLOSED")
    prev_status = tourn.status
    tourn.status = "REGISTRATION_CLOSED"

    await record_audit_log(
        db=db,
        operator=organizer.username,
        role=organizer.role,
        action="TOURNAMENT_STATE_CHANGE",
        target=f"Tournament {tourn.title} ({tourn.id})",
        details=f"Status transitioned: {prev_status} -> REGISTRATION_CLOSED",
    )
    await db.commit()
    return {"status": "success", "message": "Tournament registration is now CLOSED. Rosters locked."}

@router.post("/tournaments/{id}/cancel")
async def cancel_tournament(
    id: str,
    organizer: User = Depends(require_organizer),
    db: AsyncSession = Depends(get_db),
):
    """
    Cancel tournament: transitions to CANCELLED.
    """
    res = await db.execute(select(Tournament).where(Tournament.id == id))
    tourn = res.scalars().first()
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if organizer.role != "SUPER_ADMIN" and tourn.organizer_id != organizer.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You do not own this tournament (Rule 20)",
        )

    validate_tournament_transition(tourn.status, "CANCELLED")
    prev_status = tourn.status
    tourn.status = "CANCELLED"

    await record_audit_log(
        db=db,
        operator=organizer.username,
        role=organizer.role,
        action="TOURNAMENT_CANCELLED",
        target=f"Tournament {tourn.title} ({tourn.id})",
        details=f"Status transitioned: {prev_status} -> CANCELLED",
    )
    await db.commit()
    return {"status": "success", "message": f"Tournament '{tourn.title}' has been cancelled."}

@router.get("/my-tournaments", response_model=List[TournamentResponse])
async def get_my_organized_tournaments(
    organizer: User = Depends(require_organizer),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns tournaments owned by the authenticated organizer.
    """
    query = select(Tournament)
    if organizer.role != "SUPER_ADMIN":
        query = query.where(Tournament.organizer_id == organizer.id)

    result = await db.execute(query)
    tournaments = result.scalars().all()

    response = []
    for t in tournaments:
        g_res = await db.execute(select(Game).where(Game.id == t.game_id))
        game_obj = g_res.scalars().first()

        reg_count = (
            await db.execute(
                select(func.count(TournamentRegistration.id)).where(
                    TournamentRegistration.tournament_id == t.id
                )
            )
        ).scalar() or 0

        response.append(
            TournamentResponse(
                id=t.id,
                slug=t.slug,
                organizer_id=t.organizer_id,
                title=t.title,
                game_slug=game_obj.slug if game_obj else "game",
                game_name=game_obj.name if game_obj else "Game",
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
                rules=json.loads(t.rules_json) if t.rules_json else [],
                schedule=json.loads(t.schedule_json) if t.schedule_json else [],
                prizes=json.loads(t.prizes_json) if t.prizes_json else [],
                bracket_rounds=json.loads(t.bracket_json) if t.bracket_json else [],
                registered_rosters=[],
            )
        )
    return response

@router.post("/tournaments/{id}/generate-bracket")
async def generate_single_elimination_bracket(
    id: str,
    organizer: User = Depends(require_organizer),
    db: AsyncSession = Depends(get_db),
):
    """
    Generates a single-elimination bracket from registered teams via Bracket Engine (Section 40).
    """
    from app.services.bracket_engine import build_single_elimination_bracket

    res = await db.execute(select(Tournament).where(Tournament.id == id))
    tourn = res.scalars().first()
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if organizer.role != "SUPER_ADMIN" and tourn.organizer_id != organizer.id:
        raise HTTPException(status_code=403, detail="Forbidden: You do not own this tournament (Rule 20)")

    validate_tournament_transition(tourn.status, "LIVE")
    prev_status = tourn.status

    bracket_info = await build_single_elimination_bracket(tournament=tourn, db=db)

    await record_audit_log(
        db=db,
        operator=organizer.username,
        role=organizer.role,
        action="GENERATE_BRACKET",
        target=f"Tournament {tourn.title} ({tourn.id})",
        details=f"Generated {bracket_info['total_rounds']}-round bracket. Status: {prev_status} -> LIVE",
    )
    await db.commit()

    return {
        "status": "success",
        "message": f"Single Elimination Bracket generated with {bracket_info['total_rounds']} rounds! Tournament is LIVE.",
        "rounds": bracket_info["bracket"],
    }
