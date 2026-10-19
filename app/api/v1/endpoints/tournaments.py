import json
import re
import uuid
from typing import List, Optional
from datetime import datetime, timezone
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, get_current_user, get_current_user_optional, require_organizer
from app.models.user import User
from app.models.tournament import Tournament, TournamentRegistration
from app.models.payment import GamePayment
from app.schemas import (
    TournamentResponse,
    TournamentRegisterRequest,
    MatchResponse,
    TournamentRegisterUpiResponse,
    TournamentUpiDetailsResponse,
    SubmitTournamentUtrRequest,
    TournamentRegistrationStatusResponse,
    TournamentStageFormatsUpdate,
)
from app.services.wallet_service import debit_wallet_funds, credit_wallet_funds
from app.services.upi_service import get_next_active_upi_account, build_upi_payment_uri
from app.services.audit import record_audit_log
from app.services.notification import create_notification
from app.services.bracket_engine import get_tournament_history, get_team_journey

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

async def find_tournament_by_id_or_slug(db: AsyncIOMotorDatabase, identifier: str):
    if not identifier:
        return None
    or_clauses = [
        {"slug": identifier},
        {"id": identifier},
        {"slug": identifier.lower()},
        {"_id": identifier},
    ]
    if ObjectId.is_valid(identifier):
        try:
            or_clauses.append({"_id": ObjectId(identifier)})
        except Exception:
            pass
    try:
        slug_escaped = re.escape(identifier).replace(r"\-", "[-_ ]")
        or_clauses.append({"slug": {"$regex": f"^{slug_escaped}$", "$options": "i"}})
        # Also match title if the slug looks like a converted title
        title_pattern = re.escape(identifier.replace("-", " "))
        or_clauses.append({"title": {"$regex": f"^{title_pattern}$", "$options": "i"}})

        # Fallback for game-suffixed slugs e.g. free-fire-tournament -> matches free-fire
        clean_name = identifier.lower().replace("-tournament", "").replace("_tournament", "").strip("-_ ")
        if clean_name and clean_name != identifier.lower():
            or_clauses.append({"slug": clean_name})
            or_clauses.append({"game_slug": clean_name})
            or_clauses.append({"slug": {"$regex": f"^{re.escape(clean_name)}", "$options": "i"}})
            or_clauses.append({"title": {"$regex": re.escape(clean_name.replace("-", " ")), "$options": "i"}})
    except Exception:
        pass

    return await db.tournaments.find_one({"$or": or_clauses})


@router.get("", response_model=List[TournamentResponse])
async def list_tournaments(
    game: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    include_drafts: Optional[bool] = False,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    filter_query = {"is_dummy": {"$ne": True}}
    if status:
        stat_upper = status.upper()
        if stat_upper == "ALL":
            if include_drafts:
                filter_query["status"] = {"$ne": "DELETED"}
            else:
                filter_query["status"] = {"$nin": ["DELETED", "HIDDEN", "DRAFT"]}
        elif stat_upper == "DRAFT":
            filter_query["status"] = "DRAFT"
        elif stat_upper == "LIVE":
            filter_query["status"] = {"$in": ["LIVE", "ONGOING", "IN_PROGRESS"]}
        elif stat_upper in ["COMING_SOON", "UPCOMING"]:
            filter_query["status"] = {"$in": ["COMING_SOON", "UPCOMING"]}
        elif stat_upper in ["REGISTRATION_OPEN", "OPEN"]:
            filter_query["status"] = {"$in": ["REGISTRATION_OPEN", "OPEN"]}
        elif stat_upper in ["REGISTRATION_CLOSED", "CLOSED"]:
            filter_query["status"] = {"$in": ["REGISTRATION_CLOSED", "CLOSED"]}
        else:
            filter_query["status"] = status
    else:
        if include_drafts:
            filter_query["status"] = {"$nin": ["DELETED", "CANCELLED", "HIDDEN"]}
        else:
            filter_query["status"] = {"$nin": ["DELETED", "CANCELLED", "HIDDEN", "DRAFT"]}

    if search and search.strip():
        search_str = search.strip()
        search_terms = search_str.split()

        all_games = await db.games.find({}).to_list(length=100)

        term_clauses = []
        for term in search_terms:
            t_pat = re.escape(term)
            term_lower = term.lower()

            term_game_ids = []
            for g in all_games:
                g_name = (g.get("name") or "").lower()
                g_slug = (g.get("slug") or "").lower()
                g_short = (g.get("short_name") or "").lower()
                if term_lower in g_name or term_lower in g_slug or term_lower in g_short:
                    term_game_ids.append(str(g.get("_id", g.get("id"))))
                    if g.get("slug"):
                        term_game_ids.append(g["slug"])

            clauses = [
                {"title": {"$regex": t_pat, "$options": "i"}},
                {"description": {"$regex": t_pat, "$options": "i"}},
                {"format": {"$regex": t_pat, "$options": "i"}},
                {"slug": {"$regex": t_pat, "$options": "i"}},
                {"game_slug": {"$regex": t_pat, "$options": "i"}},
                {"game_name": {"$regex": t_pat, "$options": "i"}},
            ]
            if term_game_ids:
                clauses.append({"game_id": {"$in": term_game_ids}})

            term_clauses.append({"$or": clauses})

        if term_clauses:
            if "$and" in filter_query:
                filter_query["$and"].extend(term_clauses)
            else:
                filter_query["$and"] = term_clauses

    cursor = db.tournaments.find(filter_query)
    tournaments = await cursor.to_list(length=100)

    response = []
    for t in tournaments:
        tid = str(t.get("_id", t.get("id")))
        gid = t.get("game_id")
        game_obj = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]})
        
        if game and game_obj and game_obj.get("slug") != game:
            continue

        reg_count = await db.tournament_registrations.count_documents({
            "$or": [{"tournament_id": tid}, {"tournament_id": t.get("slug")}],
            "payment_status": {"$in": ["PAID", "FREE"]},
        })
        reserved_count = await db.tournament_registrations.count_documents({
            "$or": [{"tournament_id": tid}, {"tournament_id": t.get("slug")}],
            "payment_status": {"$in": ["PENDING", "PENDING_VERIFICATION"]},
        })
        max_teams_val = int(t.get("max_teams", 16))
        avail_slots = max(0, max_teams_val - reg_count - reserved_count)

        rules = safe_json_loads(t.get("rules", t.get("rules_json")), [])
        schedule = safe_json_loads(t.get("schedule", t.get("schedule_json")), [])
        prizes = safe_json_loads(t.get("prizes", t.get("prizes_json")), [])
        bracket = safe_json_loads(t.get("bracket_rounds", t.get("bracket_json")), [])

        prize_inr = t.get("prize_pool_inr") or 0
        fee_inr = t.get("entry_fee_inr") or 0

        # Check for live matches belonging to this tournament
        live_m = await db.matches.find({
            "$or": [{"tournament_id": tid}, {"tournament_id": t.get("slug")}],
            "status": "LIVE"
        }).to_list(length=20)
        has_live = len(live_m) > 0
        live_matches_list = [{
            "id": str(m.get("_id", m.get("id"))),
            "stage": m.get("stage", "Live Match"),
            "match_type": m.get("match_type", "QUARTER_FINAL"),
            "format": m.get("format", "BO1"),
            "series_format": m.get("series_format", "BO1"),
            "youtube_live_url": m.get("youtube_live_url") or m.get("live_link"),
            "live_link": m.get("youtube_live_url") or m.get("live_link"),
            "status": "LIVE",
        } for m in live_m]

        raw_stat = str(t.get("status", "REGISTRATION_OPEN")).upper()
        if raw_stat in ["LIVE", "ONGOING", "IN_PROGRESS"]:
            norm_status = "LIVE"
        elif raw_stat == "DRAFT":
            norm_status = "DRAFT"
        elif raw_stat in ["COMING_SOON", "UPCOMING"]:
            norm_status = "UPCOMING"
        elif raw_stat in ["REGISTRATION_CLOSED", "CLOSED"]:
            norm_status = "REGISTRATION_CLOSED"
        elif raw_stat in ["REGISTRATION_OPEN", "OPEN"]:
            norm_status = "REGISTRATION_OPEN"
        else:
            norm_status = raw_stat

        raw_min = t.get("min_team_size") or t.get("required_team_size")
        raw_max = t.get("max_team_size")
        raw_fmt = (str(t.get("format", "")) + " " + str(t.get("match_format", "")) + " " + str(t.get("title", ""))).lower()
        is_solo = bool(
            t.get("registration_type") == "SOLO"
            or t.get("is_solo")
            or (raw_min == 1 and raw_max == 1)
            or (game_obj and not game_obj.get("is_team_game", True))
            or (game_obj and game_obj.get("slug") == "chess")
            or "solo" in raw_fmt
            or "1v1" in raw_fmt
        )
        reg_type = "SOLO" if is_solo else (t.get("registration_type") or "TEAM")
        if is_solo:
            calc_min_size = 1
            calc_max_size = 1
        elif raw_min and int(raw_min) > 0:
            calc_min_size = int(raw_min)
            calc_max_size = int(raw_max) if raw_max and int(raw_max) >= calc_min_size else max(calc_min_size, 5)
        elif game_obj and game_obj.get("min_team_size"):
            calc_min_size = int(game_obj.get("min_team_size"))
            calc_max_size = int(game_obj.get("max_team_size", 5))
        else:
            calc_min_size = 1
            calc_max_size = 5

        norm_structure = "BATTLE_ROYALE" if "battle" in raw_fmt or "royale" in raw_fmt else ("ROUND_ROBIN" if "robin" in raw_fmt else "KNOCKOUT")
        raw_p_mode = t.get("participation_mode")
        if not raw_p_mode:
            if is_solo:
                part_mode = "SOLO"
            elif t.get("players_per_team") == 2 or (calc_min_size == 2 and calc_max_size == 2) or "duo" in raw_fmt:
                part_mode = "DUO"
            else:
                part_mode = "SQUAD"
        else:
            norm_p = str(raw_p_mode).upper().strip()
            part_mode = norm_p if norm_p in {"SOLO", "DUO", "SQUAD"} else "SQUAD"

        cap_label = "Max Players" if part_mode == "SOLO" else "Max Teams"
        comp_unit = "Players" if is_solo else ("Duos" if part_mode == "DUO" else ("Squads" if norm_structure == "BATTLE_ROYALE" or calc_min_size == 4 else "Teams"))
        unit_label = "Player" if is_solo else ("Team" if part_mode == "DUO" else ("Squad" if norm_structure == "BATTLE_ROYALE" or calc_min_size == 4 else "Team"))

        response.append(
            TournamentResponse(
                id=tid,
                slug=t.get("slug", tid),
                organizer_id=t.get("organizer_id"),
                title=t.get("title", "Championship Tournament"),
                game_slug=game_obj["slug"] if game_obj else "valorant",
                game_name=game_obj["name"] if game_obj else "Valorant",
                prize_pool=f"₹{prize_inr:,}",
                prize_pool_inr=prize_inr,
                entry_fee=f"₹{fee_inr} / {unit_label}" if fee_inr > 0 else "₹0 / Free",
                entry_fee_inr=fee_inr,
                status=norm_status,
                has_live_matches=has_live,
                live_matches_count=len(live_matches_list),
                live_matches=live_matches_list,
                starts_at=t.get("starts_at", "TBD"),
                roster_lock_time=None,
                server_region=t.get("server_region", "AWS Mumbai (ap-south-1)"),
                registered_teams=reg_count,
                confirmed_teams=reg_count,
                reserved_teams=reserved_count,
                available_slots=avail_slots,
                max_teams=max_teams_val,
                capacity_label=cap_label,
                min_team_size=calc_min_size,
                max_team_size=calc_max_size,
                registration_type=reg_type,
                participation_mode=part_mode,
                is_solo=is_solo,
                match_structure=norm_structure,
                competitor_unit=comp_unit,
                players_per_team=t.get("players_per_team") or (1 if is_solo else calc_min_size),
                room_capacity=t.get("room_capacity"),
                rank_requirement=t.get("rank_requirement", "Open to All Ranks"),
                format="Battle Royale" if norm_structure == "BATTLE_ROYALE" else ("Round Robin" if norm_structure == "ROUND_ROBIN" else "Knockout"),
                tournament_type="Battle Royale" if norm_structure == "BATTLE_ROYALE" else ("Round Robin" if norm_structure == "ROUND_ROBIN" else "Knockout"),
                match_format="Battle Royale" if norm_structure == "BATTLE_ROYALE" else ("Round Robin" if norm_structure == "ROUND_ROBIN" else "Knockout"),
                series_format="BO3" if "3" in str(t.get("series_format") or "") or "3" in str(t.get("match_format") or "") else "BO1",
                stage_formats=safe_json_loads(t.get("stage_formats"), {}),
                registration_deadline=t.get("registration_deadline").isoformat() if isinstance(t.get("registration_deadline"), datetime) else t.get("registration_deadline"),
                check_in_window_minutes=t.get("check_in_window_minutes", 15),
                banner_url=t.get("banner_url") or "",
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
                is_featured=bool(t.get("is_featured", False)),
                whatsapp_group_url=None,
                discord_url=None,
                community_link=None,
                media_config=t.get("media_config", {}),
                enable_mvp=bool(t.get("enable_mvp", False)),
                prize_distribution=t.get("prize_distribution") or [],
                champion=t.get("champion"),
                tournament_mvp=t.get("tournament_mvp"),
                final_results=t.get("final_results") or [],
                results_display_duration_days=int(t.get("results_display_duration_days", 30)),
                completed_at=str(t["completed_at"]) if t.get("completed_at") else None,
            )
        )
    return response

