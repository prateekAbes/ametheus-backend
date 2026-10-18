from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_db, get_current_user, get_current_user_optional, require_organizer
from app.models.user import User
from app.models.match import Match
from app.models.dispute import Dispute
from app.schemas import MatchResponse, MatchResultSubmit, DisputeCreate, BattleRoyaleSubmitResultsPayload
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
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Architecture: Single Live Match Card Per Tournament.
    - When tournament_id is NOT provided: Returns exactly ONE Live Match card per tournament from db.live_matches.
      The card dynamically tracks the active encounter (Match 1 -> Match 2 -> Grand Final or BR rounds).
    - When tournament_id IS provided: Returns all individual encounters from db.matches for tournament brackets,
      round history, and drill-down navigation without deleting previous results.
    """
    is_admin_or_organizer = bool(current_user and current_user.role in ["SUPER_ADMIN", "ADMIN", "ORGANIZER"])

    # 1. Single Live Match per Tournament (Live Matches Public View)
    if not tournament_id:
        active_tournaments_with_matches = await db.matches.distinct("tournament_id", {
            "status": {"$nin": ["CANCELLED", "DELETED", "DRAFT"]},
            "is_deleted": {"$ne": True},
            "is_dummy": {"$ne": True},
            "roster_status": {"$ne": "DRAFT"},
        })
        lm_count = await db.live_matches.count_documents({
            "current_encounter_id": {"$ne": None, "$exists": True}
        })
        if lm_count < len(active_tournaments_with_matches):
            from app.services.live_match_service import reconcile_all_live_matches
            await reconcile_all_live_matches(db)

        lm_query = {
            "current_encounter_id": {"$ne": None, "$exists": True},
        }
        if status:
            stat_upper = status.upper()
            if stat_upper in ["LIVE", "ONGOING"]:
                lm_query["status"] = {"$in": ["LIVE", "IN_PROGRESS", "CHECK_IN_OPEN"]}
            elif stat_upper in ["UPCOMING", "SCHEDULED"]:
                lm_query["status"] = {"$in": ["SCHEDULED", "CHECK_IN_OPEN", "UPCOMING", "PENDING"]}
            elif stat_upper in ["COMPLETED", "FINISHED"]:
                lm_query["status"] = {"$in": ["COMPLETED", "FINISHED"]}
            else:
                lm_query["status"] = stat_upper

        cursor = db.live_matches.find(lm_query)
        live_matches = await cursor.to_list(length=200)

        response = []
        for lm in live_matches:
            enc_id = lm.get("current_encounter_id")
            if not enc_id:
                continue

            from bson import ObjectId
            enc_q = [{"_id": enc_id}, {"id": enc_id}]
            if ObjectId.is_valid(enc_id):
                enc_q.append({"_id": ObjectId(enc_id)})

            real_match = await db.matches.find_one({
                "$or": enc_q,
                "status": {"$nin": ["CANCELLED", "DELETED"]},
                "is_deleted": {"$ne": True},
                "is_dummy": {"$ne": True},
            })
            if not real_match:
                # Purge stale live match
                await db.live_matches.delete_one({"_id": lm["_id"]})
                continue

            real_mid = str(real_match.get("_id", real_match.get("id")))
            tourn_id_val = str(lm.get("tournament_id") or real_match.get("tournament_id") or "")
            match_status = lm.get("status") or real_match.get("status", "SCHEDULED")
            scheduled_at_val = real_match.get("scheduled_at") or lm.get("scheduled_at")
            scheduled_at_iso = scheduled_at_val.isoformat() if isinstance(scheduled_at_val, datetime) else scheduled_at_val
            scheduled_time_val = real_match.get("scheduled_time") or lm.get("scheduled_time", "TBD")
            credentials_released = bool(real_match.get("credentials_released", lm.get("credentials_released", False)))

            if is_admin_or_organizer:
                lobby_code = real_match.get("lobby_code") or real_match.get("room_id") or lm.get("lobby_code")
                lobby_password = real_match.get("lobby_password") or real_match.get("room_password") or lm.get("lobby_password")
                room_id = real_match.get("room_id") or real_match.get("lobby_code") or lm.get("room_id")
                room_password = real_match.get("room_password") or real_match.get("lobby_password") or lm.get("room_password")
                custom_instructions = real_match.get("custom_instructions") or lm.get("custom_instructions")
            else:
                lobby_code = None
                lobby_password = None
                room_id = None
                room_password = None
                custom_instructions = None

            enc_history = lm.get("encounter_history") or []

            response.append(
                MatchResponse(
                    id=real_mid,
                    tournament_id=tourn_id_val,
                    tournament_title=lm.get("tournament_title", "Championship"),
                    game_slug=lm.get("game_slug", "valorant"),
                    game_name=lm.get("game_name", "Valorant"),
                    stage=lm.get("current_stage") or lm.get("current_round_name") or real_match.get("stage") or "Round 1",
                    match_type="BATTLE_ROYALE" if (lm.get("is_battle_royale") or real_match.get("match_type") == "BATTLE_ROYALE") else "MATCH",
                    format=normalize_match_format(real_match.get("format") or lm.get("format")),
                    series_format=real_match.get("series_format") or lm.get("series_format", "BO1"),
                    scheduled_time=scheduled_time_val,
                    scheduled_at=scheduled_at_iso,
                    live_link=real_match.get("youtube_live_url") or real_match.get("live_link") or lm.get("youtube_live_url") or lm.get("live_link"),
                    youtube_live_url=real_match.get("youtube_live_url") or real_match.get("live_link") or lm.get("youtube_live_url") or lm.get("live_link"),
                    order=lm.get("current_encounter_number", 1),
                    status=match_status,
                    server_region=real_match.get("server_region") or lm.get("server_region", "AWS Mumbai (ap-south-1)"),
                    lobby_code=lobby_code,
                    lobby_password=lobby_password,
                    room_id=room_id,
                    room_password=room_password,
                    custom_instructions=custom_instructions,
                    credentials_released=credentials_released,
                    teams=lm.get("teams", real_match.get("teams", [])),
                    team_ids=lm.get("team_ids", real_match.get("team_ids", [])),
                    placements=lm.get("placements", real_match.get("placements", [])),
                    qualifier_team_ids=lm.get("qualifier_team_ids", real_match.get("qualifier_team_ids", [])),
                    qualifiers_count=lm.get("qualifiers_count", real_match.get("qualifiers_count")),
                    is_final_round=bool(lm.get("is_final_round", real_match.get("is_final_round", False))),
                    team_a=lm.get("team_a"),
                    team_b=lm.get("team_b"),
                    winner_team_id=real_match.get("winner_team_id") or lm.get("winner_team_id"),
                    check_in_status=real_match.get("check_in_status") or lm.get("check_in_status") or {},
                    current_encounter_id=real_mid,
                    current_encounter_number=lm.get("current_encounter_number", 1),
                    current_encounter_title=lm.get("current_encounter_title", "Match 1"),
                    current_round_name=lm.get("current_round_name", "Round 1"),
                    current_round_number=lm.get("current_round_number", 1),
                    encounter_history=enc_history,
                )
            )
        return response

    # 2. Specific Tournament Encounters (Round Management & Bracket Drill-Down)
    query = {"roster_status": {"$ne": "DRAFT"}}
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
        t_doc = tourn or (await db.tournaments.find_one({"$or": [{"_id": tourn_id}, {"id": tourn_id}, {"slug": tourn_id}]}) if tourn_id else None)
        
        game = None
        if t_doc and t_doc.get("game_id"):
            gid = t_doc["game_id"]
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
        scheduled_at_iso = m.get("scheduled_at").isoformat() if isinstance(m.get("scheduled_at"), datetime) else m.get("scheduled_at")
        credentials_released = bool(m.get("credentials_released", False))

        if is_admin_or_organizer:
            lobby_code = m.get("lobby_code") or m.get("room_id")
            lobby_password = m.get("lobby_password") or m.get("room_password")
            room_id = m.get("room_id") or m.get("lobby_code")
            room_password = m.get("room_password") or m.get("lobby_password")
            custom_instructions = m.get("custom_instructions") or m.get("match_message")
        else:
            # Public match listing must never leak private room credentials
            lobby_code = None
            lobby_password = None
            room_id = None
            room_password = None
            custom_instructions = None

        m_num = int(m.get("match_number") or m.get("order") or 1)
        r_name = m.get("round_name") or m.get("stage") or "Round 1"
        r_num = int(m.get("round_number") or 1)

        response.append(
            MatchResponse(
                id=mid,
                tournament_id=tourn_id or "",
                tournament_title=t_doc["title"] if t_doc else "Championship",
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
                next_match_id=m.get("next_match_id"),
                team_a={"id": team_a_id, "name": team_a["name"], "tag": team_a.get("tag", "TEAM"), "score": m.get("score_a", 0)} if team_a else None,
                team_b={"id": team_b_id, "name": team_b["name"], "tag": team_b.get("tag", "TEAM"), "score": m.get("score_b", 0)} if team_b else None,
                winner_team_id=m.get("winner_team_id"),
                check_in_status=m.get("check_in_status") or {"team_a": False, "team_b": False},
                evidence_url=m.get("evidence_url"),
                dispute_notes=m.get("dispute_notes"),
                current_encounter_id=mid,
                current_encounter_number=m_num,
                current_encounter_title=m.get("stage") or f"Match {m_num}",
                current_round_name=r_name,
                current_round_number=r_num,
                encounter_history=[],
            )
        )
    return response

@router.post("/{id}/check-in")
async def captain_check_in(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from bson import ObjectId
    m_query = [{"_id": id}, {"id": id}]
    if ObjectId.is_valid(id):
        m_query.append({"_id": ObjectId(id)})
    match = await db.matches.find_one({"$or": m_query})
    if not match:
        live_doc = await db.live_matches.find_one({"$or": [{"_id": id}, {"id": id}, {"tournament_id": id}]})
        if live_doc and live_doc.get("current_encounter_id"):
            enc_id = live_doc["current_encounter_id"]
            enc_query = [{"_id": enc_id}, {"id": enc_id}]
            if ObjectId.is_valid(enc_id):
                enc_query.append({"_id": ObjectId(enc_id)})
            match = await db.matches.find_one({"$or": enc_query})
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    mid = str(match.get("_id", match.get("id")))

    # Verify user is a roster member of any competing team, solo participant, or admin
    team_a_id = match.get("team_a_id")
    team_b_id = match.get("team_b_id")
    br_teams = match.get("teams") or []
    br_team_ids = [str(t.get("id") or t.get("_id")) for t in br_teams if isinstance(t, dict)]
    competing_teams = list(set([tid for tid in [team_a_id, team_b_id] + br_team_ids if tid]))

    checked_in_team_id = None
    is_authorized = current_user.role in ["SUPER_ADMIN", "ADMIN", "ORGANIZER"]

    if not is_authorized:
        # Check team_a
        team_a = await db.teams.find_one({"$or": [{"_id": team_a_id}, {"id": team_a_id}]}) if team_a_id else None
        if current_user.id == team_a_id or (team_a and (team_a.get("captain_id") == current_user.id or any((m.get("user_id") if isinstance(m, dict) else getattr(m, "user_id", None)) == current_user.id for m in team_a.get("members", [])))):
            checked_in_team_id = team_a_id or "team_a"
            is_authorized = True

        # Check team_b
        if not is_authorized:
            team_b = await db.teams.find_one({"$or": [{"_id": team_b_id}, {"id": team_b_id}]}) if team_b_id else None
            if current_user.id == team_b_id or (team_b and (team_b.get("captain_id") == current_user.id or any((m.get("user_id") if isinstance(m, dict) else getattr(m, "user_id", None)) == current_user.id for m in team_b.get("members", [])))):
                checked_in_team_id = team_b_id or "team_b"
                is_authorized = True

        # Check BR teams
        if not is_authorized and competing_teams:
            valid_user_ids = [str(current_user.id)]
            if getattr(current_user, "clerk_user_id", None):
                valid_user_ids.append(str(current_user.clerk_user_id))

            for uid in valid_user_ids:
                if uid in competing_teams:
                    checked_in_team_id = uid
                    is_authorized = True
                    break

            if not is_authorized:
                matching_team = await db.teams.find_one({
                    "$and": [
                        {"$or": [{"_id": {"$in": competing_teams}}, {"id": {"$in": competing_teams}}]},
                        {"$or": [
                            {"captain_id": {"$in": valid_user_ids}},
                            {"members.user_id": {"$in": valid_user_ids}},
                        ]}
                    ]
                })
                if matching_team:
                    checked_in_team_id = str(matching_team.get("_id", matching_team.get("id")))
                    is_authorized = True

        # Check solo registrations
        if not is_authorized:
            valid_user_ids = [str(current_user.id)]
            if getattr(current_user, "clerk_user_id", None):
                valid_user_ids.append(str(current_user.clerk_user_id))
            tid = str(match.get("tournament_id") or "")
            reg_doc = await db.tournament_registrations.find_one({
                "$or": [{"tournament_id": tid}, {"tournament_id": match.get("tournament_slug", tid)}],
                "$or": [{"player_id": {"$in": valid_user_ids}}, {"user_id": {"$in": valid_user_ids}}],
                "payment_status": {"$in": ["PAID", "FREE"]},
            })
            if reg_doc:
                checked_in_team_id = str(reg_doc.get("team_id") or reg_doc.get("player_id") or current_user.id)
                is_authorized = True

    if not is_authorized:
        raise HTTPException(
            status_code=403,
            detail="You are not registered on any competing roster for this match.",
        )

    check_in_status = dict(match.get("check_in_status") or {})
    check_ins = list(match.get("check_ins") or [])

    # Prevent duplicate check-in records
    already_checked_in = any(
        isinstance(c, dict) and (c.get("user_id") == current_user.id or (checked_in_team_id and c.get("team_id") == checked_in_team_id))
        for c in check_ins
    )
    if already_checked_in:
        return {
            "status": "already_checked_in",
            "message": "Check-in already completed for this roster.",
            "check_in_status": check_in_status,
        }

    if checked_in_team_id == team_a_id or checked_in_team_id == "team_a":
        check_in_status["team_a"] = True
        if team_a_id:
            check_in_status[team_a_id] = True
    elif checked_in_team_id == team_b_id or checked_in_team_id == "team_b":
        check_in_status["team_b"] = True
        if team_b_id:
            check_in_status[team_b_id] = True
    elif checked_in_team_id:
        check_in_status[checked_in_team_id] = True

    # Record detailed check_ins entry with timestamp
    check_ins.append({
        "user_id": current_user.id,
        "team_id": checked_in_team_id,
        "username": current_user.username,
        "checked_in_at": datetime.now(timezone.utc).isoformat(),
    })

    # NOTE: Check-in system is removed. This endpoint now only records participation
    # confirmation without changing match status. Status remains SCHEDULED.
    current_status = match.get("status", "SCHEDULED")

    await db.matches.update_one(
        {"_id": match["_id"]},
        {"$set": {
            "check_in_status": check_in_status,
            "check_ins": check_ins,
            # Status deliberately NOT changed — keep existing status.
        }}
    )

    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="MATCH_CHECK_IN",
        target=f"Match {mid}",
        details=f"Player @{current_user.username} confirmed participation. Match status: {current_status}.",
    )
    try:
        from app.services.live_match_service import sync_live_match_for_tournament
        await sync_live_match_for_tournament(str(match.get("tournament_id")), db)
    except Exception:
        pass

    return {"status": "success", "message": "Participation confirmed.", "check_in_status": check_in_status}

@router.post("/{id}/submit-result")
async def submit_match_result(
    id: str,
    payload: MatchResultSubmit,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    # Rule 8: Reject non-organizer score submissions with HTTP 403
    if current_user.role not in ["SUPER_ADMIN", "ADMIN", "ORGANIZER"]:
        raise HTTPException(
            status_code=403,
            detail="Only tournament organizers or administrators are authorized to submit match results.",
        )

    from bson import ObjectId
    m_query = [{"_id": id}, {"id": id}]
    if ObjectId.is_valid(id):
        m_query.append({"_id": ObjectId(id)})
    match = await db.matches.find_one({"$or": m_query})
    if not match:
        live_doc = await db.live_matches.find_one({"$or": [{"_id": id}, {"id": id}, {"tournament_id": id}]})
        if live_doc and live_doc.get("current_encounter_id"):
            enc_id = live_doc["current_encounter_id"]
            enc_query = [{"_id": enc_id}, {"id": enc_id}]
            if ObjectId.is_valid(enc_id):
                enc_query.append({"_id": ObjectId(enc_id)})
            match = await db.matches.find_one({"$or": enc_query})
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    mid = str(match.get("_id", match.get("id")))

    await db.matches.update_one(
        {"_id": match["_id"]},
        {"$set": {
            "score_a": payload.score_a,
            "score_b": payload.score_b,
            "evidence_url": payload.evidence_url,
            "captain_notes": payload.captain_notes,
            "status": "COMPLETED",
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
        "status": "COMPLETED",
    })

    try:
        from app.services.live_match_service import sync_live_match_for_tournament
        await sync_live_match_for_tournament(str(match.get("tournament_id")), db)
    except Exception:
        pass

    return {"status": "success", "message": "Match result finalized successfully"}

@router.post("/{id}/qualifiers")
@router.post("/{id}/battle-royale-results")
async def submit_battle_royale_results_matches_alias(
    id: str,
    payload: dict,
    current_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from app.api.v1.endpoints.admin import submit_battle_royale_results_admin, BattleRoyaleSubmitResultsPayload
    # If qualifiers/results are being submitted on a LIVE or SCHEDULED match, transition match to ENDED first
    m = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
    if m and m.get("status") in ["LIVE", "SCHEDULED", "CHECK_IN_OPEN"]:
        await db.matches.update_one({"$or": [{"_id": id}, {"id": id}]}, {"$set": {"status": "ENDED"}})
    p = BattleRoyaleSubmitResultsPayload(**payload)
    return await submit_battle_royale_results_admin(id=id, payload=p, admin_user=current_user, db=db)


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


async def _resolve_public_team_roster(
    team_id: Optional[str],
    team_name: Optional[str],
    db: AsyncIOMotorDatabase
) -> Optional[Dict[str, Any]]:
    if not team_id and not team_name:
        return None
    from bson import ObjectId
    t_query = []
    if team_id:
        t_query.extend([{"_id": team_id}, {"id": team_id}])
        if ObjectId.is_valid(team_id):
            t_query.append({"_id": ObjectId(team_id)})
    if team_name:
        t_query.append({"name": team_name})

    team = await db.teams.find_one({"$or": t_query}) if t_query else None
    if team:
        cap_user = None
        cap_id = team.get("captain_id")
        if cap_id:
            u_query = [{"_id": cap_id}, {"id": cap_id}]
            if ObjectId.is_valid(cap_id):
                u_query.append({"_id": ObjectId(cap_id)})
            cap_user = await db.users.find_one({"$or": u_query})

        captain_info = {
            "id": str(cap_id) if cap_id else None,
            "name": (cap_user.get("full_name") or cap_user.get("username")) if cap_user else (team.get("captain_handle") or "Captain"),
            "handle": cap_user.get("in_game_name") or team.get("captain_handle") or (cap_user.get("username") if cap_user else "Captain"),
            "username": cap_user.get("username") if cap_user else None,
            "avatar_url": cap_user.get("avatar_url") if cap_user else None,
        }

        members_list = []
        raw_members = team.get("members") or []
        for m in raw_members:
            uid = m.get("user_id") if isinstance(m, dict) else getattr(m, "user_id", None)
            m_user = None
            if uid:
                u_q = [{"_id": uid}, {"id": uid}]
                if ObjectId.is_valid(uid):
                    u_q.append({"_id": ObjectId(uid)})
                m_user = await db.users.find_one({"$or": u_q})

            m_name = (m_user.get("full_name") or m_user.get("username")) if m_user else (m.get("player_handle") or m.get("game_handle") or "Player")
            m_handle = m_user.get("in_game_name") or m.get("player_handle") or m.get("game_handle") or (m_user.get("username") if m_user else "Player")
            members_list.append({
                "id": str(uid) if uid else None,
                "name": m_name,
                "handle": m_handle,
                "role": m.get("role", "PLAYER") if isinstance(m, dict) else "PLAYER",
                "avatar_url": m_user.get("avatar_url") if m_user else None,
            })

        return {
            "id": str(team.get("_id", team.get("id"))),
            "name": team.get("name"),
            "tag": team.get("tag", "TEAM"),
            "logo_url": team.get("logo_url"),
            "captain": captain_info,
            "members": members_list,
        }

    # Solo player
    u_query = []
    if team_id:
        u_query.extend([{"_id": team_id}, {"id": team_id}])
        if ObjectId.is_valid(team_id):
            u_query.append({"_id": ObjectId(team_id)})
    if team_name:
        u_query.append({"username": team_name})

    solo_user = await db.users.find_one({"$or": u_query}) if u_query else None
    if solo_user:
        solo_info = {
            "id": str(solo_user.get("_id", solo_user.get("id"))),
            "name": solo_user.get("full_name") or solo_user.get("username"),
            "handle": solo_user.get("in_game_name") or solo_user.get("username"),
            "username": solo_user.get("username"),
            "avatar_url": solo_user.get("avatar_url"),
        }
        return {
            "id": str(solo_user.get("_id", solo_user.get("id"))),
            "name": solo_user.get("username"),
            "tag": "SOLO",
            "logo_url": solo_user.get("avatar_url"),
            "captain": solo_info,
            "members": [solo_info],
        }

    return {
        "id": team_id,
        "name": team_name or "TBD",
        "tag": "TEAM",
        "logo_url": None,
        "captain": {"name": team_name or "TBD", "handle": "TBD"},
        "members": [],
    }


async def is_match_authorized_viewer(current_user: Optional[User], match: dict, db: AsyncIOMotorDatabase) -> bool:
    if not current_user:
        return False
    if current_user.role in ["SUPER_ADMIN", "ADMIN", "ORGANIZER"]:
        return True

    tourn_id = match.get("tournament_id")
    if tourn_id:
        assign_check = await db.tournament_admin_assignments.find_one({
            "admin_user_id": current_user.id,
            "tournament_id": tourn_id,
        })
        if assign_check:
            return True

    team_a_id = match.get("team_a_id")
    team_b_id = match.get("team_b_id")
    br_teams = match.get("teams") or []
    br_team_ids = [str(t.get("id") or t.get("_id")) for t in br_teams if isinstance(t, dict)]
    competing_teams = list(set([tid for tid in [team_a_id, team_b_id] + br_team_ids if tid]))

    if current_user.id in [team_a_id, team_b_id]:
        return True

    if competing_teams:
        team_doc = await db.teams.find_one({
            "$and": [
                {"$or": [{"_id": {"$in": competing_teams}}, {"id": {"$in": competing_teams}}]},
                {"$or": [
                    {"captain_id": current_user.id},
                    {"members.user_id": current_user.id},
                ]}
            ]
        })
        if team_doc:
            return True

    # Check tournament registrations for solo or team contender
    if tourn_id:
        tid_str = str(tourn_id)
        valid_uids = [str(current_user.id)]
        if getattr(current_user, "clerk_user_id", None):
            valid_uids.append(str(current_user.clerk_user_id))
        reg_check = await db.tournament_registrations.find_one({
            "$or": [{"tournament_id": tid_str}, {"tournament_id": match.get("tournament_slug", tid_str)}],
            "$or": [
                {"player_id": {"$in": valid_uids}},
                {"user_id": {"$in": valid_uids}},
                {"team_id": {"$in": competing_teams}} if competing_teams else {"team_id": "NONE"},
            ],
            "payment_status": {"$in": ["PAID", "FREE"]},
        })
        if reg_check:
            return True

    return False



@router.get("/{id}")
async def get_match_detail(
    id: str,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Sections 29, 30, 31, 32:
    Returns complete rich details for the Live Match detail view/page:
    - Tournament metadata
    - Verified roster formation (Team A & Team B captain + players)
    - Multi-team participant rosters for Battle Royale
    - Match journey & progression context (previous matches, next match, tournament rounds)
    """
    from bson import ObjectId
    m_query = {"$or": [{"_id": id}, {"id": id}]}
    if ObjectId.is_valid(id):
        m_query["$or"].append({"_id": ObjectId(id)})

    match = await db.matches.find_one(m_query)
    live_doc = None
    if not match:
        live_doc = await db.live_matches.find_one({"$or": [{"_id": id}, {"id": id}, {"tournament_id": id}]})
        if live_doc and live_doc.get("current_encounter_id"):
            enc_id = live_doc["current_encounter_id"]
            enc_query = [{"_id": enc_id}, {"id": enc_id}]
            if ObjectId.is_valid(enc_id):
                enc_query.append({"_id": ObjectId(enc_id)})
            match = await db.matches.find_one({"$or": enc_query})

        if not match:
            tourn_doc_check = await db.tournaments.find_one({"$or": [{"_id": id}, {"id": id}, {"slug": id}]})
            if tourn_doc_check:
                from app.services.live_match_service import sync_live_match_for_tournament
                tid_str = str(tourn_doc_check.get("_id", tourn_doc_check.get("id")))
                synced_live = await sync_live_match_for_tournament(tid_str, db)
                if synced_live and synced_live.get("current_encounter_id"):
                    enc_id = synced_live["current_encounter_id"]
                    enc_query = [{"_id": enc_id}, {"id": enc_id}]
                    if ObjectId.is_valid(enc_id):
                        enc_query.append({"_id": ObjectId(enc_id)})
                    match = await db.matches.find_one({"$or": enc_query})
                    live_doc = synced_live

        if not match:
            raise HTTPException(status_code=404, detail="Match not found")

    m = Match.from_mongo(match).model_dump()

    # 1. Resolve Tournament Details
    tourn_id = m.get("tournament_id")
    tourn_doc = None
    game_doc = None
    if tourn_id:
        t_query = {"$or": [{"id": tourn_id}, {"_id": tourn_id}, {"slug": tourn_id}]}
        if ObjectId.is_valid(tourn_id):
            t_query["$or"].append({"_id": ObjectId(tourn_id)})
        tourn_doc = await db.tournaments.find_one(t_query)
        if tourn_doc:
            gid = tourn_doc.get("game_id")
            if gid:
                g_query = [{"_id": gid}, {"id": gid}, {"slug": gid}]
                if ObjectId.is_valid(gid):
                    g_query.append({"_id": ObjectId(gid)})
                game_doc = await db.games.find_one({"$or": g_query})

    # Inherit tournament stream url if not set on match
    if not m.get("youtube_live_url") and tourn_doc and tourn_doc.get("live_stream_url"):
        m["youtube_live_url"] = tourn_doc.get("live_stream_url")
        m["live_link"] = tourn_doc.get("live_stream_url")

    # 2. Resolve Rich Rosters for Team A and Team B
    team_a_details = await _resolve_public_team_roster(m.get("team_a_id"), m.get("team_a_name"), db)
    team_b_details = await _resolve_public_team_roster(m.get("team_b_id"), m.get("team_b_name"), db)

    # 3. Resolve Multi-Team Rosters (Battle Royale)
    teams_details = []
    if m.get("teams") and isinstance(m.get("teams"), list):
        for t_info in m.get("teams"):
            tid = t_info.get("id") or t_info.get("_id")
            tname = t_info.get("name")
            res = await _resolve_public_team_roster(tid, tname, db)
            if res:
                teams_details.append(res)

    # 4. Resolve Previous Matches feeding into this match
    previous_matches = []
    prev_ids = list(m.get("previous_match_ids") or [])
    if not prev_ids and m.get("round_number", 1) > 1:
        prev_r = m.get("round_number", 1) - 1
        m_num = m.get("match_number", 1)
        feeder_m1 = (m_num * 2) - 1
        feeder_m2 = m_num * 2
        prev_cursor = db.matches.find({
            "tournament_id": m.get("tournament_id"),
            "round_number": prev_r,
            "match_number": {"$in": [feeder_m1, feeder_m2]}
        })
        async for pm in prev_cursor:
            previous_matches.append({
                "id": str(pm.get("_id", pm.get("id"))),
                "round_name": pm.get("round_name") or f"Round {prev_r}",
                "match_number": pm.get("match_number"),
                "team_a_name": pm.get("team_a_name") or "TBD",
                "team_b_name": pm.get("team_b_name") or "TBD",
                "winner_team_id": pm.get("winner_team_id"),
                "status": pm.get("status"),
                "score_a": pm.get("score_a", 0),
                "score_b": pm.get("score_b", 0),
            })
    elif prev_ids:
        prev_cursor = db.matches.find({"_id": {"$in": prev_ids}})
        async for pm in prev_cursor:
            previous_matches.append({
                "id": str(pm.get("_id", pm.get("id"))),
                "round_name": pm.get("round_name"),
                "match_number": pm.get("match_number"),
                "team_a_name": pm.get("team_a_name") or "TBD",
                "team_b_name": pm.get("team_b_name") or "TBD",
                "winner_team_id": pm.get("winner_team_id"),
                "status": pm.get("status"),
            })

    # 5. Resolve Downstream Match
    next_match = None
    if m.get("next_match_id"):
        nm = await db.matches.find_one({"$or": [{"_id": m["next_match_id"]}, {"id": m["next_match_id"]}]})
        if nm:
            next_match = {
                "id": str(nm.get("_id", nm.get("id"))),
                "round_name": nm.get("round_name"),
                "match_number": nm.get("match_number"),
                "team_a_name": nm.get("team_a_name") or nm.get("team_a_placeholder") or "TBD",
                "team_b_name": nm.get("team_b_name") or nm.get("team_b_placeholder") or "TBD",
                "status": nm.get("status"),
            }

    # 6. Tournament Progression Rounds
    tournament_progression = []
    if tourn_doc:
        custom_rounds = tourn_doc.get("custom_rounds") or []
        distinct_rounds = await db.matches.distinct("round_name", {"tournament_id": m.get("tournament_id")})
        rounds_seen = []
        for r_name in (custom_rounds + distinct_rounds):
            if r_name and r_name not in rounds_seen:
                rounds_seen.append(r_name)

        if not rounds_seen:
            rounds_seen = ["Quarterfinals", "Semifinals", "Grand Final"]

        curr_round_name = m.get("round_name") or m.get("stage")
        for idx, r_name in enumerate(rounds_seen, start=1):
            is_curr = (r_name == curr_round_name or idx == m.get("round_number"))
            tournament_progression.append({
                "round_number": idx,
                "round_name": r_name,
                "is_current": is_curr,
            })

    # Strip sensitive credentials from public payload unless released and authorized
    credentials_released = bool(m.get("credentials_released", False))
    is_admin_or_organizer = bool(current_user and current_user.role in ["SUPER_ADMIN", "ADMIN", "ORGANIZER"])
    is_authorized = await is_match_authorized_viewer(current_user, m, db)

    if not is_admin_or_organizer and not (credentials_released and is_authorized):
        m["room_id"] = None
        m["room_password"] = None
        m["lobby_code"] = None
        m["lobby_password"] = None
        m["custom_instructions"] = None

    m.pop("join_info", None)

    live_match = await db.live_matches.find_one({"tournament_id": tourn_id}) if tourn_id else None
    if live_match:
        m["current_encounter_id"] = live_match.get("current_encounter_id")
        m["current_encounter_number"] = live_match.get("current_encounter_number")
        m["current_encounter_title"] = live_match.get("current_encounter_title")
        m["current_round_name"] = live_match.get("current_round_name")
        m["encounter_history"] = live_match.get("encounter_history", [])
    else:
        m["current_encounter_id"] = str(match.get("_id", match.get("id")))
        m["current_encounter_number"] = int(match.get("match_number") or match.get("order") or 1)
        m["current_encounter_title"] = match.get("stage") or f"Match {m['current_encounter_number']}"
        m["current_round_name"] = match.get("round_name") or match.get("stage") or "Round 1"
        m["encounter_history"] = []

    return {
        **m,
        "tournament": {
            "id": str(tourn_doc.get("_id", tourn_doc.get("id"))) if tourn_doc else tourn_id,
            "title": tourn_doc.get("title", "Tournament") if tourn_doc else "Tournament",
            "name": tourn_doc.get("title", "Tournament") if tourn_doc else "Tournament",
            "banner_url": tourn_doc.get("banner_url") if tourn_doc else None,
            "format": tourn_doc.get("match_format") or tourn_doc.get("format", "Single Elimination") if tourn_doc else "Single Elimination",
            "status": tourn_doc.get("status", "ONGOING") if tourn_doc else "ONGOING",
            "game_id": tourn_doc.get("game_id") if tourn_doc else None,
            "game_name": (game_doc.get("name") if game_doc else tourn_doc.get("game_name")) if tourn_doc else "Esports",
            "game_slug": (game_doc.get("slug") if game_doc else "game") if game_doc else "game",
            "community_platform": tourn_doc.get("community_platform") if tourn_doc else None,
            "community_link": tourn_doc.get("community_link") if tourn_doc else None,
        },
        "team_a_details": team_a_details,
        "team_b_details": team_b_details,
        "teams_details": teams_details,
        "previous_matches": previous_matches,
        "next_match": next_match,
        "tournament_progression": tournament_progression,
    }


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
        live_doc = await db.live_matches.find_one({"$or": [{"_id": id}, {"id": id}, {"tournament_id": id}]})
        if live_doc and live_doc.get("current_encounter_id"):
            enc_id = live_doc["current_encounter_id"]
            enc_query = [{"_id": enc_id}, {"id": enc_id}]
            if ObjectId.is_valid(enc_id):
                enc_query.append({"_id": ObjectId(enc_id)})
            match = await db.matches.find_one({"$or": enc_query})
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

    # Verify user is an authorized participant or admin for this match
    is_auth = await is_match_authorized_viewer(current_user, match, db)
    if not is_auth:
        raise HTTPException(
            status_code=403,
            detail="Join information is restricted to confirmed match participants.",
        )

    # Gating on credentials_released for participants
    credentials_released = bool(match.get("credentials_released", False))
    if not credentials_released and current_user.role not in ["SUPER_ADMIN", "ADMIN", "ORGANIZER"]:
        return {
            "join_info_enabled": False,
            "join_info": None,
            "room_id": None,
            "room_password": None,
            "lobby_code": None,
            "lobby_password": None,
            "custom_instructions": None,
            "match_message": "Credentials have not been released by the organizer yet. They will appear here once released.",
            "credentials_released": False,
        }

    room_val = match.get("room_id") or match.get("lobby_code") or match.get("custom_room_id")
    pass_val = match.get("room_password") or match.get("lobby_password") or match.get("custom_room_password")

    resolved_join_info = dict(join_info or {}) if isinstance(join_info, dict) else {}
    if room_val and "room_id" not in resolved_join_info:
        resolved_join_info["room_id"] = room_val
    if pass_val and "room_password" not in resolved_join_info:
        resolved_join_info["room_password"] = pass_val

    return {
        "join_info_enabled": enabled or credentials_released,
        "join_info": resolved_join_info if (enabled or credentials_released) else None,
        "room_id": room_val,
        "room_password": pass_val,
        "custom_instructions": match.get("custom_instructions"),
        "match_message": match.get("custom_instructions") or match_msg,
        "credentials_released": credentials_released,
    }

