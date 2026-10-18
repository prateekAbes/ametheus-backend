import json
import re
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.api.deps import get_db, require_organizer
from app.models.user import User
from app.models.tournament import Tournament
from app.services.audit import record_audit_log
from app.schemas import TournamentResponse
from app.core.ws import ws_manager

router = APIRouter()

VALID_TRANSITIONS = {
    "DRAFT": ["COMING_SOON", "REGISTRATION_OPEN", "LIVE", "CANCELLED"],
    "COMING_SOON": ["DRAFT", "REGISTRATION_OPEN", "LIVE", "CANCELLED"],
    "REGISTRATION_OPEN": ["REGISTRATION_CLOSED", "LIVE", "CANCELLED"],
    "REGISTRATION_CLOSED": ["UPCOMING", "LIVE", "REGISTRATION_OPEN", "CANCELLED"],
    "UPCOMING": ["LIVE", "CANCELLED", "REGISTRATION_OPEN"],
    "LIVE": ["LIVE", "COMPLETED", "CANCELLED", "REGISTRATION_CLOSED"],
    "COMPLETED": ["LIVE"],
    "CANCELLED": ["DRAFT", "REGISTRATION_OPEN"],
}

async def find_organizer_tournament(db: AsyncIOMotorDatabase, id: str):
    if not id:
        return None
    from bson import ObjectId
    clauses = [
        {"_id": id},
        {"id": id},
        {"slug": id},
        {"slug": id.lower()},
    ]
    if ObjectId.is_valid(id):
        try:
            clauses.append({"_id": ObjectId(id)})
        except Exception:
            pass
    return await db.tournaments.find_one({"$or": clauses})

def validate_tournament_transition(current_status: str, next_status: str, is_admin: bool = False):
    curr = "LIVE" if current_status == "ONGOING" else current_status
    nxt = "LIVE" if next_status == "ONGOING" else next_status
    if curr == nxt or is_admin:
        return
    allowed = VALID_TRANSITIONS.get(curr, ["REGISTRATION_OPEN", "REGISTRATION_CLOSED", "LIVE", "COMPLETED", "CANCELLED"])
    if nxt not in allowed:
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
    min_team_size: Optional[int] = 1
    max_team_size: Optional[int] = 5
    status: Optional[str] = "REGISTRATION_OPEN"
    expected_start_date: Optional[str] = None
    registration_open_date: Optional[str] = None
    announcement_text: Optional[str] = None
    rank_requirement: str = "Open to All Ranks"
    format: str = "Knockout"
    tournament_type: Optional[str] = "Knockout"
    match_format: Optional[str] = "Best of 1"
    registration_type: Optional[str] = None
    is_solo: Optional[bool] = None
    match_structure: Optional[str] = None
    players_per_team: Optional[int] = None
    room_capacity: Optional[int] = None
    banner_url: Optional[str] = None
    hero_banner_url: Optional[str] = None
    banner_tablet_url: Optional[str] = None
    banner_mobile_url: Optional[str] = None
    logo_url: Optional[str] = None
    media_config: Optional[Dict[str, Any]] = None
    description: str
    rules: List[str] = []
    schedule: List[Dict[str, Any]] = []
    prizes: Optional[List[Dict[str, Any]]] = None
    whatsapp_group_url: Optional[str] = None
    discord_url: Optional[str] = None
    community_link: Optional[str] = None

class TournamentUpdatePayload(BaseModel):
    title: Optional[str] = None
    game_id: Optional[str] = None
    prize_pool_inr: Optional[int] = None
    entry_fee_inr: Optional[int] = None
    starts_at: Optional[str] = None
    roster_lock_time: Optional[str] = None
    server_region: Optional[str] = None
    max_teams: Optional[int] = None
    min_team_size: Optional[int] = None
    max_team_size: Optional[int] = None
    expected_start_date: Optional[str] = None
    registration_open_date: Optional[str] = None
    announcement_text: Optional[str] = None
    rank_requirement: Optional[str] = None
    format: Optional[str] = None
    tournament_type: Optional[str] = None
    match_format: Optional[str] = None
    registration_type: Optional[str] = None
    is_solo: Optional[bool] = None
    match_structure: Optional[str] = None
    players_per_team: Optional[int] = None
    room_capacity: Optional[int] = None
    banner_url: Optional[str] = None
    hero_banner_url: Optional[str] = None
    banner_tablet_url: Optional[str] = None
    banner_mobile_url: Optional[str] = None
    logo_url: Optional[str] = None
    media_config: Optional[Dict[str, Any]] = None
    description: Optional[str] = None
    rules: Optional[List[str]] = None
    schedule: Optional[List[Dict[str, Any]]] = None
    prizes: Optional[List[Dict[str, Any]]] = None
    status: Optional[str] = None
    registration_status: Optional[str] = None
    whatsapp_group_url: Optional[str] = None
    discord_url: Optional[str] = None
    community_link: Optional[str] = None

