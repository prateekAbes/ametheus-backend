import json
import re
import uuid
from typing import List, Optional
from datetime import datetime, timezone
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, get_current_user, require_organizer
from app.models.user import User
from app.models.tournament import Tournament, TournamentRegistration
from app.models.payment import GamePayment
from app.schemas import (
    TournamentResponse,
    TournamentRegisterRequest,
    MatchResponse,
    TournamentRegisterUpiResponse,
    SubmitTournamentUtrRequest,
    TournamentRegistrationStatusResponse,
    TournamentStageFormatsUpdate,
)
from app.services.wallet_service import debit_wallet_funds
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
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    filter_query = {}
    if status:
        filter_query["status"] = status

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
            "$or": [{"tournament_id": tid}, {"tournament_id": t.get("slug")}]
        })

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
        norm_status = "REGISTRATION_CLOSED" if raw_stat in ["LIVE", "ONGOING", "IN_PROGRESS"] else ("COMING_SOON" if raw_stat in ["DRAFT", "UPCOMING"] else raw_stat)

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
                entry_fee=f"₹{fee_inr} / Team" if fee_inr > 0 else "₹0 / Free",
                entry_fee_inr=fee_inr,
                status=norm_status,
                has_live_matches=has_live,
                live_matches_count=len(live_matches_list),
                live_matches=live_matches_list,
                starts_at=t.get("starts_at", "TBD"),
                roster_lock_time=t.get("roster_lock_time", "TBD"),
                server_region=t.get("server_region", "AWS Mumbai (ap-south-1)"),
                registered_teams=reg_count,
                max_teams=t.get("max_teams", 16),
                min_team_size=t.get("min_team_size", 1),
                max_team_size=t.get("max_team_size", 5),
                rank_requirement=t.get("rank_requirement", "Open to All Ranks"),
                format=t.get("format", "Single Elimination"),
                tournament_type=t.get("tournament_type", "Single Elimination"),
                match_format=t.get("match_format", "Single Elimination"),
                series_format=t.get("series_format", "BO1"),
                stage_formats=safe_json_loads(t.get("stage_formats"), {}),
                registration_deadline=t.get("registration_deadline").isoformat() if isinstance(t.get("registration_deadline"), datetime) else t.get("registration_deadline"),
                check_in_window_minutes=t.get("check_in_window_minutes", 15),
                banner_url=t.get("banner_url") or "https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=1200",
                badge_color=t.get("badge_color", "#E56A10"),
                description=t.get("description", ""),
                rules=rules,
                schedule=schedule,
                prizes=prizes,
                bracket_rounds=bracket,
                registered_rosters=[],
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
    # 1. Fetch user's teams
    my_teams_cursor = db.teams.find({
        "$or": [
            {"captain_id": current_user.id},
            {"members.user_id": current_user.id},
            {"substitutes.user_id": current_user.id},
        ],
        "is_dummy": {"$ne": True},
    })
    my_teams = await my_teams_cursor.to_list(length=100)
    my_team_ids = [str(t.get("_id", t.get("id"))) for t in my_teams]

    # 2. Query registrations matching player_id or team_id
    reg_or = [{"player_id": current_user.id}]
    if my_team_ids:
        reg_or.append({"team_id": {"$in": my_team_ids}})

    regs = await db.tournament_registrations.find({"$or": reg_or}).to_list(length=100)

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