@router.get("/my-registrations")
async def get_my_tournament_registrations(
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns all tournaments the current authenticated user is registered for,
    either as an individual contender or through their team rosters.
    """
    valid_user_ids = [str(current_user.id)]
    if getattr(current_user, "clerk_user_id", None):
        valid_user_ids.append(str(current_user.clerk_user_id))

    # 1. Fetch user's teams
    my_teams_cursor = db.teams.find({
        "$or": [
            {"captain_id": {"$in": valid_user_ids}},
            {"members.user_id": {"$in": valid_user_ids}},
            {"substitutes.user_id": {"$in": valid_user_ids}},
        ],
        "is_dummy": {"$ne": True},
    })
    my_teams = await my_teams_cursor.to_list(length=100)
    my_team_ids = [str(t.get("_id", t.get("id"))) for t in my_teams]

    # 2. Query registrations matching player_id or team_id
    reg_or = [{"player_id": {"$in": valid_user_ids}}]
    if my_team_ids:
        reg_or.append({"team_id": {"$in": my_team_ids}})

    regs = await db.tournament_registrations.find({
        "$or": reg_or,
        "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
        "status": {"$nin": ["CANCELLED", "REFUNDED"]},
    }).to_list(length=100)

    results = []
    for r in regs:
        t_id = r.get("tournament_id")
        tourn = await db.tournaments.find_one({"$or": [{"_id": t_id}, {"id": t_id}, {"slug": t_id}]})
        if not tourn:
            continue

        gid = tourn.get("game_id")
        game = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]}) if gid else None

        reg_team = None
        if r.get("team_id"):
            reg_team = next((t for t in my_teams if str(t.get("_id", t.get("id"))) == r.get("team_id")), None)
            if not reg_team:
                reg_team = await db.teams.find_one({"$or": [{"_id": r["team_id"]}, {"id": r["team_id"]}]})

        results.append({
            "registration_id": str(r.get("_id", r.get("id"))),
            "tournament_id": str(tourn.get("_id", tourn.get("id"))),
            "tournament_title": tourn.get("title", "Tournament"),
            "tournament_slug": tourn.get("slug", ""),
            "tournament_status": tourn.get("status", "REGISTRATION_OPEN"),
            "starts_at": tourn.get("starts_at", "TBD"),
            "game_slug": game["slug"] if game else "game",
            "game_name": game["name"] if game else "Competitive Title",
            "banner_url": tourn.get("banner_url") or "",
            "banner_tablet_url": tourn.get("banner_tablet_url"),
            "banner_mobile_url": tourn.get("banner_mobile_url"),
            "logo_url": tourn.get("logo_url"),
            "prize_pool_inr": tourn.get("prize_pool_inr", 0),
            "entry_fee_inr": tourn.get("entry_fee_inr", 0),
            "payment_status": r.get("payment_status", "FREE"),
            "check_in_status": r.get("check_in_status", "PENDING"),
            "registered_at": r.get("created_at").isoformat() if r.get("created_at") else "",
            "team_id": r.get("team_id"),
            "team_name": reg_team.get("name") if reg_team else None,
            "team_tag": reg_team.get("tag") if reg_team else None,
        })

    return results
 
@router.get("/summary")
async def get_tournaments_summary(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Return live aggregate tournament figures for homepage and public promotional statistics."""
    active_tournaments = await db.tournaments.find(
        {
            "status": {"$nin": ["DELETED", "CANCELLED", "HIDDEN", "DRAFT"]},
            "is_dummy": {"$ne": True},
        },
        {"prize_pool_inr": 1, "status": 1, "title": 1}
    ).to_list(length=200)

    total_prize_pool_inr = sum(int(t.get("prize_pool_inr") or 0) for t in active_tournaments)
    active_count = len(active_tournaments)

    # Indian numbering format
    s = str(total_prize_pool_inr)
    if len(s) <= 3:
        formatted = f"₹{s}"
    else:
        last_three = s[-3:]
        remaining = s[:-3]
        groups = []
        while len(remaining) > 2:
            groups.insert(0, remaining[-2:])
            remaining = remaining[:-2]
        if remaining:
            groups.insert(0, remaining)
        formatted = f"₹{','.join(groups)},{last_three}"

    return {
        "total_prize_pool_inr": total_prize_pool_inr,
        "formatted_prize_pool": formatted,
        "active_tournaments_count": active_count,
    }

@router.get("/featured", response_model=Optional[TournamentResponse])
async def get_featured_tournament(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Return the admin-designated featured tournament for the homepage."""
    t = await db.tournaments.find_one({
        "is_featured": True,
        "status": {"$nin": ["DELETED", "CANCELLED", "HIDDEN"]},
        "is_dummy": {"$ne": True},
    })
    if not t:
        return None
    tid = str(t.get("_id", t.get("id")))
    return await get_tournament_by_slug(tid, db)

@router.post("/{id}/feature")
async def set_featured_tournament(
    id: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to select or clear the main homepage featured tournament."""
    # First, clear any previously featured tournaments
    await db.tournaments.update_many({}, {"$set": {"is_featured": False}})

    if id.lower() in ["none", "unset", "clear"]:
        return {"status": "success", "message": "Homepage featured tournament cleared", "featured_tournament_id": None}

    t = await find_tournament_by_id_or_slug(db, id)
    if not t:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(t.get("_id", t.get("id")))
    await db.tournaments.update_one(
        {"$or": [{"_id": t.get("_id")}, {"id": tid}, {"slug": t.get("slug")}]},
        {"$set": {"is_featured": True}}
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="SET_HOMEPAGE_FEATURED_TOURNAMENT",
        target=f"Tournament {tid} ({t.get('title')})",
    )

    return {
        "status": "success",
        "message": f"Tournament '{t.get('title')}' is now featured on homepage",
        "featured_tournament_id": tid,
    }

@router.get("/{slug}", response_model=TournamentResponse)
async def get_tournament_by_slug(slug: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    t = await find_tournament_by_id_or_slug(db, slug)
    if not t:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(t.get("_id", t.get("id")))
    gid = t.get("game_id")
    game_obj = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]})

    reg_count = await db.tournament_registrations.count_documents({
        "$or": [{"tournament_id": tid}, {"tournament_id": t.get("slug")}],
        "payment_status": {"$in": ["PAID", "FREE"]},
    })
    reserved_count = await db.tournament_registrations.count_documents({
        "$or": [{"tournament_id": tid}, {"tournament_id": t.get("slug")}],
        "payment_status": {"$in": ["PENDING", "PENDING_VERIFICATION"]},
    })
    max_teams_val = int(t.get("max_teams", 16))
    avail_slots = max(0, max_teams_val - reg_count - reserved_count)

    rules = safe_json_loads(t.get("rules", t.get("rules_json")), [])
    schedule = safe_json_loads(t.get("schedule", t.get("schedule_json")), [])
    prizes = safe_json_loads(t.get("prizes", t.get("prizes_json")), [])
    bracket = safe_json_loads(t.get("bracket_rounds", t.get("bracket_json")), [])

    prize_inr = t.get("prize_pool_inr") or 0
    fee_inr = t.get("entry_fee_inr") or 0

    # Auto-derive min/max team size and format architecture
    raw_min = t.get("min_team_size") or t.get("required_team_size")
    raw_max = t.get("max_team_size")
    raw_fmt = (str(t.get("format", "")) + " " + str(t.get("match_format", "")) + " " + str(t.get("title", ""))).lower()
    is_solo = bool(
        t.get("registration_type") == "SOLO"
        or t.get("is_solo")
        or (raw_min == 1 and raw_max == 1)
        or (game_obj and not game_obj.get("is_team_game", True))
        or (game_obj and game_obj.get("slug") == "chess")
        or "solo" in raw_fmt
        or "1v1" in raw_fmt
    )
    reg_type = "SOLO" if is_solo else (t.get("registration_type") or "TEAM")
    if is_solo:
        min_team_size = 1
        max_team_size = 1
    elif raw_min and int(raw_min) > 0:
        min_team_size = int(raw_min)
        max_team_size = int(raw_max) if raw_max and int(raw_max) >= min_team_size else max(min_team_size, 5)
    elif game_obj and game_obj.get("min_team_size"):
        min_team_size = int(game_obj.get("min_team_size"))
        max_team_size = int(game_obj.get("max_team_size", 5))
    else:
        min_team_size = 1
        max_team_size = 5

    norm_structure = "BATTLE_ROYALE" if "battle" in raw_fmt or "royale" in raw_fmt else ("ROUND_ROBIN" if "robin" in raw_fmt else "KNOCKOUT")
    raw_p_mode = t.get("participation_mode")
    if not raw_p_mode:
        if is_solo:
            part_mode = "SOLO"
        elif t.get("players_per_team") == 2 or (min_team_size == 2 and max_team_size == 2) or "duo" in raw_fmt:
            part_mode = "DUO"
        else:
            part_mode = "SQUAD"
    else:
        norm_p = str(raw_p_mode).upper().strip()
        part_mode = norm_p if norm_p in {"SOLO", "DUO", "SQUAD"} else "SQUAD"

    cap_label = "Max Players" if part_mode == "SOLO" else "Max Teams"
    comp_unit = "Players" if is_solo else ("Duos" if part_mode == "DUO" else ("Squads" if norm_structure == "BATTLE_ROYALE" or min_team_size == 4 else "Teams"))
    unit_label = "Player" if is_solo else ("Team" if part_mode == "DUO" else ("Squad" if norm_structure == "BATTLE_ROYALE" or min_team_size == 4 else "Team"))

    # 1. Bracket rounds: if empty, auto-assemble from matches
    if not bracket:
        match_cursor = db.matches.find({
            "$or": [{"tournament_id": tid}, {"tournament_id": t.get("slug")}]
        })
        matches_list = await match_cursor.to_list(length=100)
        if matches_list:
            rounds_map = {}
            for m in matches_list:
                r_num = m.get("round_number", 1)
                if r_num not in rounds_map:
                    rounds_map[r_num] = []

                if norm_structure == "BATTLE_ROYALE" or "battle" in str(m.get("format", "")).lower() or m.get("teams"):
                    # Multi-participant Battle Royale match
                    rounds_map[r_num].append({
                        "id": str(m.get("_id", m.get("id"))),
                        "round": r_num,
                        "matchNumber": m.get("match_number", len(rounds_map[r_num]) + 1),
                        "status": m.get("status", "SCHEDULED"),
                        "scheduledTime": m.get("scheduled_time", "Scheduled Lobby"),
                        "format": "Battle Royale",
                        "teams": m.get("teams", []),
                        "teams_count": len(m.get("teams", [])),
                    })
                else:
                    team_a_doc = await db.teams.find_one({"$or": [{"_id": m.get("team_a_id")}, {"id": m.get("team_a_id")}]}) if m.get("team_a_id") else None
                    if not team_a_doc and m.get("team_a_id"):
                        user_a = await db.users.find_one({"$or": [{"_id": m.get("team_a_id")}, {"id": m.get("team_a_id")}]})
                        if user_a:
                            team_a_doc = {"name": user_a.get("display_name") or user_a.get("username", "Contender"), "tag": "SOLO"}

                    team_b_doc = await db.teams.find_one({"$or": [{"_id": m.get("team_b_id")}, {"id": m.get("team_b_id")}]}) if m.get("team_b_id") else None
                    if not team_b_doc and m.get("team_b_id"):
                        user_b = await db.users.find_one({"$or": [{"_id": m.get("team_b_id")}, {"id": m.get("team_b_id")}]})
                        if user_b:
                            team_b_doc = {"name": user_b.get("display_name") or user_b.get("username", "Contender"), "tag": "SOLO"}

                    rounds_map[r_num].append({
                        "id": str(m.get("_id", m.get("id"))),
                        "round": r_num,
                        "matchNumber": m.get("match_number", len(rounds_map[r_num]) + 1),
                        "status": m.get("status", "SCHEDULED"),
                        "scheduledTime": m.get("scheduled_time", "Scheduled Bracket Match"),
                        "teamA": {
                            "name": team_a_doc.get("name") if team_a_doc else (m.get("team_a_name") or "TBD (Slot 1)"),
                            "tag": team_a_doc.get("tag") if team_a_doc else (m.get("team_a_tag") or "TBD"),
                            "score": m.get("score_a", "-"),
                            "isWinner": bool(m.get("winner_team_id") and m.get("winner_team_id") == m.get("team_a_id")),
                        },
                        "teamB": {
                            "name": team_b_doc.get("name") if team_b_doc else (m.get("team_b_name") or "TBD (Slot 2)"),
                            "tag": team_b_doc.get("tag") if team_b_doc else (m.get("team_b_tag") or "TBD"),
                            "score": m.get("score_b", "-"),
                            "isWinner": bool(m.get("winner_team_id") and m.get("winner_team_id") == m.get("team_b_id")),
                        },
                    })
            bracket = [
                {"name": f"Round {r}", "matches": matches}
                for r, matches in sorted(rounds_map.items())
            ]

    # 2. Registered rosters from database registrations
    registered_rosters = []
    reg_cursor = db.tournament_registrations.find({
        "$or": [{"tournament_id": tid}, {"tournament_id": t.get("slug")}],
        "payment_status": {"$in": ["PAID", "FREE"]},
    })
    registrations = await reg_cursor.to_list(length=100)
    for reg in registrations:
        if reg.get("team_id"):
            team_doc = await db.teams.find_one({"$or": [{"_id": reg["team_id"]}, {"id": reg["team_id"]}]})
            if team_doc:
                members_handles = [m.get("game_handle", m.get("username", "player")) if isinstance(m, dict) else getattr(m, "game_handle", "player") for m in team_doc.get("members", [])]
                cap_user = await db.users.find_one({"$or": [{"_id": team_doc.get("captain_id")}, {"id": team_doc.get("captain_id")}]})
                registered_rosters.append({
                    "tag": team_doc.get("tag", "TEAM"),
                    "name": team_doc.get("name", "Team"),
                    "captain": cap_user.get("username", "Captain") if cap_user else "Captain",
                    "members": members_handles,
                })
        elif reg.get("player_id"):
            p_user = await db.users.find_one({"$or": [{"_id": reg["player_id"]}, {"id": reg["player_id"]}]})
            p_name = p_user.get("display_name") or p_user.get("username", "Contender") if p_user else "Contender"
            registered_rosters.append({
                "tag": "SOLO",
                "name": p_name,
                "captain": p_name,
                "members": [p_name],
            })

    # Check for live matches belonging to this tournament
    live_m = await db.matches.find({
        "$or": [{"tournament_id": tid}, {"tournament_id": t.get("slug")}],
        "status": "LIVE"
    }).to_list(length=20)
    has_live = len(live_m) > 0
    live_matches_list = [{
        "id": str(m.get("_id", m.get("id"))),
        "stage": m.get("stage", "Live Match"),
        "match_type": m.get("match_type", "QUARTER_FINAL"),
        "format": m.get("format", "BO1"),
        "series_format": m.get("series_format", "BO1"),
        "youtube_live_url": m.get("youtube_live_url") or m.get("live_link"),
        "live_link": m.get("youtube_live_url") or m.get("live_link"),
        "status": "LIVE",
    } for m in live_m]

    raw_stat = str(t.get("status", "REGISTRATION_OPEN")).upper()
    if raw_stat in ["LIVE", "ONGOING", "IN_PROGRESS"]:
        norm_status = "LIVE"
    elif raw_stat == "DRAFT":
        norm_status = "DRAFT"
    elif raw_stat in ["COMING_SOON", "UPCOMING"]:
        norm_status = "UPCOMING"
    elif raw_stat in ["REGISTRATION_CLOSED", "CLOSED"]:
        norm_status = "REGISTRATION_CLOSED"
    elif raw_stat in ["REGISTRATION_OPEN", "OPEN"]:
        norm_status = "REGISTRATION_OPEN"
    else:
        norm_status = raw_stat

    return TournamentResponse(
        id=tid,
        slug=t.get("slug", tid),
        organizer_id=t.get("organizer_id"),
        title=t.get("title", "Championship Tournament"),
        game_slug=game_obj["slug"] if game_obj else "valorant",
        game_name=game_obj["name"] if game_obj else "Valorant",
        prize_pool=f"₹{prize_inr:,}",
        prize_pool_inr=prize_inr,
        entry_fee=f"₹{fee_inr} / {unit_label}" if fee_inr > 0 else "₹0 / Free",
        entry_fee_inr=fee_inr,
        status=norm_status,
        registration_type=reg_type,
        is_solo=is_solo,
        match_structure=norm_structure,
        competitor_unit=comp_unit,
        players_per_team=t.get("players_per_team") or (1 if is_solo else (4 if norm_structure == "BATTLE_ROYALE" else min_team_size)),
        room_capacity=t.get("room_capacity") or (max_teams_val * (1 if is_solo else 4) if norm_structure == "BATTLE_ROYALE" else None),
        has_live_matches=has_live,
        live_matches_count=len(live_matches_list),
        live_matches=live_matches_list,
        expected_start_date=t.get("expected_start_date"),
        registration_open_date=t.get("registration_open_date"),
        announcement_text=t.get("announcement_text"),
        starts_at=t.get("starts_at", "TBD"),
        roster_lock_time=None,
        server_region=t.get("server_region", "AWS Mumbai (ap-south-1)"),
        registered_teams=reg_count,
        confirmed_teams=reg_count,
        reserved_teams=reserved_count,
        available_slots=avail_slots,
        max_teams=max_teams_val,
        capacity_label=cap_label,
        min_team_size=min_team_size,
        max_team_size=max_team_size,
        rank_requirement=t.get("rank_requirement", "Open to All Ranks"),
        format="Battle Royale" if norm_structure == "BATTLE_ROYALE" else ("Round Robin" if norm_structure == "ROUND_ROBIN" else "Knockout"),
        tournament_type="Battle Royale" if norm_structure == "BATTLE_ROYALE" else ("Round Robin" if norm_structure == "ROUND_ROBIN" else "Knockout"),
        match_format="Battle Royale" if norm_structure == "BATTLE_ROYALE" else ("Round Robin" if norm_structure == "ROUND_ROBIN" else "Knockout"),
        series_format="BO3" if "3" in str(t.get("series_format") or "") or "3" in str(t.get("match_format") or "") else "BO1",
        stage_formats=safe_json_loads(t.get("stage_formats"), {}),
        participation_mode=part_mode,
        registration_deadline=t.get("registration_deadline").isoformat() if isinstance(t.get("registration_deadline"), datetime) else t.get("registration_deadline"),
        check_in_window_minutes=t.get("check_in_window_minutes", 15),
        banner_url=t.get("banner_url") or "",
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
        registered_rosters=registered_rosters,
        is_featured=bool(t.get("is_featured", False)),
        whatsapp_group_url=None,
        discord_url=None,
        community_link=None,
        media_config=t.get("media_config", {}),
        enable_mvp=bool(t.get("enable_mvp", False)),
        prize_distribution=t.get("prize_distribution") or [],
        champion=t.get("champion"),
        tournament_mvp=t.get("tournament_mvp"),
        final_results=t.get("final_results") or [],
        results_display_duration_days=int(t.get("results_display_duration_days", 30)),
        completed_at=str(t["completed_at"]) if t.get("completed_at") else None,
    )