def parse_iso_datetime(dt_str: str) -> datetime:
    s = dt_str.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

def slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s

def normalize_competition_format(fmt: Optional[str]) -> str:
    if not fmt:
        return "Knockout"
    s = fmt.lower().strip()
    if "battle" in s or "royale" in s:
        return "Battle Royale"
    elif "robin" in s:
        return "Round Robin"
    return "Knockout"

def normalize_series_format(fmt: Optional[str]) -> str:
    if not fmt:
        return "BO1"
    s = fmt.upper().strip()
    if "3" in s:
        return "BO3"
    return "BO1"

def serialize_tournament_response(t: dict, game_obj: Optional[dict], reg_count: int) -> TournamentResponse:
    tid = str(t.get("_id", t.get("id")))

    def parse_list(val):
        if isinstance(val, list):
            return val
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return []
        return []

    rules = parse_list(t.get("rules", t.get("rules_json", [])))
    schedule = parse_list(t.get("schedule", t.get("schedule_json", [])))
    prizes = parse_list(t.get("prizes", t.get("prizes_json", [])))
    bracket = parse_list(t.get("bracket_rounds", t.get("bracket_json", [])))

    prize_inr = t.get("prize_pool_inr", 0)
    fee_inr = t.get("entry_fee_inr", 0)

    comp_fmt = normalize_competition_format(t.get("format") or t.get("tournament_type"))
    series_fmt = normalize_series_format(t.get("series_format") or t.get("match_format"))

    min_s = t.get("min_team_size", 1)
    max_s = t.get("max_team_size", 5)
    raw_fmt_str = (str(t.get("format", "")) + " " + str(t.get("match_format", "")) + " " + str(t.get("title", ""))).lower()
    is_solo = bool(
        t.get("is_solo")
        or t.get("registration_type") == "SOLO"
        or (min_s == 1 and max_s == 1)
        or "solo" in raw_fmt_str
        or "1v1" in raw_fmt_str
    )
    reg_type = "SOLO" if is_solo else (t.get("registration_type") or "TEAM")
    comp_structure = "BATTLE_ROYALE" if "battle" in comp_fmt.lower() else ("ROUND_ROBIN" if "robin" in comp_fmt.lower() else "KNOCKOUT")
    comp_unit = "Players" if is_solo else ("Squads" if comp_structure == "BATTLE_ROYALE" or min_s == 4 else "Teams")
    unit_label = "Player" if is_solo else ("Squad" if comp_structure == "BATTLE_ROYALE" or min_s == 4 else "Team")

    return TournamentResponse(
        id=tid,
        slug=t.get("slug", tid),
        organizer_id=t.get("organizer_id"),
        title=t.get("title", "Tournament"),
        game_slug=game_obj["slug"] if game_obj else "game",
        game_name=game_obj["name"] if game_obj else "Game",
        prize_pool=f"₹{prize_inr:,}",
        prize_pool_inr=prize_inr,
        entry_fee=f"₹{fee_inr} / {unit_label}" if fee_inr > 0 else "₹0 / Free",
        entry_fee_inr=fee_inr,
        status=t.get("status", "REGISTRATION_OPEN"),
        expected_start_date=t.get("expected_start_date"),
        registration_open_date=t.get("registration_open_date"),
        announcement_text=t.get("announcement_text"),
        starts_at=t.get("starts_at", "TBD"),
        roster_lock_time=t.get("roster_lock_time", "TBD"),
        server_region=t.get("server_region", "AWS Mumbai (ap-south-1)"),
        registered_teams=reg_count,
        confirmed_teams=reg_count,
        max_teams=t.get("max_teams", 16),
        min_team_size=min_s,
        max_team_size=max_s,
        rank_requirement=t.get("rank_requirement", "Open to All Ranks"),
        format=comp_fmt,
        tournament_type=comp_fmt,
        match_format=comp_fmt,
        series_format=series_fmt,
        registration_type=reg_type,
        is_solo=is_solo,
        match_structure=comp_structure,
        competitor_unit=comp_unit,
        players_per_team=t.get("players_per_team") or (1 if is_solo else min_s),
        room_capacity=t.get("room_capacity"),
        banner_url=t.get("banner_url", ""),
        hero_banner_url=t.get("hero_banner_url"),
        banner_tablet_url=t.get("banner_tablet_url"),
        banner_mobile_url=t.get("banner_mobile_url"),
        logo_url=t.get("logo_url"),
        badge_color=t.get("badge_color", "#E56A10"),
        description=t.get("description", ""),
        rules=rules,
        schedule=schedule,
        prizes=prizes,
        bracket_rounds=bracket,
        registered_rosters=[],
        whatsapp_group_url=t.get("whatsapp_group_url"),
        discord_url=t.get("discord_url"),
        community_link=t.get("community_link"),
        media_config=t.get("media_config", {}),
    )


