from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.dispute import Dispute
from app.schemas import MatchResponse, MatchResultSubmit, DisputeCreate
from app.services.audit import record_audit_log
from app.core.ws import ws_manager

router = APIRouter()

@router.get("", response_model=List[MatchResponse])
async def list_matches(
    tournament_id: Optional[str] = Query(None, description="Filter matches by tournament ID or slug"),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = {}
    if tournament_id:
        tourn = await db.tournaments.find_one({"$or": [{"_id": tournament_id}, {"id": tournament_id}, {"slug": tournament_id}]})
        if tourn:
            tid = str(tourn.get("_id", tourn.get("id")))
            tslug = tourn.get("slug")
            query["$or"] = [{"tournament_id": tid}, {"tournament_id": tslug}]
        else:
            query["tournament_id"] = tournament_id

    cursor = db.matches.find(query)
    matches = await cursor.to_list(length=200)

    response = []
    for m in matches:
        mid = str(m.get("_id", m.get("id")))
        tourn_id = m.get("tournament_id")
        tourn = await db.tournaments.find_one({"$or": [{"_id": tourn_id}, {"id": tourn_id}]}) if tourn_id else None
        
        game = None
        if tourn and tourn.get("game_id"):
            gid = tourn["game_id"]
            game = await db.games.find_one({"$or": [{"_id": gid}, {"id": gid}, {"slug": gid}]})

        team_a_id = m.get("team_a_id")
        team_b_id = m.get("team_b_id")

        team_a = await db.teams.find_one({"$or": [{"_id": team_a_id}, {"id": team_a_id}]}) if team_a_id else None
        team_b = await db.teams.find_one({"$or": [{"_id": team_b_id}, {"id": team_b_id}]}) if team_b_id else None

        response.append(
            MatchResponse(
                id=mid,
                tournament_id=tourn_id or "",
                tournament_title=tourn["title"] if tourn else "Championship",
                game_slug=game["slug"] if game else "valorant",
                game_name=game["name"] if game else "Valorant",
                stage=m.get("stage", "QUARTER_FINAL"),
                format=m.get("format", "BO1"),
                scheduled_time=m.get("scheduled_time", "TBD"),
                status=m.get("status", "SCHEDULED"),
                server_region=m.get("server_region", "AWS Mumbai (ap-south-1)"),
                lobby_code=m.get("lobby_code"),
                lobby_password=m.get("lobby_password"),
                team_a={"name": team_a["name"], "tag": team_a["tag"], "score": m.get("score_a", 0)} if team_a else None,
                team_b={"name": team_b["name"], "tag": team_b["tag"], "score": m.get("score_b", 0)} if team_b else None,
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

    if competing_teams and current_user.role not in ["SUPER_ADMIN", "ORGANIZER"]:
        mem_check = await db.teams.find_one({
            "$and": [
                {"$or": [{"_id": {"$in": competing_teams}}, {"id": {"$in": competing_teams}}]},
                {"members.user_id": current_user.id},
            ]
        })
        if not mem_check:
            raise HTTPException(
                status_code=403,
                detail="You are not registered on either competing roster for this match.",
            )

    new_status = match.get("status", "SCHEDULED")
    if new_status == "CHECK_IN_OPEN":
        new_status = "LIVE"
        await db.matches.update_one({"_id": match["_id"]}, {"$set": {"status": "LIVE"}})

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
