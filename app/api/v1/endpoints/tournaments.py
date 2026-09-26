import json
import re
import uuid
from typing import List, Optional
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.tournament import Tournament, TournamentRegistration
from app.schemas import TournamentResponse, TournamentRegisterRequest, MatchResponse
from app.services.wallet_service import debit_wallet_funds

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
                status=t.get("status", "REGISTRATION_OPEN"),
                starts_at=t.get("starts_at", "TBD"),
                roster_lock_time=t.get("roster_lock_time", "TBD"),
                server_region=t.get("server_region", "AWS Mumbai (ap-south-1)"),
                registered_teams=reg_count,
                max_teams=t.get("max_teams", 16),
                min_team_size=t.get("min_team_size", 1),
                max_team_size=t.get("max_team_size", 5),
                rank_requirement=t.get("rank_requirement", "Open to All Ranks"),
                format=t.get("format", "Single Elimination Bracket"),
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
        status=t.get("status", "REGISTRATION_OPEN"),
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
        format=t.get("format", "Single Elimination Bracket"),
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

    # 2. Capacity check
    reg_count = await db.tournament_registrations.count_documents({
        "$or": [{"tournament_id": tid}, {"tournament_id": tourn.get("slug")}]
    })

    max_teams = tourn.get("max_teams", 16)
    if reg_count >= max_teams:
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
        })
        if existing_reg:
            raise HTTPException(status_code=400, detail="You are already registered for this tournament")

        wallet_txn_id = None
        if fee_inr > 0:
            fee_paise = int(round(fee_inr * 100))
            wallet_txn_id = f"txn_tourn_{uuid.uuid4().hex[:12]}"
            await debit_wallet_funds(
                db=db,
                user_id=current_user.id,
                amount_paise=fee_paise,
                txn_type="TOURNAMENT_ENTRY",
                reference_id=tid,
                description=f"Solo entry fee: {tourn.get('title', 'Tournament')}",
            )

        reg = TournamentRegistration(
            tournament_id=tid,
            player_id=current_user.id,
            payment_status="PAID" if fee_inr > 0 else "FREE",
            wallet_txn_id=wallet_txn_id,
            order_id=wallet_txn_id or f"reg_{uuid.uuid4().hex[:10]}",
            check_in_status="PENDING",
        )
        await db.tournament_registrations.insert_one(reg.to_mongo())

        from app.services.notification import create_notification
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
    if team.get("game_id") != tourn.get("game_id"):
        raise HTTPException(
            status_code=400,
            detail="Your team is registered for a different competitive title than this tournament",
        )

    # 5b. Roster bounds check (Requirement 2 & 3: Tournament defines its own required team size)
    tourn_min = tourn.get("min_team_size") or tourn.get("required_team_size")
    min_size = int(tourn_min) if tourn_min is not None else 1

    tourn_max = tourn.get("max_team_size")
    max_size = int(tourn_max) if tourn_max is not None else max(min_size, 5)

    members = team.get("members", [])
    members_count = len(members)
    # If the team was created with a captain_id and members list doesn't include the captain, ensure captain counts
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
    })

    if existing_reg:
        raise HTTPException(status_code=400, detail="Team is already registered for this tournament")

    # 7. Debit entry fee from captain wallet if paid tournament (Requirement 24, 25, 40)
    wallet_txn_id = None
    if fee_inr > 0:
        fee_paise = int(round(fee_inr * 100))
        wallet_txn_id = f"txn_tourn_{uuid.uuid4().hex[:12]}"
        await debit_wallet_funds(
            db=db,
            user_id=current_user.id,
            amount_paise=fee_paise,
            txn_type="TOURNAMENT_ENTRY",
            reference_id=tid,
            description=f"Team entry fee: {tourn.get('title', 'Tournament')} ({team.get('name')})",
        )

    # 8. Create registration record
    reg = TournamentRegistration(
        tournament_id=tid,
        team_id=team_id,
        payment_status="PAID" if fee_inr > 0 else "FREE",
        wallet_txn_id=wallet_txn_id,
        order_id=wallet_txn_id or f"reg_{uuid.uuid4().hex[:10]}",
        check_in_status="PENDING",
    )
    await db.tournament_registrations.insert_one(reg.to_mongo())

    from app.services.notification import create_notification
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