@router.post("/tournaments", response_model=TournamentResponse, status_code=status.HTTP_201_CREATED)
async def create_tournament(
    payload: TournamentCreatePayload,
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Organizer creates a new tournament (Rule 9). Default state is REGISTRATION_OPEN or payload.status.
    """
    if not payload.title or not payload.title.strip():
        raise HTTPException(status_code=422, detail="Tournament title cannot be empty")

    min_size = payload.min_team_size or 1
    max_size = payload.max_team_size or 5
    if min_size < 1:
        raise HTTPException(status_code=422, detail="Minimum team size must be at least 1")
    if max_size < min_size:
        raise HTTPException(status_code=422, detail="Maximum team size cannot be less than minimum team size")
    if payload.max_teams < 2:
        raise HTTPException(status_code=422, detail="Maximum teams must be at least 2")
    if payload.prize_pool_inr < 0:
        raise HTTPException(status_code=422, detail="Prize pool cannot be negative")
    if payload.entry_fee_inr < 0:
        raise HTTPException(status_code=422, detail="Entry fee cannot be negative")

    try:
        starts_dt = parse_iso_datetime(payload.starts_at)
        lock_dt = parse_iso_datetime(payload.roster_lock_time)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid ISO 8601 date format for starts_at or roster_lock_time: {str(e)}")

    if lock_dt > starts_dt:
        raise HTTPException(status_code=422, detail="Roster lock time must be at or before tournament start time")

    game = await db.games.find_one({"$or": [{"_id": payload.game_id}, {"id": payload.game_id}, {"slug": payload.game_id}]})
    if not game:
        raise HTTPException(status_code=404, detail="Selected game discipline not found")

    gid = str(game.get("_id", game.get("id")))
    base_slug = payload.slug or slugify(payload.title)
    existing = await db.tournaments.find_one({"slug": base_slug})
    slug = base_slug if not existing else f"{base_slug}-{organizer.id[:6]}"

    banner = payload.banner_url or None
    banner_tablet = payload.banner_tablet_url or None
    banner_mobile = payload.banner_mobile_url or None
    logo = payload.logo_url or None

    prizes = payload.prizes if payload.prizes else [{"place": "1st", "amount": f"₹{payload.prize_pool_inr}"}]

    init_status = payload.status or "REGISTRATION_OPEN"

    is_solo = bool(
        payload.is_solo
        or payload.registration_type == "SOLO"
        or (min_size == 1 and max_size == 1)
        or "solo" in str(payload.format).lower()
        or "1v1" in str(payload.format).lower()
    )
    reg_type = "SOLO" if is_solo else (payload.registration_type or "TEAM")
    if is_solo:
        min_size = 1
        max_size = 1

    comp_structure = "BATTLE_ROYALE" if "battle" in str(payload.format).lower() else ("ROUND_ROBIN" if "robin" in str(payload.format).lower() else "KNOCKOUT")

    tourn = Tournament(
        slug=slug,
        game_id=gid,
        organizer_id=organizer.id,
        title=payload.title.strip(),
        prize_pool_inr=payload.prize_pool_inr,
        entry_fee_inr=payload.entry_fee_inr,
        status=init_status,
        expected_start_date=payload.expected_start_date,
        registration_open_date=payload.registration_open_date,
        registration_deadline=lock_dt,
        announcement_text=payload.announcement_text,
        starts_at=payload.starts_at,
        roster_lock_time=payload.roster_lock_time,
        server_region=payload.server_region,
        max_teams=payload.max_teams,
        min_team_size=min_size,
        max_team_size=max_size,
        registration_type=reg_type,
        is_solo=is_solo,
        match_structure=comp_structure,
        players_per_team=payload.players_per_team or (1 if is_solo else min_size),
        room_capacity=payload.room_capacity,
        rank_requirement=payload.rank_requirement,
        format=normalize_competition_format(payload.format),
        tournament_type=normalize_competition_format(payload.tournament_type or payload.format),
        match_format=normalize_competition_format(payload.format),
        series_format=normalize_series_format(payload.match_format),
        banner_url=banner or "",
        hero_banner_url=payload.hero_banner_url,
        banner_tablet_url=banner_tablet,
        banner_mobile_url=banner_mobile,
        logo_url=logo,

        badge_color=game.get("accent_color", "#E56A10"),
        description=payload.description,
        rules=payload.rules,
        schedule=payload.schedule,
        prizes=prizes,
        bracket_rounds=[],
        whatsapp_group_url=payload.whatsapp_group_url,
        discord_url=payload.discord_url,
        community_link=payload.community_link,
    )
    if payload.media_config is not None:
        tourn.media_config = payload.media_config
    tourn_doc = tourn.to_mongo()
    tourn_doc["id"] = str(tourn_doc["_id"])
    await db.tournaments.insert_one(tourn_doc)

    return serialize_tournament_response(tourn_doc, game, 0)

@router.put("/tournaments/{id}", response_model=TournamentResponse)
async def update_tournament(
    id: str,
    payload: TournamentUpdatePayload,
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Rule 20: Organizer can modify only their own tournament.
    """
    tourn = await find_organizer_tournament(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if organizer.role != "SUPER_ADMIN" and tourn.get("organizer_id") != organizer.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You do not own this tournament (Rule 20)",
        )

    update_fields = {}
    if payload.title is not None:
        if not payload.title.strip():
            raise HTTPException(status_code=422, detail="Tournament title cannot be empty")
        update_fields["title"] = payload.title.strip()
    if payload.prize_pool_inr is not None:
        if payload.prize_pool_inr < 0:
            raise HTTPException(status_code=422, detail="Prize pool cannot be negative")
        update_fields["prize_pool_inr"] = payload.prize_pool_inr
    if payload.entry_fee_inr is not None:
        if payload.entry_fee_inr < 0:
            raise HTTPException(status_code=422, detail="Entry fee cannot be negative")
        update_fields["entry_fee_inr"] = payload.entry_fee_inr
    if payload.starts_at is not None:
        update_fields["starts_at"] = payload.starts_at
    if payload.roster_lock_time is not None:
        update_fields["roster_lock_time"] = payload.roster_lock_time
    if payload.starts_at is not None or payload.roster_lock_time is not None:
        new_starts = payload.starts_at or tourn.get("starts_at")
        new_lock = payload.roster_lock_time or tourn.get("roster_lock_time")
        if new_starts and new_lock and new_starts != "TBD" and new_lock != "TBD":
            try:
                starts_dt = parse_iso_datetime(new_starts)
                lock_dt = parse_iso_datetime(new_lock)
                if lock_dt > starts_dt:
                    raise HTTPException(status_code=422, detail="Roster lock time must be at or before tournament start time")
            except HTTPException:
                raise
            except Exception:
                pass
    if payload.whatsapp_group_url is not None:
        update_fields["whatsapp_group_url"] = payload.whatsapp_group_url
    if payload.community_link is not None:
        update_fields["community_link"] = payload.community_link
    if payload.server_region is not None:
        update_fields["server_region"] = payload.server_region
    if payload.max_teams is not None:
        if payload.max_teams < 2:
            raise HTTPException(status_code=422, detail="Maximum teams must be at least 2")
        update_fields["max_teams"] = payload.max_teams
    if payload.rank_requirement is not None:
        update_fields["rank_requirement"] = payload.rank_requirement
    if payload.format is not None:
        comp_fmt = normalize_competition_format(payload.format)
        update_fields["format"] = comp_fmt
        update_fields["tournament_type"] = comp_fmt
        update_fields["match_format"] = comp_fmt
    elif payload.tournament_type is not None:
        comp_fmt = normalize_competition_format(payload.tournament_type)
        update_fields["format"] = comp_fmt
        update_fields["tournament_type"] = comp_fmt
        update_fields["match_format"] = comp_fmt
    if payload.match_format is not None:
        update_fields["series_format"] = normalize_series_format(payload.match_format)
    if payload.whatsapp_group_url is not None:
        update_fields["whatsapp_group_url"] = payload.whatsapp_group_url.strip() or None
    if payload.discord_url is not None:
        update_fields["discord_url"] = payload.discord_url.strip() or None
    if payload.community_link is not None:
        update_fields["community_link"] = payload.community_link.strip() or None
    if payload.banner_url is not None:
        update_fields["banner_url"] = payload.banner_url
    if payload.hero_banner_url is not None:
        update_fields["hero_banner_url"] = payload.hero_banner_url
    if payload.banner_tablet_url is not None:
        update_fields["banner_tablet_url"] = payload.banner_tablet_url
    if payload.banner_mobile_url is not None:
        update_fields["banner_mobile_url"] = payload.banner_mobile_url
    if payload.logo_url is not None:
        update_fields["logo_url"] = payload.logo_url
    if payload.media_config is not None:
        update_fields["media_config"] = payload.media_config
        if isinstance(payload.media_config, dict):
            mc = payload.media_config
            if "card" in mc and isinstance(mc["card"], dict):
                update_fields["banner_url"] = mc["card"].get("asset_url") or ""
            if "hero" in mc and isinstance(mc["hero"], dict):
                update_fields["hero_banner_url"] = mc["hero"].get("asset_url") or ""
            if "logo" in mc and isinstance(mc["logo"], dict):
                update_fields["logo_url"] = mc["logo"].get("asset_url") or ""
    if payload.description is not None:
        update_fields["description"] = payload.description
    if payload.rules is not None:
        update_fields["rules"] = payload.rules
    if payload.game_id is not None:
        update_fields["game_id"] = payload.game_id
    if payload.prizes is not None:
        update_fields["prizes"] = payload.prizes
    if payload.min_team_size is not None:
        if payload.min_team_size < 1:
            raise HTTPException(status_code=422, detail="Minimum team size must be at least 1")
        update_fields["min_team_size"] = payload.min_team_size
    if payload.max_team_size is not None:
        eff_min = payload.min_team_size if payload.min_team_size is not None else tourn.get("min_team_size", 1)
        if payload.max_team_size < eff_min:
            raise HTTPException(status_code=422, detail="Maximum team size cannot be less than minimum team size")
        update_fields["max_team_size"] = payload.max_team_size
    if payload.registration_type is not None:
        update_fields["registration_type"] = payload.registration_type.upper()
        update_fields["is_solo"] = (payload.registration_type.upper() == "SOLO")
    if payload.is_solo is not None:
        update_fields["is_solo"] = payload.is_solo
        if payload.is_solo:
            update_fields["registration_type"] = "SOLO"
            update_fields["min_team_size"] = 1
            update_fields["max_team_size"] = 1
    if payload.match_structure is not None:
        update_fields["match_structure"] = payload.match_structure.upper()
    if payload.players_per_team is not None:
        update_fields["players_per_team"] = payload.players_per_team
    if payload.room_capacity is not None:
        update_fields["room_capacity"] = payload.room_capacity

    if payload.registration_status is not None:
        update_fields["registration_status"] = payload.registration_status.strip().upper()
    if payload.status is not None:
        status_norm = "LIVE" if payload.status == "ONGOING" else payload.status
        validate_tournament_transition(tourn.get("status", "DRAFT"), status_norm, is_admin=(organizer.role == "SUPER_ADMIN"))
        update_fields["status"] = status_norm
        if status_norm == "REGISTRATION_CLOSED":
            update_fields["registration_status"] = "CLOSED"
        elif status_norm in ("REGISTRATION_OPEN", "UPCOMING"):
            update_fields["registration_status"] = "OPEN"
    if payload.expected_start_date is not None:
        update_fields["expected_start_date"] = payload.expected_start_date
    if payload.registration_open_date is not None:
        update_fields["registration_open_date"] = payload.registration_open_date
    if payload.announcement_text is not None:
        update_fields["announcement_text"] = payload.announcement_text

    if update_fields:
        await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": update_fields})

    updated_tourn = await db.tournaments.find_one({"_id": tourn["_id"]})
    gid = updated_tourn.get("game_id")
    game = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]})
    tid = str(updated_tourn.get("_id", updated_tourn.get("id")))
    tslug = updated_tourn.get("slug", "")
    reg_count = await db.tournament_registrations.count_documents({
        "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
        "payment_status": {"$in": ["PAID", "FREE"]},
    })

    # If tournament transitioned to LIVE, update existing scheduled matches to LIVE
    if updated_tourn.get("status") in ["LIVE", "ONGOING"]:
        await db.matches.update_many(
            {
                "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
                "team_a_id": {"$ne": None},
                "team_b_id": {"$ne": None},
                "status": {"$in": ["SCHEDULED", "CHECK_IN_OPEN"]},
            },
            {"$set": {"status": "LIVE"}}
        )

    return serialize_tournament_response(updated_tourn, game, reg_count)