@router.get("/{slug}/matches", response_model=List[MatchResponse])
async def get_tournament_matches(
    slug: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    t = await find_tournament_by_id_or_slug(db, slug)
    if not t:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(t.get("_id", t.get("id")))
    t_slug = t.get("slug", tid)
    matches = await db.matches.find({
        "$or": [{"tournament_id": tid}, {"tournament_id": t_slug}],
        "roster_status": {"$ne": "DRAFT"},
    }).to_list(length=100)

    gid = t.get("game_id")
    game = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]})

    from app.api.v1.endpoints.matches import is_match_authorized_viewer
    is_admin_or_organizer = bool(current_user and current_user.role in ["SUPER_ADMIN", "ADMIN", "ORGANIZER"])

    response = []
    for m in matches:
        mid = str(m.get("_id", m.get("id")))
        team_a_id = m.get("team_a_id")
        team_b_id = m.get("team_b_id")

        team_a = await db.teams.find_one({"$or": [{"_id": team_a_id}, {"id": team_a_id}]}) if team_a_id else None
        if not team_a and team_a_id:
            user_a = await db.users.find_one({"$or": [{"_id": team_a_id}, {"id": team_a_id}]})
            if user_a:
                team_a = {"name": user_a.get("display_name") or user_a.get("username", "Contender"), "tag": "SOLO"}

        team_b = await db.teams.find_one({"$or": [{"_id": team_b_id}, {"id": team_b_id}]}) if team_b_id else None
        if not team_b and team_b_id:
            user_b = await db.users.find_one({"$or": [{"_id": team_b_id}, {"id": team_b_id}]})
            if user_b:
                team_b = {"name": user_b.get("display_name") or user_b.get("username", "Contender"), "tag": "SOLO"}

        match_status = str(m.get("status", "")).upper()
        credentials_released = bool(m.get("credentials_released", False)) or (match_status == "LIVE")
        is_auth = await is_match_authorized_viewer(current_user, m, db)

        if is_admin_or_organizer or (credentials_released and is_auth):
            room_id = m.get("room_id") or m.get("lobby_code")
            room_password = m.get("room_password") or m.get("lobby_password")
            lobby_code = m.get("lobby_code") or m.get("room_id")
            lobby_password = m.get("lobby_password") or m.get("room_password")
            custom_instructions = m.get("custom_instructions") or m.get("match_message")
        else:
            room_id = None
            room_password = None
            lobby_code = None
            lobby_password = None
            custom_instructions = None

        m_num = int(m.get("match_number") or m.get("order") or 1)
        r_name = m.get("round_name") or m.get("stage") or "Round 1"
        r_num = int(m.get("round_number") or 1)

        response.append(
            MatchResponse(
                id=mid,
                tournament_id=tid,
                tournament_title=t.get("title", "Tournament"),
                game_slug=game["slug"] if game else "valorant",
                game_name=game["name"] if game else "Valorant",
                stage=m.get("stage", "QUARTER_FINAL"),
                round_name=r_name,
                round_number=r_num,
                match_number=m_num,
                format=m.get("format", "BO1"),
                series_format=m.get("series_format", "BO1"),
                scheduled_time=m.get("scheduled_time", "TBD"),
                scheduled_date=m.get("scheduled_date"),
                status=m.get("status", "SCHEDULED"),
                server_region=m.get("server_region", t.get("server_region", "AWS Mumbai (ap-south-1)")),
                lobby_code=lobby_code,
                lobby_password=lobby_password,
                room_id=room_id,
                room_password=room_password,
                custom_instructions=custom_instructions,
                credentials_released=credentials_released,
                teams=m.get("teams", []),
                team_ids=m.get("team_ids", []),
                placements=m.get("placements", []),
                qualifier_team_ids=m.get("qualifier_team_ids", []),
                qualifiers_count=m.get("qualifiers_count"),
                is_final_round=bool(m.get("is_final_round", False)),
                is_solo=bool(m.get("is_solo", t.get("is_solo", False))),
                check_in_status=m.get("check_in_status", {}),
                team_a={"name": team_a["name"], "tag": team_a.get("tag", "TEAM"), "score": m.get("score_a", 0)} if team_a else ({"name": m.get("team_a_name"), "tag": m.get("team_a_tag", "TBD"), "score": m.get("score_a", 0)} if m.get("team_a_name") else None),
                team_b={"name": team_b["name"], "tag": team_b.get("tag", "TEAM"), "score": m.get("score_b", 0)} if team_b else ({"name": m.get("team_b_name"), "tag": m.get("team_b_tag", "TBD"), "score": m.get("score_b", 0)} if m.get("team_b_name") else None),
                evidence_url=m.get("evidence_url"),
                dispute_notes=m.get("dispute_notes"),
            )
        )
    return response