@router.get("/{slug}", response_model=TournamentResponse)
async def get_tournament_by_slug(slug: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    t = await find_tournament_by_id_or_slug(db, slug)
    if not t:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(t.get("_id", t.get("id")))
    gid = t.get("game_id")
    game_obj = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]})

    reg_count = await db.tournament_registrations.count_documents({
        "$or": [{"tournament_id": tid}, {"tournament_id": t.get("slug")}]
    })

    rules = safe_json_loads(t.get("rules", t.get("rules_json")), [])
    schedule = safe_json_loads(t.get("schedule", t.get("schedule_json")), [])
    prizes = safe_json_loads(t.get("prizes", t.get("prizes_json")), [])
    bracket = safe_json_loads(t.get("bracket_rounds", t.get("bracket_json")), [])

    prize_inr = t.get("prize_pool_inr") or 0
    fee_inr = t.get("entry_fee_inr") or 0

    # Auto-derive min/max team size for solo titles (e.g. Chess)
    is_solo_game = bool(game_obj and (not game_obj.get("is_team_game", True) or game_obj.get("slug") == "chess"))
    min_team_size = 1 if is_solo_game else t.get("min_team_size", 1)
    max_team_size = 1 if is_solo_game else t.get("max_team_size", 5)

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
                team_a_doc = await db.teams.find_one({"$or": [{"_id": m.get("team_a_id")}, {"id": m.get("team_a_id")}]}) if m.get("team_a_id") else None
                team_b_doc = await db.teams.find_one({"$or": [{"_id": m.get("team_b_id")}, {"id": m.get("team_b_id")}]}) if m.get("team_b_id") else None
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
        "$or": [{"tournament_id": tid}, {"tournament_id": t.get("slug")}]
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
    norm_status = "REGISTRATION_CLOSED" if raw_stat in ["LIVE", "ONGOING", "IN_PROGRESS"] else ("COMING_SOON" if raw_stat in ["DRAFT", "UPCOMING"] else raw_stat)

    return TournamentResponse(
        id=tid,
        slug=t.get("slug", tid),
        organizer_id=t.get("organizer_id"),
        title=t.get("title", "Championship Tournament"),
        game_slug=game_obj["slug"] if game_obj else "valorant",
        game_name=game_obj["name"] if game_obj else "Valorant",
        prize_pool=f"₹{prize_inr:,}",
        prize_pool_inr=prize_inr,
        entry_fee=f"₹{fee_inr} / Team" if fee_inr > 0 else "₹0 / Free",
        entry_fee_inr=fee_inr,
        status=norm_status,
        has_live_matches=has_live,
        live_matches_count=len(live_matches_list),
        live_matches=live_matches_list,
        expected_start_date=t.get("expected_start_date"),
        registration_open_date=t.get("registration_open_date"),
        announcement_text=t.get("announcement_text"),
        starts_at=t.get("starts_at", "TBD"),
        roster_lock_time=t.get("roster_lock_time", "TBD"),
        server_region=t.get("server_region", "AWS Mumbai (ap-south-1)"),
        registered_teams=reg_count,
        max_teams=t.get("max_teams", 16),
        min_team_size=min_team_size,
        max_team_size=max_team_size,
        rank_requirement=t.get("rank_requirement", "Open to All Ranks"),
        format=t.get("format", "Single Elimination"),
        tournament_type=t.get("tournament_type", "Single Elimination"),
        match_format=t.get("match_format", "Single Elimination"),
        series_format=t.get("series_format", "BO1"),
        stage_formats=safe_json_loads(t.get("stage_formats"), {}),
        registration_deadline=t.get("registration_deadline").isoformat() if isinstance(t.get("registration_deadline"), datetime) else t.get("registration_deadline"),
        check_in_window_minutes=t.get("check_in_window_minutes", 15),
        banner_url=t.get("banner_url") or "https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=1200",
        badge_color=t.get("badge_color", "#E56A10"),
        description=t.get("description", ""),
        rules=rules,
        schedule=schedule,
        prizes=prizes,
        bracket_rounds=bracket,
        registered_rosters=registered_rosters,
    )