class MatchUpdatePayload(BaseModel):
    lobby_code: Optional[str] = None
    lobby_password: Optional[str] = None
    youtube_live_url: Optional[str] = None
    live_link: Optional[str] = None
    server_region: Optional[str] = None
    status: Optional[str] = None
    score_a: Optional[int] = None
    score_b: Optional[int] = None
    winner_team_id: Optional[str] = None

@router.patch("/{id}")
async def update_match_details(
    id: str,
    payload: MatchUpdatePayload,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    if current_user.role not in ["SUPER_ADMIN", "ADMIN", "ORGANIZER"]:
        raise HTTPException(status_code=403, detail="Admin or Organizer privilege required")

    match = await db.matches.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not match:
        raise HTTPException(status_code=404, detail="Match not found")

    update_data = {}
    if payload.lobby_code is not None:
        val = payload.lobby_code.strip() if payload.lobby_code else None
        update_data["lobby_code"] = val
        update_data["room_id"] = val
    if payload.lobby_password is not None:
        val = payload.lobby_password.strip() if payload.lobby_password else None
        update_data["lobby_password"] = val
        update_data["room_password"] = val
    if payload.youtube_live_url is not None:
        url = payload.youtube_live_url.strip() or None
        update_data["youtube_live_url"] = url
        update_data["live_link"] = url
    elif payload.live_link is not None:
        url = payload.live_link.strip() or None
        update_data["youtube_live_url"] = url
        update_data["live_link"] = url
    if payload.server_region is not None:
        update_data["server_region"] = payload.server_region
    if payload.status is not None:
        stat_upper = payload.status.upper()
        valid_statuses = [
            "DRAFT", "SCHEDULED", "CHECK_IN_OPEN", "LIVE",
            "ENDED", "RESULTS_SAVED", "RESULTS_PENDING",
            "FINALIZED", "COMPLETED", "CANCELLED"
        ]
        if stat_upper not in valid_statuses:
            raise HTTPException(status_code=400, detail=f"Invalid match status: {payload.status}")

        curr_stat = str(match.get("status", "SCHEDULED")).upper()
        if curr_stat in ["COMPLETED", "FINALIZED"] and stat_upper in ["LIVE", "SCHEDULED", "CHECK_IN_OPEN"]:
            raise HTTPException(status_code=400, detail="Cannot restart a completed or finalized match.")

        update_data["status"] = stat_upper
    if payload.score_a is not None:
        update_data["score_a"] = payload.score_a
    if payload.score_b is not None:
        update_data["score_b"] = payload.score_b
    if payload.winner_team_id is not None:
        update_data["winner_team_id"] = payload.winner_team_id

    if update_data:
        update_data["updated_at"] = datetime.now(timezone.utc)
        await db.matches.update_one({"_id": match["_id"]}, {"$set": update_data})
        try:
            from app.services.live_match_service import sync_live_match_for_tournament
            await sync_live_match_for_tournament(str(match.get("tournament_id")), db)
        except Exception:
            pass

    return {"status": "success", "message": "Match updated successfully"}

@router.post("/{id}/battle-royale-results")
async def submit_match_battle_royale_results(
    id: str,
    payload: BattleRoyaleSubmitResultsPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    from app.api.v1.endpoints.admin import submit_battle_royale_results_admin
    return await submit_battle_royale_results_admin(
        id=id,
        payload=payload,
        admin_user=current_user,
        db=db,
    )


