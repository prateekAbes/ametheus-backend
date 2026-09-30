from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.match import Match
from app.models.dispute import Dispute
from app.schemas import MatchResponse, MatchResultSubmit, DisputeCreate
from app.services.audit import record_audit_log
from app.core.ws import ws_manager

router = APIRouter()

def normalize_match_format(fmt: Optional[str]) -> str:
    if not fmt:
        return "Best of 1"
    f = fmt.strip().lower()
    if f in ["bo1", "best of 1"]:
        return "Best of 1"
    if f in ["bo3", "best of 3", "bo5", "best of 5"]:
        return "Best of 3"
    return "Best of 1"

@router.get("", response_model=List[MatchResponse])
async def list_matches(
    tournament_id: Optional[str] = Query(None, description="Filter matches by tournament ID or slug"),
    status: Optional[str] = Query(None, description="Filter matches by status (e.g. LIVE, UPCOMING, COMPLETED)"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = {}
    if tournament_id:
        tourn = await db.tournaments.find_one({"$or": [{"_id": tournament_id}, {"id": tournament_id}, {"slug": tournament_id}]})
        if tourn:
            tid = str(tourn.get("_id", tourn.get("id")))
            tslug = tourn.get("slug")
            query["$or"] = [{"tournament_id": tid}, {"tournament_id": tslug}]

            # Auto-generate matches if tournament is LIVE or ONGOING with 0 matches and >= 2 registrations
            if tourn.get("status") in ["LIVE", "ONGOING"]:
                matches_count = await db.matches.count_documents({"$or": [{"tournament_id": tid}, {"tournament_id": tslug}]})
                reg_count = await db.tournament_registrations.count_documents({
                    "$or": [{"tournament_id": tid}, {"tournament_id": tslug}],
                    "payment_status": {"$nin": ["REJECTED", "CANCELLED", "REFUNDED"]},
                })
                if matches_count == 0 and reg_count >= 2:
                    from app.services.bracket_engine import build_single_elimination_bracket
                    from app.models.tournament import Tournament
                    tourn_obj = Tournament.from_mongo(tourn)
                    await build_single_elimination_bracket(tournament=tourn_obj, db=db)
        else:
            query["tournament_id"] = tournament_id

    if status:
        stat_upper = status.upper()
        if stat_upper in ["LIVE", "ONGOING"]:
            query["status"] = {"$in": ["LIVE", "IN_PROGRESS", "CHECK_IN_OPEN"]}
        elif stat_upper in ["UPCOMING", "SCHEDULED"]:
            query["status"] = {"$in": ["SCHEDULED", "CHECK_IN_OPEN"]}
        else:
            query["status"] = stat_upper

    cursor = db.matches.find(query)
    matches = await cursor.to_list(length=200)

    response = []
    for m in matches:
        mid = str(m.get("_id", m.get("id")))
        tourn_id = m.get("tournament_id")
        tourn = await db.tournaments.find_one({"$or": [{"_id": tourn_id}, {"id": tourn_id}, {"slug": tourn_id}]}) if tourn_id else None
        
        game = None
        if tourn and tourn.get("game_id"):
            gid = tourn["game_id"]
            game = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]})

        team_a_id = m.get("team_a_id")
        team_b_id = m.get("team_b_id")

        team_a = await db.teams.find_one({"$or": [{"_id": team_a_id}, {"id": team_a_id}]}) if team_a_id else None
        if not team_a and team_a_id:
            user_a = await db.users.find_one({"$or": [{"_id": team_a_id}, {"id": team_a_id}]})
            if user_a:
                team_a = {"name": user_a.get("username") or user_a.get("in_game_name") or "Player A", "tag": "SOLO"}

        team_b = await db.teams.find_one({"$or": [{"_id": team_b_id}, {"id": team_b_id}]}) if team_b_id else None
        if not team_b and team_b_id:
            user_b = await db.users.find_one({"$or": [{"_id": team_b_id}, {"id": team_b_id}]})
            if user_b:
                team_b = {"name": user_b.get("username") or user_b.get("in_game_name") or "Player B", "tag": "SOLO"}

        match_status = m.get("status", "SCHEDULED")
        if tourn and tourn.get("status") in ["LIVE", "ONGOING"] and team_a_id and team_b_id and match_status in ["SCHEDULED", "CHECK_IN_OPEN"]:
            match_status = "LIVE"

        scheduled_at_iso = m.get("scheduled_at").isoformat() if isinstance(m.get("scheduled_at"), datetime) else m.get("scheduled_at")
        response.append(
            MatchResponse(
                id=mid,
                tournament_id=tourn_id or "",
                tournament_title=tourn["title"] if tourn else "Championship",
                game_slug=game["slug"] if game else "valorant",
                game_name=game["name"] if game else "Valorant",
                stage=m.get("stage", "Quarterfinals"),
                match_type=m.get("match_type", "QUARTER_FINAL"),
                format=normalize_match_format(m.get("format") or m.get("match_format") or m.get("series_format")),
                series_format=m.get("series_format") or m.get("format") or "BO1",
                scheduled_time=m.get("scheduled_time", "TBD"),
                scheduled_at=scheduled_at_iso,
                live_link=m.get("youtube_live_url") or m.get("live_link"),
                youtube_live_url=m.get("youtube_live_url") or m.get("live_link"),
                order=m.get("order", 0),
                status=match_status,
                server_region=m.get("server_region", "AWS Mumbai (ap-south-1)"),
                lobby_code=m.get("lobby_code"),
                lobby_password=m.get("lobby_password"),
                team_a={"id": team_a_id, "name": team_a["name"], "tag": team_a.get("tag", "TEAM"), "score": m.get("score_a", 0)} if team_a else None,
                team_b={"id": team_b_id, "name": team_b["name"], "tag": team_b.get("tag", "TEAM"), "score": m.get("score_b", 0)} if team_b else None,
                winner_team_id=m.get("winner_team_id"),
                check_in_status=m.get("check_in_status") or {"team_a": False, "team_b": False},
                evidence_url=m.get("evidence_url"),
                dispute_notes=m.get("dispute_notes"),
            )
        )
    return response