@router.get("/{slug}/matches", response_model=List[MatchResponse])
async def get_tournament_matches(slug: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    t = await find_tournament_by_id_or_slug(db, slug)
    if not t:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(t.get("_id", t.get("id")))
    t_slug = t.get("slug", tid)
    matches = await db.matches.find({
        "$or": [{"tournament_id": tid}, {"tournament_id": t_slug}]
    }).to_list(length=100)

    gid = t.get("game_id")
    game = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]})

    response = []
    for m in matches:
        mid = str(m.get("_id", m.get("id")))
        team_a_id = m.get("team_a_id")
        team_b_id = m.get("team_b_id")
        team_a = await db.teams.find_one({"$or": [{"_id": team_a_id}, {"id": team_a_id}]}) if team_a_id else None
        team_b = await db.teams.find_one({"$or": [{"_id": team_b_id}, {"id": team_b_id}]}) if team_b_id else None

        response.append(
            MatchResponse(
                id=mid,
                tournament_id=tid,
                tournament_title=t.get("title", "Tournament"),
                game_slug=game["slug"] if game else "valorant",
                game_name=game["name"] if game else "Valorant",
                stage=m.get("stage", "QUARTER_FINAL"),
                format=m.get("format", "BO1"),
                scheduled_time=m.get("scheduled_time", "TBD"),
                status=m.get("status", "SCHEDULED"),
                server_region=m.get("server_region", t.get("server_region", "AWS Mumbai (ap-south-1)")),
                lobby_code=m.get("lobby_code"),
                lobby_password=m.get("lobby_password"),
                team_a={"name": team_a["name"], "tag": team_a["tag"], "score": m.get("score_a", 0)} if team_a else None,
                team_b={"name": team_b["name"], "tag": team_b["tag"], "score": m.get("score_b", 0)} if team_b else None,
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
        "payment_status": {"$ne": "REJECTED"}
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
    is_solo_game = bool(game_obj and (not game_obj.get("is_team_game", True) or game_obj.get("slug") == "chess"))

    if not payload.team_id:
        # Solo player registration without requiring a team
        existing_reg = await db.tournament_registrations.find_one({
            "tournament_id": tid,
            "player_id": current_user.id,
            "payment_status": {"$ne": "REJECTED"}
        })
        if existing_reg:
            raise HTTPException(status_code=400, detail="You are already registered for this tournament")

        # 1. Insert registration record first (PENDING if paid, FREE if free)
        reg = TournamentRegistration(
            tournament_id=tid,
            player_id=current_user.id,
            payment_method="WALLET" if fee_inr > 0 else "FREE",
            payment_status="PENDING_WALLET" if fee_inr > 0 else "FREE",
            order_id=f"reg_{uuid.uuid4().hex[:10]}",
            check_in_status="PENDING",
        )
        try:
            await db.tournament_registrations.insert_one(reg.to_mongo())
        except DuplicateKeyError:
            raise HTTPException(status_code=400, detail="You are already registered for this tournament")

        # 2. Debit wallet funds if paid
        wallet_txn_id = None
        if fee_inr > 0:
            fee_paise = int(round(fee_inr * 100))
            wallet_txn_id = f"txn_tourn_{uuid.uuid4().hex[:12]}"
            try:
                await debit_wallet_funds(
                    db=db,
                    user_id=current_user.id,
                    amount_paise=fee_paise,
                    txn_type="TOURNAMENT_ENTRY",
                    reference_id=reg.id,
                    description=f"Solo entry fee: {tourn.get('title', 'Tournament')}",
                )
                await db.tournament_registrations.update_one(
                    {"_id": reg.id},
                    {"$set": {"payment_status": "PAID", "wallet_txn_id": wallet_txn_id, "order_id": wallet_txn_id}}
                )
                reg.payment_status = "PAID"
                reg.wallet_txn_id = wallet_txn_id
                reg.order_id = wallet_txn_id
            except Exception as e:
                # Rollback registration if payment fails
                await db.tournament_registrations.delete_one({"_id": reg.id})
                raise e

        if reg_count + 1 >= max_teams:
            await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": {"status": "REGISTRATION_CLOSED"}})

        await create_notification(
            db=db,
            user_id=current_user.id,
            title="Tournament Registered",
            message=f'You are confirmed as a solo contender for "{tourn.get("title", "Tournament")}". Check bracket schedule for match timings.',
            notification_type="TOURNAMENT",
            link=f"/tournaments/{tourn.get('slug', tid)}",
        )

        return {
            "status": "success",
            "message": f'Successfully registered as a solo contender for {tourn.get("title", "Tournament")}!',
            "order_id": reg.order_id,
            "entry_fee_inr": fee_inr,
        }

    # If game is solo, multi-player teams are strictly prohibited
    if is_solo_game:
        raise HTTPException(
            status_code=400,
            detail=f"{game_obj.get('name', 'Chess')} is a solo 1v1 discipline. Team registration is not permitted. Please register as an individual contender.",
        )

    # 3. Team check
    team = await db.teams.find_one({"$or": [{"_id": payload.team_id}, {"id": payload.team_id}]})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    team_id = str(team.get("_id", team.get("id")))

    # 4. Captain authorization
    if team.get("captain_id") != current_user.id:
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
    min_size = int(tourn_min) if tourn_min is not None else 1

    tourn_max = tourn.get("max_team_size")
    max_size = int(tourn_max) if tourn_max is not None else max(min_size, 5)

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
        "payment_status": {"$ne": "REJECTED"}
    })

    if existing_reg:
        raise HTTPException(status_code=400, detail="Team is already registered for this tournament")

    # 7. Create registration record first (PENDING if paid, FREE if free)
    reg = TournamentRegistration(
        tournament_id=tid,
        team_id=team_id,
        payment_method="WALLET" if fee_inr > 0 else "FREE",
        payment_status="PENDING_WALLET" if fee_inr > 0 else "FREE",
        order_id=f"reg_{uuid.uuid4().hex[:10]}",
        check_in_status="PENDING",
    )
    try:
        await db.tournament_registrations.insert_one(reg.to_mongo())
    except DuplicateKeyError:
        raise HTTPException(status_code=400, detail="Team is already registered for this tournament")

    # 8. Debit entry fee from captain wallet if paid tournament
    wallet_txn_id = None
    if fee_inr > 0:
        fee_paise = int(round(fee_inr * 100))
        wallet_txn_id = f"txn_tourn_{uuid.uuid4().hex[:12]}"
        try:
            await debit_wallet_funds(
                db=db,
                user_id=current_user.id,
                amount_paise=fee_paise,
                txn_type="TOURNAMENT_ENTRY",
                reference_id=reg.id,
                description=f"Team entry fee: {tourn.get('title', 'Tournament')} ({team.get('name')})",
            )
            await db.tournament_registrations.update_one(
                {"_id": reg.id},
                {"$set": {"payment_status": "PAID", "wallet_txn_id": wallet_txn_id, "order_id": wallet_txn_id}}
            )
            reg.payment_status = "PAID"
            reg.wallet_txn_id = wallet_txn_id
            reg.order_id = wallet_txn_id
        except Exception as e:
            # Rollback registration if payment fails
            await db.tournament_registrations.delete_one({"_id": reg.id})
            raise e

    if reg_count + 1 >= max_teams:
        await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": {"status": "REGISTRATION_CLOSED"}})

    await create_notification(
        db=db,
        user_id=current_user.id,
        title="Tournament Registered",
        message=f'Team "{team["name"]}" is confirmed for "{tourn.get("title", "Tournament")}". Check bracket schedule for match timings.',
        notification_type="TOURNAMENT",
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
    Returns whether current user (or user's teams) is currently registered for the tournament.
    """
    tourn = await find_tournament_by_id_or_slug(db, id)
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    tid = str(tourn.get("_id", tourn.get("id")))

    # 1. Check solo registration
    solo_reg = await db.tournament_registrations.find_one({
        "tournament_id": tid,
        "player_id": current_user.id,
        "payment_status": {"$ne": "REJECTED"}
    })
    if solo_reg:
        solo_reg["id"] = str(solo_reg.get("_id", solo_reg.get("id")))
        solo_reg.pop("_id", None)
        return TournamentRegistrationStatusResponse(is_registered=True, registration=solo_reg)

    # 2. Check teams where user is captain or member
    user_teams_cursor = db.teams.find({
        "$or": [
            {"captain_id": current_user.id},
            {"members.user_id": current_user.id},
        ]
    })
    user_teams = await user_teams_cursor.to_list(length=50)
    team_ids = [str(t.get("_id", t.get("id"))) for t in user_teams]

    if team_ids:
        team_reg = await db.tournament_registrations.find_one({
            "tournament_id": tid,
            "team_id": {"$in": team_ids},
            "payment_status": {"$ne": "REJECTED"}
        })
        if team_reg:
            team_reg["id"] = str(team_reg.get("_id", team_reg.get("id")))
            team_reg.pop("_id", None)
            return TournamentRegistrationStatusResponse(is_registered=True, registration=team_reg)

    return TournamentRegistrationStatusResponse(is_registered=False, registration=None)

@router.post("/{id}/register-upi", response_model=TournamentRegisterUpiResponse)
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
    4. Creates TournamentRegistration with PENDING_VERIFICATION.
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
        "payment_status": {"$ne": "REJECTED"}
    })
    max_teams = tourn.get("max_teams", 16)
    if reg_count >= max_teams:
        await db.tournaments.update_one({"_id": tourn["_id"]}, {"$set": {"status": "REGISTRATION_CLOSED"}})
        raise HTTPException(status_code=400, detail=f"Tournament capacity of {max_teams} teams has been reached")

    fee_inr = tourn.get("entry_fee_inr", 0)
    fee_paise = int(round(fee_inr * 100))

    gid = tourn.get("game_id")
    game_obj = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]})
    is_solo_game = bool(game_obj and (not game_obj.get("is_team_game", True) or game_obj.get("slug") == "chess"))

    team_id = None
    team_name = None

    if payload.team_id:
        if is_solo_game:
            raise HTTPException(status_code=400, detail=f"{game_obj.get('name', 'Chess')} is a solo 1v1 discipline.")
        team = await db.teams.find_one({"$or": [{"_id": payload.team_id}, {"id": payload.team_id}]})
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        if team.get("captain_id") != current_user.id:
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

        existing_reg = await db.tournament_registrations.find_one({
            "tournament_id": tid,
            "team_id": team_id,
            "payment_status": {"$ne": "REJECTED"}
        })
        if existing_reg:
            raise HTTPException(status_code=400, detail="Team is already registered for this tournament")
    else:
        existing_reg = await db.tournament_registrations.find_one({
            "tournament_id": tid,
            "player_id": current_user.id,
            "payment_status": {"$ne": "REJECTED"}
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

    reg = TournamentRegistration(
        tournament_id=tid,
        team_id=team_id,
        player_id=current_user.id if not team_id else None,
        payment_method="UPI",
        payment_status="PENDING_VERIFICATION",
        check_in_status="PENDING",
        order_id=f"upi_tourn_{uuid.uuid4().hex[:10]}",
    )
    try:
        await db.tournament_registrations.insert_one(reg.to_mongo())
    except DuplicateKeyError:
        raise HTTPException(status_code=400, detail="Already registered for this tournament")

    payment = GamePayment(
        payment_type="TOURNAMENT_ENTRY",
        user_id=current_user.id,
        game_id=tourn.get("game_id"),
        game_name=tourn.get("title"),
        tournament_id=tid,
        registration_id=reg.id,
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

    # Link payment to registration
    await db.tournament_registrations.update_one(
        {"_id": reg.id},
        {"$set": {"payment_id": payment.id}}
    )

    return TournamentRegisterUpiResponse(
        payment_id=payment.id,
        registration_id=reg.id,
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

@router.post("/{id}/submit-upi-utr")
async def submit_tournament_upi_utr(
    id: str,
    payload: SubmitTournamentUtrRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Submits 12-digit UTR reference for direct UPI tournament registration.
    """
    payment = await db.game_payments.find_one({"$or": [{"_id": payload.payment_id}, {"id": payload.payment_id}]})
    if not payment:
        raise HTTPException(status_code=404, detail="Payment record not found")

    if payment.get("user_id") != current_user.id and current_user.role != "SUPER_ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized to submit UTR for this payment")

    clean_utr = payload.utr.strip()
    if len(clean_utr) < 6:
        raise HTTPException(status_code=400, detail="Invalid UPI Transaction ID / UTR. Must be at least 6 characters.")

    existing_utr = await db.game_payments.find_one({
        "utr": clean_utr,
        "$or": [{"_id": {"$ne": payment["_id"]}}, {"id": {"$ne": payment.get("id")}}]
    })
    if existing_utr:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This UPI Transaction ID / UTR has already been submitted for another payment.",
        )

    try:
        await db.game_payments.update_one(
            {"_id": payment["_id"]},
            {"$set": {
                "utr": clean_utr,
                "screenshot_url": payload.screenshot_url,
                "status": "PENDING",
            }}
        )
    except DuplicateKeyError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This UPI Transaction ID / UTR has already been submitted for another payment.",
        )

    reg_id = payment.get("registration_id")
    if reg_id:
        await db.tournament_registrations.update_one(
            {"$or": [{"_id": reg_id}, {"id": reg_id}]},
            {"$set": {"utr": clean_utr, "payment_status": "PENDING_VERIFICATION"}}
        )

    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="SUBMIT_TOURNAMENT_UTR",
        target=f"Payment {payment.get('id')} / Tourn {id}",
        details=f"Submitted UTR {clean_utr}",
    )

    await create_notification(
        db=db,
        user_id=current_user.id,
        title="Tournament UPI Payment Submitted",
        message=f"UTR {clean_utr} submitted for tournament registration. Slot held pending admin verification.",
        notification_type="PAYMENT",
        link=f"/tournaments/{id}",
    )

    return {
        "status": "PENDING",
        "message": "Payment UTR submitted successfully. Awaiting admin verification.",
        "payment_id": payload.payment_id,
        "utr": clean_utr,
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