@router.post("/{id}/register")
async def register_team_for_tournament(
    id: str,
    payload: TournamentRegisterRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Section 39: Complete Tournament Registration Validation in MongoDB:
    - Tournament exists
    - Registration is currently open
    - Tournament capacity has not been reached
    - Team exists
    - Player is captain/authorized
    - Team belongs to correct game discipline
    - Team is not already registered
    """
    tourn = await find_tournament_by_id_or_slug(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))

    # 1. Registration open check
    if tourn.get("status") != "REGISTRATION_OPEN":
        raise HTTPException(
            status_code=400,
            detail=f"Registration is not open for this tournament (current status: {tourn.get('status')})",
        )

    # 1b. Registration deadline check
    reg_deadline = tourn.get("registration_deadline")
    if reg_deadline:
        try:
            if isinstance(reg_deadline, str):
                reg_deadline_dt = datetime.fromisoformat(reg_deadline.replace("Z", "+00:00"))
            else:
                reg_deadline_dt = reg_deadline
            now_utc = datetime.now(timezone.utc)
            if reg_deadline_dt.tzinfo is None:
                reg_deadline_dt = reg_deadline_dt.replace(tzinfo=timezone.utc)
            if now_utc > reg_deadline_dt:
                await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": {"status": "REGISTRATION_CLOSED"}})
                raise HTTPException(
                    status_code=400,
                    detail="Registration deadline for this tournament has passed.",
                )
        except HTTPException:
            raise
        except Exception:
            pass

    # 2. Capacity check
    reg_count = await db.tournament_registrations.count_documents({
        "$or": [{"tournament_id": tid}, {"tournament_id": tourn.get("slug")}],
        "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
    })

    max_teams = tourn.get("max_teams", 16)
    if reg_count >= max_teams:
        await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": {"status": "REGISTRATION_CLOSED"}})
        raise HTTPException(
            status_code=400,
            detail=f"Tournament capacity of {max_teams} teams has been reached",
        )

    fee_inr = tourn.get("entry_fee_inr", 0)

    gid = tourn.get("game_id")
    game_obj = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]})
    if game_obj and game_obj.get("status") == "COMING_SOON":
        raise HTTPException(
            status_code=400,
            detail=f"{game_obj.get('name', 'This game')} is coming soon and tournament registration is not available yet.",
        )

    is_solo_game = bool(game_obj and (not game_obj.get("is_team_game", True) or game_obj.get("slug") == "chess"))
    raw_tourn_fmt = str(tourn.get("format") or tourn.get("tournament_type") or tourn.get("match_format") or "").lower()
    is_solo_tourn = bool(
        tourn.get("is_solo")
        or tourn.get("registration_type") == "SOLO"
        or is_solo_game
        or "solo" in raw_tourn_fmt
        or "1v1" in raw_tourn_fmt
        or (tourn.get("min_team_size") == 1 and tourn.get("max_team_size") == 1)
    )

    user_game_id = payload.game_id or payload.in_game_id

    if is_solo_tourn or not payload.team_id:
        # Solo player registration without requiring a team
        existing_reg = await db.tournament_registrations.find_one({
            "$or": [{"tournament_id": tid}, {"tournament_id": tourn.get("slug")}],
            "player_id": current_user.id,
            "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
        })
        if existing_reg:
            raise HTTPException(status_code=400, detail="You are already registered for this tournament")

        # 1. Debit wallet funds FIRST if paid
        wallet_txn_id = None
        reg_id = f"reg_{uuid.uuid4().hex[:12]}"
        if fee_inr > 0:
            fee_paise = int(round(fee_inr * 100))
            wallet_txn_id = f"txn_tourn_{uuid.uuid4().hex[:12]}"
            await debit_wallet_funds(
                db=db,
                user_id=current_user.id,
                amount_paise=fee_paise,
                txn_type="TOURNAMENT_ENTRY",
                reference_id=reg_id,
                description=f"Solo entry fee: {tourn.get('title', 'Tournament')}",
            )

        # 2. Insert registration record with PAID or FREE status
        reg = TournamentRegistration(
            id=reg_id,
            tournament_id=tid,
            player_id=current_user.id,
            game_id=user_game_id,
            in_game_id=user_game_id,
            payment_method="WALLET" if fee_inr > 0 else "FREE",
            payment_status="PAID" if fee_inr > 0 else "FREE",
            order_id=wallet_txn_id or f"reg_{uuid.uuid4().hex[:10]}",
            wallet_txn_id=wallet_txn_id,
            check_in_status="PENDING",
        )
        try:
            await db.tournament_registrations.insert_one(reg.to_mongo())
        except DuplicateKeyError:
            if fee_inr > 0:
                from app.services.wallet_service import credit_wallet_funds
                await credit_wallet_funds(
                    db=db,
                    user_id=current_user.id,
                    amount_paise=fee_paise,
                    txn_type="REFUND",
                    reference_id=wallet_txn_id,
                    description="Refund duplicate tournament entry",
                )
            raise HTTPException(status_code=400, detail="You are already registered for this tournament")

        if reg_count + 1 >= max_teams:
            await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": {"status": "REGISTRATION_CLOSED"}})

        await create_notification(
            db=db,
            user_id=current_user.id,
            title="Tournament Registered",
            message=f'You are confirmed as a solo contender for "{tourn.get("title", "Tournament")}". Check bracket schedule for match timings.',
            notification_type="REGISTRATION",
            category="REGISTRATION",
            link=f"/tournaments/{tourn.get('slug', tid)}",
        )

        return {
            "status": "success",
            "message": f'Successfully registered as a solo contender for {tourn.get("title", "Tournament")}!',
            "order_id": reg.order_id,
            "entry_fee_inr": fee_inr,
        }

    # If game or tournament is solo, multi-player teams are strictly prohibited
    if is_solo_tourn or is_solo_game:
        raise HTTPException(
            status_code=400,
            detail=f"{tourn.get('title', 'This competition')} is a solo discipline. Team registration is not permitted. Please register as an individual contender.",
        )

    # 3. Team check
    team = await db.teams.find_one({"$or": [{"_id": payload.team_id}, {"id": payload.team_id}]})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    team_id = str(team.get("_id", team.get("id")))

    # 4. Captain authorization
    valid_captain_ids = {str(current_user.id)}
    if getattr(current_user, "clerk_user_id", None):
        valid_captain_ids.add(str(current_user.clerk_user_id))
    if str(team.get("captain_id")) not in valid_captain_ids and current_user.role != "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the verified team captain can register this team",
        )

    # 5. Game discipline match
    valid_game_ids = {str(tourn.get("game_id"))}
    if game_obj:
        for k in ("_id", "id", "slug"):
            if game_obj.get(k):
                valid_game_ids.add(str(game_obj.get(k)))
    if str(team.get("game_id")) not in valid_game_ids:
        raise HTTPException(
            status_code=400,
            detail="Your team is registered for a different competitive title than this tournament",
        )

    # 5b. Roster bounds check
    tourn_min = tourn.get("min_team_size") or tourn.get("required_team_size")
    game_min = game_obj.get("min_team_size") if game_obj else None
    if is_solo_game:
        min_size = 1
    elif tourn_min and int(tourn_min) > 0:
        min_size = int(tourn_min)
    elif game_min and int(game_min) > 0:
        min_size = int(game_min)
    else:
        min_size = 1

    tourn_max = tourn.get("max_team_size")
    game_max = game_obj.get("max_team_size") if game_obj else None
    if is_solo_game:
        max_size = 1
    elif tourn_max and int(tourn_max) >= min_size:
        max_size = int(tourn_max)
    elif game_max and int(game_max) >= min_size:
        max_size = int(game_max)
    else:
        max_size = max(min_size, 5)

    members = team.get("members", [])
    members_count = len(members)
    cap_id = team.get("captain_id")
    if cap_id and not any((m.get("user_id") if isinstance(m, dict) else getattr(m, "user_id", None)) == cap_id for m in members):
        members_count += 1

    if members_count < min_size:
        raise HTTPException(
            status_code=400,
            detail=f"Your team has {members_count} member(s), but this tournament requires a minimum of {min_size} players. Please recruit {min_size - members_count} more player(s) before registering.",
        )
    if members_count > max_size:
        raise HTTPException(
            status_code=400,
            detail=f"Your team has {members_count} member(s), but this tournament allows a maximum of {max_size} players.",
        )

    # 6. Duplicate registration check
    existing_reg = await db.tournament_registrations.find_one({
        "tournament_id": tid,
        "team_id": team_id,
        "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
    })

    if existing_reg:
        raise HTTPException(status_code=400, detail="Team is already registered for this tournament")

    # 7. Debit entry fee from captain wallet FIRST if paid tournament
    wallet_txn_id = None
    reg_id = f"reg_{uuid.uuid4().hex[:12]}"
    if fee_inr > 0:
        fee_paise = int(round(fee_inr * 100))
        wallet_txn_id = f"txn_tourn_{uuid.uuid4().hex[:12]}"
        await debit_wallet_funds(
            db=db,
            user_id=current_user.id,
            amount_paise=fee_paise,
            txn_type="TOURNAMENT_ENTRY",
            reference_id=reg_id,
            description=f"Team entry fee: {tourn.get('title', 'Tournament')} ({team.get('name')})",
        )

    # 8. Create registration record with PAID or FREE status
    reg = TournamentRegistration(
        id=reg_id,
        tournament_id=tid,
        team_id=team_id,
        game_id=user_game_id,
        in_game_id=user_game_id,
        payment_method="WALLET" if fee_inr > 0 else "FREE",
        payment_status="PAID" if fee_inr > 0 else "FREE",
        order_id=wallet_txn_id or f"reg_{uuid.uuid4().hex[:10]}",
        wallet_txn_id=wallet_txn_id,
        check_in_status="PENDING",
    )
    try:
        await db.tournament_registrations.insert_one(reg.to_mongo())
    except DuplicateKeyError:
        if fee_inr > 0:
            from app.services.wallet_service import credit_wallet_funds
            await credit_wallet_funds(
                db=db,
                user_id=current_user.id,
                amount_paise=fee_paise,
                txn_type="REFUND",
                reference_id=wallet_txn_id,
                description=f"Refund duplicate team entry ({team.get('name')})",
            )
        raise HTTPException(status_code=400, detail="Team is already registered for this tournament")

    if reg_count + 1 >= max_teams:
        await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": {"status": "REGISTRATION_CLOSED"}})

    await create_notification(
        db=db,
        user_id=current_user.id,
        title="Tournament Registered",
        message=f'Team "{team["name"]}" is confirmed for "{tourn.get("title", "Tournament")}". Check bracket schedule for match timings.',
        notification_type="REGISTRATION",
        category="REGISTRATION",
        link=f"/tournaments/{tourn.get('slug', tid)}",
    )

    return {
        "status": "success",
        "message": f'Team "{team["name"]}" successfully registered for {tourn.get("title", "Tournament")}!',
        "registration_id": reg.id,
        "payment_status": reg.payment_status,
        "wallet_txn_id": reg.wallet_txn_id,
        "order_id": reg.order_id,
        "entry_fee_inr": fee_inr,
    }

@router.get("/{id}/registration-status", response_model=TournamentRegistrationStatusResponse)
async def get_registration_status(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns whether current user (or user's teams) is currently registered for the tournament,
    along with registration details, registration position, and registered team roster.
    """
    tourn = await find_tournament_by_id_or_slug(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    valid_user_ids = [str(current_user.id)]
    if getattr(current_user, "clerk_user_id", None):
        valid_user_ids.append(str(current_user.clerk_user_id))

    # 1. Check solo registration
    reg = await db.tournament_registrations.find_one({
        "tournament_id": {"$in": [tid, tslug]},
        "player_id": {"$in": valid_user_ids},
        "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
        "status": {"$nin": ["CANCELLED", "REFUNDED"]},
    })

    # 2. Check team registration if not found as solo
    if not reg:
        user_teams_cursor = db.teams.find({
            "$or": [
                {"captain_id": {"$in": valid_user_ids}},
                {"members.user_id": {"$in": valid_user_ids}},
                {"substitutes.user_id": {"$in": valid_user_ids}},
            ]
        })
        user_teams = await user_teams_cursor.to_list(length=50)
        team_ids = [str(t.get("_id", t.get("id"))) for t in user_teams]
        if team_ids:
            reg = await db.tournament_registrations.find_one({
                "tournament_id": {"$in": [tid, tslug]},
                "team_id": {"$in": team_ids},
                "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
                "status": {"$nin": ["CANCELLED", "REFUNDED"]},
            })

    if not reg:
        return TournamentRegistrationStatusResponse(
            is_registered=False,
            status=None,
            payment_status=None,
            registration=None,
            team=None,
            position=None,
        )

    reg_dict = dict(reg)
    reg_dict["id"] = str(reg_dict.get("_id", reg_dict.get("id")))
    reg_dict.pop("_id", None)
    if reg_dict.get("created_at") and hasattr(reg_dict["created_at"], "isoformat"):
        reg_dict["created_at"] = reg_dict["created_at"].isoformat()
    if reg_dict.get("registered_at") and hasattr(reg_dict["registered_at"], "isoformat"):
        reg_dict["registered_at"] = reg_dict["registered_at"].isoformat()

    # Look up payment record for screenshot_url / rejection_reason / utr / fee_paid if missing
    p_id = reg_dict.get("payment_id")
    if p_id:
        p_doc = await db.game_payments.find_one({"$or": [{"_id": p_id}, {"id": p_id}]})
        if p_doc:
            if not reg_dict.get("screenshot_url") and p_doc.get("screenshot_url"):
                reg_dict["screenshot_url"] = p_doc.get("screenshot_url")
            if not reg_dict.get("rejection_reason") and p_doc.get("rejection_reason"):
                reg_dict["rejection_reason"] = p_doc.get("rejection_reason")
            if not reg_dict.get("utr") and p_doc.get("utr"):
                reg_dict["utr"] = p_doc.get("utr")
            if "fee_paid" not in reg_dict and p_doc.get("amount_inr") is not None:
                reg_dict["fee_paid"] = p_doc.get("amount_inr")

    # Calculate position if confirmed
    position = None
    if reg_dict.get("payment_status") in ["PAID", "FREE"]:
        reg_time = reg.get("created_at") or reg.get("registered_at")
        pos_count = await db.tournament_registrations.count_documents({
            "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
            "payment_status": {"$in": ["PAID", "FREE"]},
            "created_at": {"$lte": reg_time} if reg_time else {"$exists": True},
        })
        position = max(1, pos_count)

    team_payload = None
    if reg_dict.get("team_id"):
        team_doc = await db.teams.find_one({"$or": [{"_id": reg_dict["team_id"]}, {"id": reg_dict["team_id"]}]})
        if team_doc:
            captain_id = str(team_doc.get("captain_id"))
            cap_user = await db.users.find_one({"$or": [{"_id": captain_id}, {"id": captain_id}, {"clerk_user_id": captain_id}]})
            captain_name = (cap_user.get("display_name") or cap_user.get("username", "Captain")) if cap_user else "Captain"

            raw_members = team_doc.get("members", [])
            user_ids_to_fetch = set()
            if captain_id:
                user_ids_to_fetch.add(captain_id)
            for m in raw_members:
                if isinstance(m, dict) and m.get("user_id"):
                    user_ids_to_fetch.add(str(m.get("user_id")))

            user_docs = await db.users.find({
                "$or": [{"_id": {"$in": list(user_ids_to_fetch)}}, {"id": {"$in": list(user_ids_to_fetch)}}, {"clerk_user_id": {"$in": list(user_ids_to_fetch)}}]
            }).to_list(len(user_ids_to_fetch) + 10)
            user_map = {}
            for u in user_docs:
                u_id = str(u.get("_id", u.get("id")))
                user_map[u_id] = u
                if u.get("clerk_user_id"):
                    user_map[str(u["clerk_user_id"])] = u

            members_list = []
            cap_info = user_map.get(captain_id)
            members_list.append({
                "user_id": captain_id,
                "username": cap_info.get("username", captain_name) if cap_info else captain_name,
                "display_name": (cap_info.get("display_name") or cap_info.get("username", captain_name)) if cap_info else captain_name,
                "avatar_url": cap_info.get("avatar_url") if cap_info else None,
                "role": "CAPTAIN",
                "game_handle": cap_info.get("username", captain_name) if cap_info else captain_name,
            })

            for m in raw_members:
                if not isinstance(m, dict):
                    continue
                uid = str(m.get("user_id"))
                if uid == captain_id:
                    continue
                uinfo = user_map.get(uid)
                uname = uinfo.get("username", "player") if uinfo else "player"
                dname = (uinfo.get("display_name") or uname) if uinfo else uname
                members_list.append({
                    "user_id": uid,
                    "username": uname,
                    "display_name": dname,
                    "avatar_url": uinfo.get("avatar_url") if uinfo else None,
                    "role": m.get("role", "MEMBER"),
                    "game_handle": m.get("game_handle") or uname,
                })

            is_captain = any(str(cid) in valid_user_ids for cid in [captain_id])
            team_payload = {
                "id": str(team_doc.get("_id", team_doc.get("id"))),
                "name": team_doc.get("name", "Team"),
                "tag": team_doc.get("tag", "TEAM"),
                "logo_url": team_doc.get("logo_url"),
                "captain_id": captain_id,
                "captain_name": captain_name,
                "my_role": "CAPTAIN" if is_captain else "PLAYER",
                "members": members_list,
            }
    else:
        team_payload = {
            "id": str(current_user.id),
            "name": current_user.display_name or current_user.username,
            "tag": "SOLO",
            "logo_url": None,
            "captain_id": str(current_user.id),
            "captain_name": current_user.username,
            "my_role": "CAPTAIN",
            "members": [{
                "user_id": str(current_user.id),
                "username": current_user.username,
                "display_name": current_user.display_name or current_user.username,
                "avatar_url": current_user.avatar_url,
                "role": "SOLO CONTENDER",
                "game_handle": current_user.username,
            }],
        }

    return TournamentRegistrationStatusResponse(
        is_registered=True,
        status=reg_dict.get("status", "CONFIRMED" if reg_dict.get("payment_status") in ["PAID", "FREE"] else "PENDING"),
        payment_status=reg_dict.get("payment_status"),
        registration=reg_dict,
        team=team_payload,
    )

@router.post("/{id}/unregister")
async def unregister_from_tournament(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Cancel registration and unregister participant from tournament.
    Refunds entry fee to user wallet if paid via wallet.
    Only registration owner (solo player or team captain) or admin can cancel.
    Prevents cancellation if tournament has active matches or recorded results.
    """
    tourn = await find_tournament_by_id_or_slug(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    # 1. Check tournament lifecycle state
    t_status = str(tourn.get("status", "")).upper()
    is_admin = current_user.role in ["SUPER_ADMIN", "ADMIN"]
    if t_status in ["LIVE", "IN_PROGRESS", "ACTIVE", "COMPLETED", "ENDED"] and not is_admin:
        raise HTTPException(
            status_code=400,
            detail="Cannot unregister: Tournament matches are already active or concluded."
        )

    valid_user_ids = {str(current_user.id)}
    if getattr(current_user, "clerk_user_id", None):
        valid_user_ids.add(str(current_user.clerk_user_id))

    # Find the user's teams
    user_teams = await db.teams.find({"captain_id": {"$in": list(valid_user_ids)}}).to_list(length=50)
    user_team_ids = [str(t.get("_id", t.get("id"))) for t in user_teams]

    # 2. Find active registration
    reg_query = {
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"payment_status": {"$in": ["PAID", "FREE", "PENDING", "PENDING_VERIFICATION"]}},
            {"$or": [
                {"player_id": {"$in": list(valid_user_ids)}},
                {"user_id": {"$in": list(valid_user_ids)}},
                {"team_id": {"$in": user_team_ids}}
            ]}
        ]
    }
    reg = await db.tournament_registrations.find_one(reg_query)
    if not reg:
        raise HTTPException(status_code=404, detail="No active registration found for this tournament.")

    # 3. Prevent unregistration if this participant is in an active or completed match
    part_id = str(reg.get("team_id") or reg.get("player_id") or reg.get("user_id"))
    active_match = await db.matches.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [
                {"team_a_id": part_id},
                {"team_b_id": part_id},
                {"registered_participant_ids": part_id},
            ]},
            {"status": {"$in": ["LIVE", "COMPLETED", "RESULTS_SAVED"]}}
        ]
    })
    if active_match and not is_admin:
        raise HTTPException(
            status_code=400,
            detail="Cannot unregister: You are participating in a match that is live or completed."
        )

    # 4. Handle refund if entry fee was paid via wallet
    refunded_inr = 0
    now = datetime.now(timezone.utc)
    is_paid = reg.get("payment_status") == "PAID"
    paid_via_wallet = reg.get("payment_method") in ["WALLET", None] or "amount_paid" in reg
    if is_paid and paid_via_wallet:
        fee_inr = int(tourn.get("entry_fee_inr") or tourn.get("entry_fee") or reg.get("amount_paid") or 0)
        if fee_inr > 0:
            fee_paise = int(round(fee_inr * 100))
            refund_ref = f"refund_{reg.get('id')}_{uuid.uuid4().hex[:8]}"
            await credit_wallet_funds(
                db=db,
                user_id=reg.get("player_id") or reg.get("user_id") or current_user.id,
                amount_paise=fee_paise,
                txn_type="REFUND",
                reference_id=refund_ref,
                description=f"Entry fee refund: Cancelled registration for {tourn.get('title')}",
            )
            refunded_inr = fee_inr

    # 5. Mark registration as CANCELLED
    await db.tournament_registrations.update_one(
        {"_id": reg["_id"]},
        {"$set": {
            "payment_status": "CANCELLED",
            "status": "CANCELLED",
            "cancelled_at": now,
            "cancelled_by": current_user.username,
            "refunded_inr": refunded_inr,
        }}
    )

    # 6. Remove from any scheduled upcoming round eligible_contenders
    await db.tournament_rounds.update_many(
        {
            "$and": [
                {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
                {"status": {"$in": ["UPCOMING", "SCHEDULED"]}}
            ]
        },
        {"$pull": {"eligible_contenders": {"id": part_id}}}
    )

    # 7. Decrement participant counts and reopen registration if closed
    t_updates: Dict[str, Any] = {}
    if (tourn.get("current_participants") or 0) > 0:
        t_updates["current_participants"] = max(0, int(tourn.get("current_participants", 1)) - 1)
    if (tourn.get("current_teams") or 0) > 0:
        t_updates["current_teams"] = max(0, int(tourn.get("current_teams", 1)) - 1)
    if tourn.get("status") == "REGISTRATION_CLOSED" and not tourn.get("is_registration_closed"):
        t_updates["status"] = "REGISTRATION_OPEN"

    if t_updates:
        await db.tournaments.update_one(
            {"_id": tourn["_id"]},
            {"$set": t_updates}
        )

    # 8. Create in-app notification
    await create_notification(
        db=db,
        user_id=current_user.id,
        title="Tournament Registration Cancelled",
        message=f'Your registration for "{tourn.get("title")}" has been cancelled.' + (f' ₹{refunded_inr} has been refunded to your wallet.' if refunded_inr > 0 else ''),
        notification_type="REGISTRATION",
        category="REGISTRATION",
        link=f"/tournaments/{tslug}",
    )

    return {
        "status": "success",
        "message": f'Registration cancelled successfully.' + (f' ₹{refunded_inr} refunded to your wallet.' if refunded_inr > 0 else ''),
        "refunded_amount_inr": refunded_inr,
    }

@router.get("/{id}/credentials")
async def get_tournament_participant_credentials(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns official match room credentials for registered participants when released.
    """
    tourn = await find_tournament_by_id_or_slug(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    is_admin = current_user.role in ["SUPER_ADMIN", "ADMIN", "ORGANIZER"]

    valid_user_ids = {str(current_user.id)}
    if getattr(current_user, "clerk_user_id", None):
        valid_user_ids.add(str(current_user.clerk_user_id))

    # Check registration
    reg = await db.tournament_registrations.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"payment_status": {"$in": ["PAID", "FREE"]}},
            {"$or": [
                {"player_id": {"$in": list(valid_user_ids)}},
                {"team_id": {"$exists": True}}
            ]}
        ]
    })
    is_registered = bool(reg) or is_admin
    if not is_registered and not is_admin:
        # Check team membership
        user_teams = await db.teams.find({
            "$or": [
                {"captain_id": {"$in": list(valid_user_ids)}},
                {"members.user_id": {"$in": list(valid_user_ids)}}
            ]
        }).to_list(length=20)
        team_ids = [str(t.get("_id", t.get("id"))) for t in user_teams]
        if team_ids:
            team_reg = await db.tournament_registrations.find_one({
                "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
                "team_id": {"$in": team_ids},
                "payment_status": {"$in": ["PAID", "FREE"]},
            })
            if team_reg:
                is_registered = True

    if not is_registered:
        raise HTTPException(status_code=403, detail="Only registered participants can access match room credentials.")

    # 1. Check user active match in this tournament
    user_part_ids = list(valid_user_ids)
    if reg and reg.get("team_id"):
        user_part_ids.append(str(reg.get("team_id")))

    user_match = await db.matches.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"$or": [
                {"team_a_id": {"$in": user_part_ids}},
                {"team_b_id": {"$in": user_part_ids}},
                {"registered_participant_ids": {"$in": user_part_ids}},
            ]},
            {"status": {"$ne": "DELETED"}},
        ]
    })
    if user_match:
        m_stat = str(user_match.get("status", "")).upper()
        m_released = bool(user_match.get("credentials_released")) or m_stat == "LIVE" or is_admin
        if m_released and user_match.get("room_id"):
            return {
                "status": "success",
                "is_released": True,
                "room_id": user_match.get("room_id"),
                "room_password": user_match.get("room_password") or "None",
                "custom_instructions": user_match.get("custom_instructions"),
                "server_region": user_match.get("server_region") or tourn.get("server_region", "AWS Mumbai (ap-south-1)"),
                "round_name": user_match.get("round_name") or "Match",
                "match_number": user_match.get("match_number", 1),
            }

    # 2. Check active round in tournament_rounds
    active_round = await db.tournament_rounds.find_one({
        "$and": [
            {"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]},
            {"status": {"$in": ["LIVE", "UPCOMING", "SCHEDULED"]}},
        ]
    })
    if active_round:
        r_stat = str(active_round.get("status", "")).upper()
        r_released = bool(active_round.get("credentials_released")) or r_stat == "LIVE" or is_admin
        if r_released and active_round.get("room_id"):
            return {
                "status": "success",
                "is_released": True,
                "room_id": active_round.get("room_id"),
                "room_password": active_round.get("room_password") or "None",
                "custom_instructions": active_round.get("custom_instructions"),
                "server_region": tourn.get("server_region", "AWS Mumbai (ap-south-1)"),
                "round_name": active_round.get("stage_name") or active_round.get("round_name"),
            }

    # 3. Check tournament-level customRoomId
    t_room_id = tourn.get("custom_room_id") or tourn.get("customRoomId")
    if t_room_id and (tourn.get("status") == "LIVE" or is_admin):
        return {
            "status": "success",
            "is_released": True,
            "room_id": t_room_id,
            "room_password": tourn.get("custom_room_password") or tourn.get("customRoomPassword") or "None",
            "custom_instructions": tourn.get("custom_instructions"),
            "server_region": tourn.get("server_region", "AWS Mumbai (ap-south-1)"),
        }

    return {
        "status": "success",
        "is_released": False,
        "message": "Credentials will be live prior to the game.",
    }


@router.post("/{id}/register-upi", response_model=TournamentRegisterUpiResponse)
@router.post("/{id}/register/upi", response_model=TournamentRegisterUpiResponse)
async def register_tournament_direct_upi(
    id: str,
    payload: TournamentRegisterRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Direct UPI Tournament Registration Flow:
    1. Validates tournament status, deadline, and capacity.
    2. Validates team requirements (or solo player).
    3. Fetches active rotating platform UPI account.
    4. Creates TournamentRegistration with PENDING (immediately reserves slot).
    5. Creates GamePayment with TOURNAMENT_ENTRY and INITIATED status.
    6. Returns UPI ID, QR code and payee details for user transfer.
    """
    tourn = await find_tournament_by_id_or_slug(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))

    if tourn.get("status") != "REGISTRATION_OPEN":
        raise HTTPException(
            status_code=400,
            detail=f"Registration is not open for this tournament (current status: {tourn.get('status')})",
        )

    # Check deadline
    reg_deadline = tourn.get("registration_deadline")
    if reg_deadline:
        try:
            reg_deadline_dt = datetime.fromisoformat(reg_deadline.replace("Z", "+00:00")) if isinstance(reg_deadline, str) else reg_deadline
            now_utc = datetime.now(timezone.utc)
            if reg_deadline_dt.tzinfo is None:
                reg_deadline_dt = reg_deadline_dt.replace(tzinfo=timezone.utc)
            if now_utc > reg_deadline_dt:
                await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": {"status": "REGISTRATION_CLOSED"}})
                raise HTTPException(status_code=400, detail="Registration deadline has passed")
        except HTTPException:
            raise
        except Exception:
            pass

    # Capacity check
    reg_count = await db.tournament_registrations.count_documents({
        "$or": [{"tournament_id": tid}, {"tournament_id": tourn.get("slug")}],
        "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
    })
    max_teams = tourn.get("max_teams", 16)
    if reg_count >= max_teams:
        await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": {"status": "REGISTRATION_CLOSED"}})
        raise HTTPException(status_code=400, detail=f"Tournament capacity of {max_teams} teams has been reached")

    fee_inr = tourn.get("entry_fee_inr", 0)
    fee_paise = int(round(fee_inr * 100))

    gid = tourn.get("game_id")
    game_obj = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]})
    if game_obj and game_obj.get("status") == "COMING_SOON":
        raise HTTPException(
            status_code=400,
            detail=f"{game_obj.get('name', 'This game')} is coming soon and tournament registration is not available yet.",
        )

    is_solo_game = bool(game_obj and (not game_obj.get("is_team_game", True) or game_obj.get("slug") == "chess"))
    raw_tourn_fmt = str(tourn.get("format") or tourn.get("tournament_type") or tourn.get("match_format") or "").lower()
    is_solo_tourn = bool(
        tourn.get("is_solo")
        or tourn.get("registration_type") == "SOLO"
        or is_solo_game
        or "solo" in raw_tourn_fmt
        or "1v1" in raw_tourn_fmt
        or (tourn.get("min_team_size") == 1 and tourn.get("max_team_size") == 1)
    )

    if is_solo_tourn and payload.team_id:
        raise HTTPException(status_code=400, detail="This is a solo competition. Individual player registration is required.")
    if not is_solo_tourn and not payload.team_id:
        raise HTTPException(status_code=400, detail="Team selection is required for team tournaments.")

    team_id = None
    team_name = None

    if payload.team_id:
        if is_solo_game or is_solo_tourn:
            raise HTTPException(status_code=400, detail="This is a solo competition. Individual player registration is required.")
        team = await db.teams.find_one({"$or": [{"_id": payload.team_id}, {"id": payload.team_id}]})
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        valid_captain_ids = {str(current_user.id)}
        if getattr(current_user, "clerk_user_id", None):
            valid_captain_ids.add(str(current_user.clerk_user_id))
        if str(team.get("captain_id")) not in valid_captain_ids and current_user.role != "SUPER_ADMIN":
            raise HTTPException(status_code=403, detail="Only verified team captain can register this team")
        team_id = str(team.get("_id", team.get("id")))
        team_name = team.get("name")

        valid_game_ids = {str(tourn.get("game_id"))}
        if game_obj:
            for k in ("_id", "id", "slug"):
                if game_obj.get(k):
                    valid_game_ids.add(str(game_obj.get(k)))
        if str(team.get("game_id")) not in valid_game_ids:
            raise HTTPException(
                status_code=400,
                detail="Your team is registered for a different competitive title than this tournament",
            )

        # Roster bounds check
        tourn_min = tourn.get("min_team_size") or tourn.get("required_team_size")
        game_min = game_obj.get("min_team_size") if game_obj else None
        if is_solo_game:
            min_size = 1
        elif tourn_min and int(tourn_min) > 0:
            min_size = int(tourn_min)
        elif game_min and int(game_min) > 0:
            min_size = int(game_min)
        else:
            min_size = 1

        tourn_max = tourn.get("max_team_size")
        game_max = game_obj.get("max_team_size") if game_obj else None
        if is_solo_game:
            max_size = 1
        elif tourn_max and int(tourn_max) >= min_size:
            max_size = int(tourn_max)
        elif game_max and int(game_max) >= min_size:
            max_size = int(game_max)
        else:
            max_size = max(min_size, 5)

        members = team.get("members", [])
        members_count = len(members)
        cap_id = team.get("captain_id")
        if cap_id and not any((m.get("user_id") if isinstance(m, dict) else getattr(m, "user_id", None)) == cap_id for m in members):
            members_count += 1

        if members_count < min_size:
            raise HTTPException(
                status_code=400,
                detail=f"Your team has {members_count} member(s), but this tournament requires a minimum of {min_size} players. Please recruit {min_size - members_count} more player(s) before registering.",
            )
        if members_count > max_size:
            raise HTTPException(
                status_code=400,
                detail=f"Your team has {members_count} member(s), but this tournament allows a maximum of {max_size} players.",
            )

        existing_reg = await db.tournament_registrations.find_one({
            "tournament_id": tid,
            "team_id": team_id,
            "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
        })
        if existing_reg:
            raise HTTPException(status_code=400, detail="Team is already registered for this tournament")
    else:
        existing_reg = await db.tournament_registrations.find_one({
            "tournament_id": tid,
            "player_id": current_user.id,
            "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
        })
        if existing_reg:
            raise HTTPException(status_code=400, detail="You are already registered for this tournament")

    # Fetch active rotating admin UPI account
    upi_account = await get_next_active_upi_account(db)
    if not upi_account:
        raise HTTPException(
            status_code=503,
            detail="No active platform UPI payment accounts currently available. Please use wallet balance or contact support.",
        )

    upi_id_str = upi_account.get("upi_id")
    payee_name_str = upi_account.get("payee_name", "Ametheus Esports")
    account_code_str = upi_account.get("code", "UPI001")
    account_id_str = str(upi_account.get("_id", upi_account.get("id")))
    qr_url_str = upi_account.get("qr_url")

    upi_uri = build_upi_payment_uri(
        upi_id=upi_id_str,
        payee_name=payee_name_str,
        amount_inr=float(fee_inr),
        note=f"Tournament entry fee: {tourn.get('title', 'Tournament')[:25]}",
    )

    reg_id = f"reg_upi_{uuid.uuid4().hex[:10]}"
    payment = GamePayment(
        payment_type="TOURNAMENT_ENTRY",
        user_id=current_user.id,
        game_id=tourn.get("game_id"),
        game_name=tourn.get("title"),
        tournament_id=tid,
        registration_id=reg_id,
        team_id=team_id,
        team_name=team_name,
        amount_paise=fee_paise,
        amount_inr=float(fee_inr),
        payment_method="UPI",
        payment_account_id=account_id_str,
        payment_account_code=account_code_str,
        upi_id=upi_id_str,
        payee_name=payee_name_str,
        qr_url=qr_url_str,
        upi_uri=upi_uri,
        status="INITIATED",
    )
    await db.game_payments.insert_one(payment.to_mongo())

    # Create reserved TournamentRegistration with PENDING
    user_game_id = payload.game_id or payload.in_game_id
    reg = TournamentRegistration(
        id=reg_id,
        tournament_id=tid,
        team_id=team_id,
        player_id=current_user.id if not team_id else None,
        game_id=user_game_id,
        in_game_id=user_game_id,
        payment_method="UPI",
        payment_status="PENDING",
        payment_id=payment.id,
        order_id=f"upi_tourn_{uuid.uuid4().hex[:10]}",
        check_in_status="PENDING",
    )
    await db.tournament_registrations.insert_one(reg.to_mongo())

    if reg_count + 1 >= max_teams:
        await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": {"status": "REGISTRATION_CLOSED"}})

    return TournamentRegisterUpiResponse(
        payment_id=payment.id,
        registration_id=reg_id,
        tournament_id=tid,
        tournament_title=tourn.get("title", "Tournament"),
        amount_inr=float(fee_inr),
        amount_paise=fee_paise,
        account_code=account_code_str,
        upi_id=upi_id_str,
        payee_name=payee_name_str,
        qr_url=qr_url_str,
        upi_uri=upi_uri,
        status="INITIATED",
    )

@router.get("/{id}/upi-details", response_model=TournamentUpiDetailsResponse)
async def get_tournament_upi_details(
    id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns active platform UPI payment details and QR code for a tournament
    WITHOUT creating any registration or payment records in the database.
    """
    tourn = await find_tournament_by_id_or_slug(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))
    fee_inr = tourn.get("entry_fee_inr", 0)
    fee_paise = int(round(fee_inr * 100))

    upi_account = await get_next_active_upi_account(db)
    if not upi_account:
        raise HTTPException(
            status_code=503,
            detail="No active platform UPI payment accounts currently available. Please use wallet balance or contact support.",
        )

    upi_id_str = upi_account.get("upi_id")
    payee_name_str = upi_account.get("payee_name", "Ametheus Esports")
    account_code_str = upi_account.get("code", "UPI001")
    qr_url_str = upi_account.get("qr_url")

    upi_uri = build_upi_payment_uri(
        upi_id=upi_id_str,
        payee_name=payee_name_str,
        amount_inr=float(fee_inr),
        note=f"Tournament entry fee: {tourn.get('title', 'Tournament')[:25]}",
    )

    return TournamentUpiDetailsResponse(
        tournament_id=tid,
        tournament_title=tourn.get("title", "Tournament"),
        amount_inr=float(fee_inr),
        amount_paise=fee_paise,
        account_code=account_code_str,
        upi_id=upi_id_str,
        payee_name=payee_name_str,
        qr_url=qr_url_str,
        upi_uri=upi_uri,
    )

@router.post("/{id}/submit-upi-utr")
async def submit_tournament_upi_utr(
    id: str,
    payload: SubmitTournamentUtrRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Submits 12-digit UTR reference for direct UPI tournament registration.
    Only upon valid submission is a registration created with PENDING_VERIFICATION.
    """
    clean_utr = payload.utr.strip() if payload.utr else ""
    if len(clean_utr) < 6:
        raise HTTPException(status_code=400, detail="Invalid UPI Transaction ID / UTR. Must be at least 6 characters.")

    tourn = await find_tournament_by_id_or_slug(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    tid = str(tourn.get("_id", tourn.get("id")))

    # Check for duplicate UTR across existing submitted payments
    existing_utr = await db.game_payments.find_one({
        "utr": clean_utr,
        "status": {"$in": ["PENDING", "PENDING_VERIFICATION", "APPROVED", "SUBMITTED"]}
    })
    if existing_utr:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This UPI Transaction ID / UTR has already been submitted for another payment.",
        )

    payment = None
    if payload.payment_id:
        payment = await db.game_payments.find_one({"$or": [{"_id": payload.payment_id}, {"id": payload.payment_id}]})

    if not payment and tourn.get("status") in ["REGISTRATION_CLOSED", "CLOSED", "CANCELLED", "COMPLETED"]:
        raise HTTPException(
            status_code=400,
            detail="Registration is closed for this tournament.",
        )

    user_game_id = payload.game_id or payload.in_game_id

    if payment:
        if payment.get("user_id") != current_user.id and current_user.role != "SUPER_ADMIN":
            raise HTTPException(status_code=403, detail="Not authorized to submit UTR for this payment")
        team_id = payment.get("team_id")
        fee_inr = payment.get("amount_inr", tourn.get("entry_fee_inr", 0))
    else:
        team_id = payload.team_id
        fee_inr = tourn.get("entry_fee_inr", 0)
        upi_account = await get_next_active_upi_account(db)
        account_id_str = str(upi_account.get("_id", upi_account.get("id"))) if upi_account else "default"
        account_code_str = upi_account.get("code", "UPI001") if upi_account else "UPI001"
        upi_id_str = upi_account.get("upi_id", "admin@upi") if upi_account else "admin@upi"
        payee_name_str = upi_account.get("payee_name", "Ametheus Esports") if upi_account else "Ametheus Esports"
        fee_paise = int(round(fee_inr * 100))

        payment = GamePayment(
            payment_type="TOURNAMENT_ENTRY",
            user_id=current_user.id,
            game_id=tourn.get("game_id"),
            game_name=tourn.get("title"),
            tournament_id=tid,
            team_id=team_id,
            amount_paise=fee_paise,
            amount_inr=float(fee_inr),
            payment_method="UPI",
            payment_account_id=account_id_str,
            payment_account_code=account_code_str,
            upi_id=upi_id_str,
            payee_name=payee_name_str,
            status="PENDING",
            utr=clean_utr,
        )
        await db.game_payments.insert_one(payment.to_mongo())

    # Create or update TournamentRegistration with PENDING_VERIFICATION
    reg = await db.tournament_registrations.find_one({
        "tournament_id": tid,
        "$or": [{"team_id": team_id} if team_id else {"player_id": current_user.id}]
    })

    if not reg:
        new_reg = TournamentRegistration(
            tournament_id=tid,
            team_id=team_id,
            player_id=current_user.id if not team_id else None,
            game_id=user_game_id,
            in_game_id=user_game_id,
            payment_method="UPI",
            payment_status="PENDING_VERIFICATION",
            payment_id=payment.id if hasattr(payment, "id") else str(payment.get("_id")),
            utr=clean_utr,
            screenshot_url=payload.screenshot_url,
            order_id=f"upi_tourn_{uuid.uuid4().hex[:10]}",
            check_in_status="PENDING",
        )
        await db.tournament_registrations.insert_one(new_reg.to_mongo())
        reg_id = new_reg.id
    else:
        reg_id = str(reg.get("_id", reg.get("id")))
        update_doc = {
            "utr": clean_utr,
            "screenshot_url": payload.screenshot_url,
            "payment_status": "PENDING_VERIFICATION",
            "payment_id": payment.id if hasattr(payment, "id") else str(payment.get("_id")),
        }
        if user_game_id:
            update_doc["game_id"] = user_game_id
            update_doc["in_game_id"] = user_game_id
        await db.tournament_registrations.update_one(
            {"_id": reg["_id"]},
            {"$set": update_doc}
        )

    # Update payment to PENDING with UTR
    p_id = payment.id if hasattr(payment, "id") else str(payment.get("_id"))
    await db.game_payments.update_one(
        {"$or": [{"_id": p_id}, {"id": p_id}]},
        {"$set": {
            "utr": clean_utr,
            "screenshot_url": payload.screenshot_url,
            "status": "PENDING",
            "registration_id": reg_id,
        }}
    )

    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="SUBMIT_TOURNAMENT_UTR",
        target=f"Payment {p_id} / Tourn {id}",
        details=f"Submitted UTR {clean_utr}",
    )

    await create_notification(
        db=db,
        user_id=current_user.id,
        title="Tournament UPI Payment Submitted",
        message=f"UTR {clean_utr} submitted for tournament registration. Slot held pending admin verification.",
        notification_type="WALLET",
        category="WALLET",
        link=f"/tournaments/{id}",
    )

    return {
        "status": "PENDING",
        "message": "Payment UTR submitted successfully. Awaiting admin verification.",
        "payment_id": p_id,
        "utr": clean_utr,
        "payment_status": "PENDING_VERIFICATION",
        "registration_id": reg_id,
    }

@router.patch("/admin/tournaments/{id}/stage-formats")
async def update_tournament_stage_formats(
    id: str,
    payload: TournamentStageFormatsUpdate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Configures tournament_type, match_format, stage_formats, and registration_deadline.
    """
    tourn = await find_tournament_by_id_or_slug(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    update_fields = {}
    if payload.tournament_type:
        update_fields["tournament_type"] = payload.tournament_type
    if payload.match_format:
        update_fields["match_format"] = payload.match_format
    if payload.stage_formats is not None:
        update_fields["stage_formats"] = payload.stage_formats
    if payload.registration_deadline is not None:
        update_fields["registration_deadline"] = payload.registration_deadline

    if update_fields:
        await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": update_fields})

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="UPDATE_TOURNAMENT_STAGE_FORMATS",
        target=f"Tournament {id}",
        details=str(update_fields),
    )

    return {"status": "success", "message": "Tournament stage formats updated successfully", "updated": update_fields}


@router.get("/{id}/history")
async def get_tournament_history_endpoint(
    id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns full stage-by-stage and round-by-round tournament history with scores, winners, and timestamps.
    """
    tourn = await find_tournament_by_id_or_slug(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    t_id = str(tourn.get("id") or tourn.get("_id"))
    return await get_tournament_history(t_id, db)


@router.get("/{id}/teams/{team_id}/journey")
async def get_team_journey_endpoint(
    id: str,
    team_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns dynamic team journey path across all tournament stages and rounds.
    """
    tourn = await find_tournament_by_id_or_slug(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")
    t_id = str(tourn.get("id") or tourn.get("_id"))
    return await get_team_journey(t_id, team_id, db)


@router.get("/{id}/community")
@router.get("/{id}/community-link")
async def get_tournament_community_link(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Participant-Only Community Access:
    Returns the configured community_platform (Discord or WhatsApp) and private community_link.
    Restricted to platform administrators and confirmed (PAID/FREE) registered participants.
    """
    tourn = await find_tournament_by_id_or_slug(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))
    tslug = tourn.get("slug", tid)

    platform = tourn.get("community_platform") or "WHATSAPP"
    whatsapp_url = tourn.get("whatsapp_group_url") or (tourn.get("community_link") if platform == "WHATSAPP" else None)
    discord_url = tourn.get("discord_url") or (tourn.get("community_link") if platform == "DISCORD" or "discord" in str(tourn.get("community_link") or "").lower() else None)
    link = whatsapp_url or discord_url or tourn.get("community_link")
    instructions = tourn.get("participant_instructions") or tourn.get("instructions") or "Custom Lobby Room ID, Password, Bracket calls, Coin toss, and real-time referee communications will be broadcasted exclusively in this channel."

    if not whatsapp_url and not discord_url and not link:
        return {
            "community_platform": platform,
            "community_link": None,
            "whatsapp_group_url": None,
            "discord_url": None,
            "instructions": instructions,
            "tournament_title": tourn.get("title", "Tournament"),
        }

    # Admins and organizers can always view
    if current_user.role in ["SUPER_ADMIN", "ORGANIZER"]:
        return {
            "community_platform": platform,
            "community_link": link,
            "whatsapp_group_url": whatsapp_url,
            "discord_url": discord_url,
            "instructions": instructions,
            "tournament_title": tourn.get("title", "Tournament"),
        }

    # Check tournament assignment for scoped admins
    assign_check = await db.tournament_admin_assignments.find_one({
        "admin_user_id": {"$in": [current_user.id, getattr(current_user, "clerk_user_id", None)]},
        "tournament_id": {"$in": [tid, tslug]},
    })
    if assign_check:
        return {
            "community_platform": platform,
            "community_link": link,
            "whatsapp_group_url": whatsapp_url,
            "discord_url": discord_url,
            "instructions": instructions,
            "tournament_title": tourn.get("title", "Tournament"),
        }

    # Check if current user is a confirmed participant (solo or team member)
    valid_user_ids = [str(current_user.id)]
    if getattr(current_user, "clerk_user_id", None):
        valid_user_ids.append(str(current_user.clerk_user_id))

    user_teams_cursor = db.teams.find({
        "$or": [
            {"captain_id": {"$in": valid_user_ids}},
            {"members.user_id": {"$in": valid_user_ids}},
            {"substitutes.user_id": {"$in": valid_user_ids}},
        ]
    })
    user_teams = await user_teams_cursor.to_list(length=100)
    user_team_ids = [str(t.get("_id", t.get("id"))) for t in user_teams]

    reg_or = [{"player_id": {"$in": valid_user_ids}}]
    if user_team_ids:
        reg_or.append({"team_id": {"$in": user_team_ids}})

    confirmed_reg = await db.tournament_registrations.find_one({
        "tournament_id": {"$in": [tid, tslug]},
        "$or": reg_or,
        "payment_status": {"$in": ["PAID", "FREE", "PENDING_VERIFICATION"]},
        "status": {"$nin": ["CANCELLED", "REFUNDED"]},
    })

    if not confirmed_reg:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Community link is restricted to confirmed participants of this tournament.",
        )

    return {
        "community_platform": platform,
        "community_link": link,
        "whatsapp_group_url": whatsapp_url,
        "discord_url": discord_url,
        "instructions": instructions,
        "tournament_title": tourn.get("title", "Tournament"),
    }