@router.post("/{id}/check-in")
async def captain_check_in(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    match = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    mid = str(match.get("_id", match.get("id")))

    # Verify user is a roster member of either competing team or admin
    team_a_id = match.get("team_a_id")
    team_b_id = match.get("team_b_id")
    competing_teams = [tid for tid in [team_a_id, team_b_id] if tid]

    team_a = await db.teams.find_one({"$or": [{"_id": team_a_id}, {"id": team_a_id}]}) if team_a_id else None
    team_b = await db.teams.find_one({"$or": [{"_id": team_b_id}, {"id": team_b_id}]}) if team_b_id else None

    is_team_a = current_user.id == team_a_id or (team_a and team_a.get("captain_id") == current_user.id) or any((m.get("user_id") if isinstance(m, dict) else getattr(m, "user_id", None)) == current_user.id for m in (team_a.get("members", []) if team_a else []))
    is_team_b = current_user.id == team_b_id or (team_b and team_b.get("captain_id") == current_user.id) or any((m.get("user_id") if isinstance(m, dict) else getattr(m, "user_id", None)) == current_user.id for m in (team_b.get("members", []) if team_b else []))

    if competing_teams and not is_team_a and not is_team_b and current_user.role not in ["SUPER_ADMIN", "ORGANIZER"]:
        raise HTTPException(
            status_code=403,
            detail="You are not registered on either competing roster for this match.",
        )

    check_in_status = dict(match.get("check_in_status") or {"team_a": False, "team_b": False})
    if is_team_a:
        check_in_status["team_a"] = True
    if is_team_b:
        check_in_status["team_b"] = True

    new_status = match.get("status", "SCHEDULED")
    if check_in_status.get("team_a") and check_in_status.get("team_b"):
        new_status = "LIVE"
    elif new_status == "SCHEDULED":
        new_status = "CHECK_IN_OPEN"

    await db.matches.update_one(
        {"_id": match["_id"]},
        {"$set": {"check_in_status": check_in_status, "status": new_status}}
    )

    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="MATCH_CHECK_IN",
        target=f"Match {mid}",
        details=f"Player @{current_user.username} checked in. Match status is {new_status}.",
    )
    return {"status": "success", "message": f"Check-in verified. Match is {new_status}."}