@router.post("/tournaments/{id}/publish", response_model=TournamentResponse)
async def publish_tournament(
    id: str,
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Organizer or Super Admin privilege: One-click publish tournament from COMING_SOON/DRAFT to REGISTRATION_OPEN.
    """
    tourn = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if organizer.role != "SUPER_ADMIN" and tourn.get("organizer_id") != organizer.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only publish your own tournaments",
        )

    validate_tournament_transition(tourn.get("status", "DRAFT"), "REGISTRATION_OPEN")

    await db.tournaments.update_one(
        {"_id": tourn["_id"]},
        {"$set": {"status": "REGISTRATION_OPEN"}}
    )

    await record_audit_log(
        db=db,
        operator=organizer.username,
        role=organizer.role,
        action="PUBLISH_TOURNAMENT",
        target=f"Tournament {tourn.get('title')} ({id})",
        details="Published tournament to REGISTRATION_OPEN",
    )

    updated = await db.tournaments.find_one({"_id": tourn["_id"]})
    gid = updated.get("game_id")
    game = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]})
    tid = str(updated.get("_id", updated.get("id")))
    reg_count = await db.tournament_registrations.count_documents({
        "$or": [{"tournament_id": tid}, {"tournament_id": updated.get("slug")}],
        "payment_status": {"$in": ["PAID", "FREE"]},
    })
    return serialize_tournament_response(updated, game, reg_count)

@router.delete("/tournaments/{id}")
async def delete_tournament(
    id: str,
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    tourn = await find_organizer_tournament(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if organizer.role != "SUPER_ADMIN" and tourn.get("organizer_id") != organizer.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You do not have permission to delete this tournament",
        )

    tid_str = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug")
    t_filter = {"$or": [{"tournament_id": id}, {"tournament_id": tid_str}, {"tournament_id": tslug}]}

    # 1. Cascade delete all dependent matches and live match broadcast card
    await db.matches.delete_many(t_filter)
    await db.live_matches.delete_many(t_filter)

    # 2. Delete all tournament-specific registrations
    await db.tournament_registrations.delete_many(t_filter)

    # 3. Delete tournament-specific match requests
    await db.organizer_match_requests.delete_many(t_filter)

    # 4. If this tournament was featured on the homepage, clear the feature
    if tourn.get("is_featured"):
        await db.tournaments.update_many({"is_featured": True}, {"$set": {"is_featured": False}})

    # 5. Financial audit safety: preserve payments and wallet accounting history,
    # while marking tournament as permanently deleted
    await db.game_payments.update_many(
        t_filter,
        {"$set": {"tournament_deleted": True}}
    )

    # 6. Permanently delete tournament document
    await db.tournaments.delete_one({"_id": tourn["_id"]})

    # 7. Broadcast deletion event
    try:
        await ws_manager.broadcast({
            "type": "TOURNAMENT_DELETED",
            "tournament_id": tid_str,
            "slug": tslug,
        })
    except Exception:
        pass

    await record_audit_log(
        db=db,
        operator=organizer.username,
        role=organizer.role,
        action="DELETE_TOURNAMENT",
        target=f"Tournament {tourn.get('title')} ({id})",
        details=f"Permanently deleted tournament '{tourn.get('title')}' and purged dependent matches/registrations by {organizer.username}",
    )
    return {
        "status": "success",
        "message": f"Tournament '{tourn.get('title')}' successfully deleted",
        "tournament_id": tid_str,
    }

@router.post("/tournaments/{id}/close-registration")
async def close_tournament_registration(
    id: str,
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    tourn = await find_organizer_tournament(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if organizer.role != "SUPER_ADMIN" and tourn.get("organizer_id") != organizer.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You do not own this tournament (Rule 20)",
        )

    validate_tournament_transition(tourn.get("status", "REGISTRATION_OPEN"), "REGISTRATION_CLOSED")
    prev_status = tourn.get("status")

    await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": {"status": "REGISTRATION_CLOSED"}})
    tid = str(tourn.get("_id", tourn.get("id")))

    await record_audit_log(
        db=db,
        operator=organizer.username,
        role=organizer.role,
        action="TOURNAMENT_STATE_CHANGE",
        target=f"Tournament {tourn.get('title')} ({tid})",
        details=f"Status transitioned: {prev_status} -> REGISTRATION_CLOSED",
    )
    return {"status": "success", "message": "Tournament registration is now CLOSED. Rosters locked."}

@router.post("/tournaments/{id}/reopen-registration")
async def reopen_tournament_registration(
    id: str,
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    tourn = await find_organizer_tournament(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if organizer.role != "SUPER_ADMIN" and tourn.get("organizer_id") != organizer.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You do not own this tournament (Rule 20)",
        )

    validate_tournament_transition(tourn.get("status", "REGISTRATION_CLOSED"), "REGISTRATION_OPEN")
    prev_status = tourn.get("status")

    await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": {"status": "REGISTRATION_OPEN"}})
    tid = str(tourn.get("_id", tourn.get("id")))

    await record_audit_log(
        db=db,
        operator=organizer.username,
        role=organizer.role,
        action="TOURNAMENT_STATE_CHANGE",
        target=f"Tournament {tourn.get('title')} ({tid})",
        details=f"Status transitioned: {prev_status} -> REGISTRATION_OPEN (Reopened)",
    )
    return {"status": "success", "message": "Tournament registration is now REOPENED. Contenders may register."}

@router.post("/tournaments/{id}/cancel")
async def cancel_tournament(
    id: str,
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    tourn = await find_organizer_tournament(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if organizer.role != "SUPER_ADMIN" and tourn.get("organizer_id") != organizer.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You do not own this tournament (Rule 20)",
        )

    validate_tournament_transition(tourn.get("status", "DRAFT"), "CANCELLED")
    prev_status = tourn.get("status")

    await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": {"status": "CANCELLED"}})
    tid = str(tourn.get("_id", tourn.get("id")))

    await record_audit_log(
        db=db,
        operator=organizer.username,
        role=organizer.role,
        action="TOURNAMENT_CANCELLED",
        target=f"Tournament {tourn.get('title')} ({tid})",
        details=f"Status transitioned: {prev_status} -> CANCELLED",
    )
    return {"status": "success", "message": f"Tournament '{tourn.get('title')}' has been cancelled."}

@router.post("/tournaments/{id}/start-live", response_model=TournamentResponse)
async def start_tournament_live(
    id: str,
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Organizer or Super Admin privilege: Transition tournament directly to LIVE status.
    Updates scheduled matches to LIVE.
    """
    tourn = await find_organizer_tournament(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if organizer.role != "SUPER_ADMIN" and tourn.get("organizer_id") != organizer.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You do not own this tournament (Rule 20)",
        )

    validate_tournament_transition(tourn.get("status", "REGISTRATION_CLOSED"), "LIVE", is_admin=(organizer.role == "SUPER_ADMIN"))
    prev_status = tourn.get("status")

    await db.tournaments.update_one(
        {"_id": tourn["_id"]},
        {"$set": {"status": "LIVE"}}
    )
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    # Transition matches to LIVE
    await db.matches.update_many(
        {
            "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
            "team_a_id": {"$ne": None},
            "team_b_id": {"$ne": None},
            "status": {"$in": ["SCHEDULED", "CHECK_IN_OPEN"]},
        },
        {"$set": {"status": "LIVE"}}
    )

    await record_audit_log(
        db=db,
        operator=organizer.username,
        role=organizer.role,
        action="TOURNAMENT_SET_LIVE",
        target=f"Tournament {tourn.get('title')} ({tid})",
        details=f"Status transitioned: {prev_status} -> LIVE",
    )

    try:
        from app.services.live_match_service import sync_live_match_for_tournament
        await sync_live_match_for_tournament(tid, db)
    except Exception:
        pass

    updated = await db.tournaments.find_one({"_id": tourn["_id"]})
    gid = updated.get("game_id")
    game = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]})
    reg_count = await db.tournament_registrations.count_documents({
        "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
        "payment_status": {"$in": ["PAID", "FREE"]},
    })
    return serialize_tournament_response(updated, game, reg_count)

