import json
import re
from typing import List, Optional
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, get_current_user
from app.models.user import User
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
            detail=f"Tournament capacity of {max_teams} squads has been reached",
        )

    fee_inr = tourn.get("entry_fee_inr", 0)

    if not payload.team_id:
        # Solo player registration without requiring a team
        existing_reg = await db.tournament_registrations.find_one({
            "tournament_id": tid,
            "player_id": current_user.id,
        })
        if existing_reg:
            raise HTTPException(status_code=400, detail="You are already registered for this tournament")

        reg = TournamentRegistration(
            tournament_id=tid,
            player_id=current_user.id,
            payment_status="PAID" if fee_inr > 0 else "FREE",
            order_id="order_rzp_mock_solo" if fee_inr > 0 else None,
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

    # 3. Team check
    team = await db.teams.find_one({"$or": [{"_id": payload.team_id}, {"id": payload.team_id}]})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    team_id = str(team.get("_id", team.get("id")))

    # 4. Captain authorization
    if team.get("captain_id") != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the verified squad captain can register this team",
        )

    # 5. Game discipline match
    if team.get("game_id") != tourn.get("game_id"):
        raise HTTPException(
            status_code=400,
            detail="Your squad is registered for a different competitive title than this tournament",
        )

    # 6. Duplicate registration check
    existing_reg = await db.tournament_registrations.find_one({
        "tournament_id": tid,
        "team_id": team_id,
    })

    if existing_reg:
        raise HTTPException(status_code=400, detail="Team is already registered for this tournament")

    # 7. Create registration record
    reg = TournamentRegistration(
        tournament_id=tid,
        team_id=team_id,
        payment_status="PAID" if fee_inr > 0 else "FREE",
        order_id="order_rzp_mock_123" if fee_inr > 0 else None,
        check_in_status="PENDING",
    )
    await db.tournament_registrations.insert_one(reg.to_mongo())

    from app.services.notification import create_notification
    await create_notification(
        db=db,
        user_id=current_user.id,
        title="Tournament Registered",
        message=f'Squad "{team["name"]}" is confirmed for "{tourn.get("title", "Tournament")}". Check bracket schedule for match timings.',
        notification_type="TOURNAMENT",
        link=f"/tournaments/{tourn.get('slug', tid)}",
    )

    return {
        "status": "success",
        "message": f'Squad "{team["name"]}" successfully registered for {tourn.get("title", "Tournament")}!',
        "order_id": reg.order_id,
        "entry_fee_inr": fee_inr,
    }