@router.post("/{id}/submit-result")
async def submit_match_result(
    id: str,
    payload: MatchResultSubmit,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    match = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    mid = str(match.get("_id", match.get("id")))
    team_a_id = match.get("team_a_id")
    team_b_id = match.get("team_b_id")
    competing_teams = [tid for tid in [team_a_id, team_b_id] if tid]

    if competing_teams and current_user.role not in ["SUPER_ADMIN", "ORGANIZER"]:
        is_solo = current_user.id in competing_teams
        if not is_solo:
            mem_check = await db.teams.find_one({
                "$and": [
                    {"$or": [{"_id": {"$in": competing_teams}}, {"id": {"$in": competing_teams}}]},
                    {"members.user_id": current_user.id},
                ]
            })
            if not mem_check:
                raise HTTPException(
                    status_code=403,
                    detail="Only participating roster members or platform arbiters can submit scores.",
                )

    await db.matches.update_one(
        {"_id": match["_id"]},
        {"$set": {
            "score_a": payload.score_a,
            "score_b": payload.score_b,
            "evidence_url": payload.evidence_url,
            "captain_notes": payload.captain_notes,
            "status": "PENDING_VERIFICATION",
        }}
    )

    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="SUBMIT_MATCH_RESULT",
        target=f"Match {mid} (Score: {payload.score_a}-{payload.score_b})",
        details=f"Proof: {payload.evidence_url or 'None'} | Notes: {payload.captain_notes or 'None'}",
    )

    await ws_manager.broadcast({
        "type": "MATCH_SCORE_SUBMITTED",
        "match_id": mid,
        "score_a": payload.score_a,
        "score_b": payload.score_b,
        "status": "PENDING_VERIFICATION",
    })

    return {"status": "success", "message": "Match result & proof submitted for admin verification"}