@router.post("/tournaments/{id}/complete", response_model=TournamentResponse)
async def complete_tournament_organizer(
    id: str,
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Organizer or Super Admin privilege: Transition tournament to COMPLETED status.
    """
    tourn = await find_organizer_tournament(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if organizer.role != "SUPER_ADMIN" and tourn.get("organizer_id") != organizer.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You do not own this tournament (Rule 20)",
        )

    validate_tournament_transition(tourn.get("status", "LIVE"), "COMPLETED", is_admin=(organizer.role == "SUPER_ADMIN"))
    prev_status = tourn.get("status")

    await db.tournaments.update_one(
        {"_id": tourn["_id"]},
        {"$set": {"status": "COMPLETED"}}
    )
    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    await record_audit_log(
        db=db,
        operator=organizer.username,
        role=organizer.role,
        action="TOURNAMENT_COMPLETED",
        target=f"Tournament {tourn.get('title')} ({tid})",
        details=f"Status transitioned: {prev_status} -> COMPLETED",
    )

    try:
        from app.services.live_match_service import sync_live_match_for_tournament
        await sync_live_match_for_tournament(tid, db)
    except Exception:
        pass

    updated = await db.tournaments.find_one({"_id": tourn["_id"]})
    gid = updated.get("game_id")
    game = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]})
    reg_count = await db.tournament_registrations.count_documents({
        "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
        "payment_status": {"$in": ["PAID", "FREE"]},
    })
    return serialize_tournament_response(updated, game, reg_count)

@router.get("/my-tournaments", response_model=List[TournamentResponse])
async def get_my_organized_tournaments(
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = {} if organizer.role == "SUPER_ADMIN" else {"organizer_id": organizer.id}
    cursor = db.tournaments.find(query)
    tournaments = await cursor.to_list(length=100)

    response = []
    for t in tournaments:
        gid = t.get("game_id")
        game_obj = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]}) if gid else None
        tid = str(t.get("_id", t.get("id")))
        reg_count = await db.tournament_registrations.count_documents({
            "$or": [{"tournament_id": tid}, {"tournament_id": t.get("slug")}],
            "payment_status": {"$in": ["PAID", "FREE"]},
        })
        response.append(serialize_tournament_response(t, game_obj, reg_count))

    return response

@router.post("/tournaments/{id}/generate-bracket")
@router.post("/tournaments/{id}/generate-brackets")
async def generate_single_elimination_bracket(
    id: str,
    organizer: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from app.services.bracket_engine import generate_tournament_bracket

    tourn_doc = await find_organizer_tournament(db, id)
    if not tourn_doc:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if organizer.role != "SUPER_ADMIN" and tourn_doc.get("organizer_id") != organizer.id:
        raise HTTPException(status_code=403, detail="Forbidden: You do not own this tournament (Rule 20)")

    validate_tournament_transition(tourn_doc.get("status", "DRAFT"), "LIVE")
    prev_status = tourn_doc.get("status")

    tourn_obj = Tournament.from_mongo(tourn_doc)
    bracket_info = await generate_tournament_bracket(tournament=tourn_obj, db=db)

    tid = str(tourn_doc.get("_id", tourn_doc.get("id")))
    await record_audit_log(
        db=db,
        operator=organizer.username,
        role=organizer.role,
        action="GENERATE_BRACKET",
        target=f"Tournament {tourn_doc.get('title')} ({tid})",
        details=f"Generated {bracket_info['total_rounds']}-round bracket. Status: {prev_status} -> LIVE",
    )

    return {
        "status": "success",
        "message": f"Single Elimination Bracket generated with {bracket_info['total_rounds']} rounds! Tournament is LIVE.",
        "rounds": bracket_info["bracket"],
    }