@router.post("/{id}/dispute")
async def create_match_dispute(
    id: str,
    payload: DisputeCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    match = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    mid = str(match.get("_id", match.get("id")))
    tourn_id = match.get("tournament_id")

    dispute = Dispute(
        tournament_id=tourn_id,
        match_id=mid,
        submitted_by=current_user.id,
        reason=payload.reason,
        description=payload.description,
        evidence_url=payload.evidence_url,
        status="OPEN",
    )
    dispute_doc = dispute.to_mongo()
    await db.disputes.insert_one(dispute_doc)

    await db.matches.update_one(
        {"_id": match["_id"]},
        {"$set": {
            "status": "DISPUTED",
            "dispute_notes": payload.description,
        }}
    )

    return {
        "status": "success",
        "message": "Dispute opened and match frozen for administrative review",
        "dispute_id": dispute.id,
    }


@router.get("/{id}")
async def get_match_detail(
    id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from bson import ObjectId
    m_query = {"$or": [{"_id": id}, {"id": id}]}
    if ObjectId.is_valid(id):
        m_query["$or"].append({"_id": ObjectId(id)})

    match = await db.matches.find_one(m_query)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    m = Match.from_mongo(match).model_dump()

    # Inherit tournament stream url if not set on match
    if not m.get("youtube_live_url"):
        tourn_id = m.get("tournament_id")
        if tourn_id:
            t_query = {"$or": [{"id": tourn_id}, {"_id": tourn_id}, {"slug": tourn_id}]}
            if ObjectId.is_valid(tourn_id):
                t_query["$or"].append({"_id": ObjectId(tourn_id)})
            tourn_doc = await db.tournaments.find_one(t_query)
            if tourn_doc and tourn_doc.get("live_stream_url"):
                m["youtube_live_url"] = tourn_doc.get("live_stream_url")
                m["live_link"] = tourn_doc.get("live_stream_url")

    # Strip sensitive join credentials from public view
    m.pop("join_info", None)

    return m


@router.get("/{id}/join-info")
async def get_match_join_info(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Participant-Only Match Join Information Access:
    Returns the game-specific join credentials (join code, party ID, room ID, lobby ID, password, join link, match message).
    Restricted to platform administrators and confirmed participants of either team competing in this match.
    """
    from bson import ObjectId
    m_query = {"$or": [{"_id": id}, {"id": id}]}
    if ObjectId.is_valid(id):
        m_query["$or"].append({"_id": ObjectId(id)})

    match = await db.matches.find_one(m_query)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    enabled = bool(match.get("join_info_enabled", False))
    join_info = match.get("join_info") if enabled else None
    match_msg = match.get("match_message")

    # Admins and organizers can always access
    if current_user.role in ["SUPER_ADMIN", "ORGANIZER"]:
        return {
            "join_info_enabled": enabled,
            "join_info": join_info,
            "match_message": match_msg,
        }

    # Scoped tournament admin assignment check
    tourn_id = match.get("tournament_id")
    if tourn_id:
        assign_check = await db.tournament_admin_assignments.find_one({
            "admin_user_id": current_user.id,
            "tournament_id": tourn_id,
        })
        if assign_check:
            return {
                "join_info_enabled": enabled,
                "join_info": join_info,
                "match_message": match_msg,
            }

    # Verify user is a confirmed participant on either competing team
    team_a_id = match.get("team_a_id")
    team_b_id = match.get("team_b_id")
    team_names = [t for t in [match.get("team_a_name"), match.get("team_b_name"), match.get("team_a_placeholder"), match.get("team_b_placeholder")] if t]
    competing_teams = [t for t in [team_a_id, team_b_id] if t]

    is_participant = False
    # Check solo player match
    if current_user.id in [team_a_id, team_b_id]:
        is_participant = True
    else:
        # Check team rosters by IDs or team names
        team_match = await db.teams.find_one({
            "$and": [
                {"$or": [
                    {"_id": {"$in": competing_teams}},
                    {"id": {"$in": competing_teams}},
                    {"name": {"$in": team_names}},
                ]},
                {"$or": [
                    {"captain_id": current_user.id},
                    {"members.user_id": current_user.id},
                    {"substitutes.user_id": current_user.id},
                ]}
            ]
        })
        if team_match:
            is_participant = True

    # Also check if user has a confirmed registration matching this match
    if not is_participant:
        user_reg = await db.tournament_registrations.find_one({
            "tournament_id": {"$in": [tourn_id, str(match.get("tournament_id"))]},
            "$or": [
                {"player_id": current_user.id},
                {"captain_id": current_user.id},
                {"team_id": {"$in": competing_teams}},
                {"team_name": {"$in": team_names}},
            ],
            "payment_status": {"$in": ["PAID", "FREE"]},
        })
        if user_reg:
            is_participant = True

    if not is_participant:
        raise HTTPException(
            status_code=403,
            detail="Join information is restricted to confirmed match participants.",
        )

    # Check participant has a confirmed registration in this tournament
    reg_check = await db.tournament_registrations.find_one({
        "tournament_id": {"$in": [tourn_id, str(match.get("tournament_id"))]},
        "$or": [
            {"player_id": current_user.id},
            {"captain_id": current_user.id},
            {"team_id": {"$in": competing_teams}},
            {"team_name": {"$in": team_names}},
        ],
        "payment_status": {"$in": ["PAID", "FREE"]},
    })
    if not reg_check and current_user.role not in ["SUPER_ADMIN", "ORGANIZER"]:
        raise HTTPException(
            status_code=403,
            detail="Your tournament registration is not confirmed.",
        )

    return {
        "join_info_enabled": enabled,
        "join_info": join_info,
        "match_message": match_msg,
    }

