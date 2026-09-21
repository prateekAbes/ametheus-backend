from typing import List, Union
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.team import Team, TeamMember
from app.services.audit import record_audit_log
from app.schemas import TeamCreate, TeamJoin, TeamResponse, TeamMemberResponse

router = APIRouter()

async def serialize_team(team: Union[dict, Team], db: AsyncIOMotorDatabase) -> TeamResponse:
    team_dict = team if isinstance(team, dict) else team.model_dump()
    team_id = str(team_dict.get("_id", team_dict.get("id")))

    game_id = team_dict.get("game_id")
    game = await db.games.find_one({"$or": [{"_id": game_id}, {"id": game_id}, {"slug": game_id}]})

    captain_id = team_dict.get("captain_id")
    captain = await db.users.find_one({"$or": [{"_id": captain_id}, {"id": captain_id}]})

    raw_members = team_dict.get("members", [])
    user_ids = [m.get("user_id") if isinstance(m, dict) else m.user_id for m in raw_members]

    users_dict = {}
    if user_ids:
        cursor = db.users.find({"$or": [{"_id": {"$in": user_ids}}, {"id": {"$in": user_ids}}]})
        async for u in cursor:
            uid = str(u.get("_id", u.get("id")))
            users_dict[uid] = u.get("display_name") or u.get("username")

    member_responses = []
    for m in raw_members:
        mid = m.get("id", m.get("_id")) if isinstance(m, dict) else getattr(m, "id", None)
        muid = m.get("user_id") if isinstance(m, dict) else m.user_id
        mrole = m.get("role", "MEMBER") if isinstance(m, dict) else m.role
        mhandle = m.get("game_handle", "") if isinstance(m, dict) else m.game_handle
        member_responses.append(
            TeamMemberResponse(
                id=str(mid),
                user_id=muid,
                username=users_dict.get(muid, "Member"),
                role=mrole,
                game_handle=mhandle,
            )
        )

    cap_name = "Captain"
    if captain:
        cap_name = captain.get("display_name") or captain.get("username") or "Captain"

    return TeamResponse(
        id=team_id,
        game_slug=game["slug"] if game else "valorant",
        name=team_dict["name"],
        tag=team_dict["tag"],
        invite_code=team_dict["invite_code"],
        captain_id=captain_id,
        captain_username=cap_name,
        max_members=team_dict.get("max_members", 5),
        members=member_responses,
    )

@router.post("", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
async def create_team(
    payload: TeamCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    # Check game exists
    game = await db.games.find_one({"slug": payload.game_slug})
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    # Reject team creation for solo games (e.g. Chess)
    is_team_game = game.get("is_team_game", True)
    max_size = game.get("max_team_size", 5)
    if not is_team_game or max_size <= 1:
        raise HTTPException(
            status_code=400,
            detail=f"{game.get('name', 'This game')} is a solo title and does not support squad creation.",
        )

    gid = str(game.get("_id", game.get("id")))

    # Add captain as first member
    captain_member = TeamMember(
        user_id=current_user.id,
        role="CAPTAIN",
        game_handle=payload.captain_handle,
    )

    team = Team(
        game_id=gid,
        captain_id=current_user.id,
        name=payload.name,
        tag=payload.tag.upper(),
        max_members=max_size,
        members=[captain_member],
    )
    team_doc = team.to_mongo()
    await db.teams.insert_one(team_doc)

    return await serialize_team(team_doc, db)

@router.post("/join", response_model=TeamResponse)
async def join_team(
    payload: TeamJoin,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    # Rule 3.3: Team Join is ALWAYS ₹0. No payment checks.
    clean_code = payload.invite_code.strip().upper()
    team = await db.teams.find_one({"invite_code": clean_code})
    if not team:
        raise HTTPException(status_code=404, detail="Invalid invite code. No squad found.")

    game_id = team.get("game_id")
    game = await db.games.find_one({"$or": [{"_id": game_id}, {"id": game_id}, {"slug": game_id}]})
    if game and (not game.get("is_team_game", True) or game.get("max_team_size", 5) <= 1):
        raise HTTPException(
            status_code=400,
            detail="Cannot join a squad for a solo discipline.",
        )

    members = team.get("members", [])
    max_cap = team.get("max_members") or (game.get("max_team_size", 5) if game else 5)
    if len(members) >= max_cap:
        raise HTTPException(status_code=400, detail="Team roster is already full.")

    if any((m.get("user_id") if isinstance(m, dict) else m.user_id) == current_user.id for m in members):
        raise HTTPException(status_code=400, detail="User is already on this team roster.")

    team_id = str(team.get("_id", team.get("id")))
    new_member = TeamMember(
        team_id=team_id,
        user_id=current_user.id,
        role="MEMBER",
        game_handle=payload.player_handle,
    )

    await db.teams.update_one(
        {"_id": team["_id"]},
        {"$push": {"members": new_member.model_dump()}}
    )
    updated_team = await db.teams.find_one({"_id": team["_id"]})
    return await serialize_team(updated_team, db)

@router.get("/my-teams", response_model=List[TeamResponse])
async def get_my_teams(
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    cursor = db.teams.find({
        "$or": [
            {"captain_id": current_user.id},
            {"members.user_id": current_user.id},
        ]
    })
    teams = await cursor.to_list(length=100)
    return [await serialize_team(t, db) for t in teams]

@router.get("/code/{code}", response_model=TeamResponse)
async def get_team_by_code(code: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    clean_code = code.strip().upper()
    team = await db.teams.find_one({"invite_code": clean_code})
    if not team:
        raise HTTPException(status_code=404, detail="Invalid invite code. No squad found.")
    return await serialize_team(team, db)

@router.get("/{id}", response_model=TeamResponse)
async def get_team(id: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    team = await db.teams.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return await serialize_team(team, db)

@router.post("/{id}/leave")
async def leave_team(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Allow a non-captain member to voluntarily leave the roster.
    """
    team = await db.teams.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.get("captain_id") == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The captain cannot leave the team. Disband the team or transfer leadership.",
        )

    members = team.get("members", [])
    if not any((m.get("user_id") if isinstance(m, dict) else m.user_id) == current_user.id for m in members):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You are not a member of this team roster.",
        )

    await db.teams.update_one(
        {"_id": team["_id"]},
        {"$pull": {"members": {"user_id": current_user.id}}}
    )
    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="LEAVE_TEAM",
        target=f"Team {team['name']} ({str(team['_id'])})",
        details=f"User {current_user.username} left team {team['name']}",
    )
    return {"status": "success", "message": f"Successfully left team {team['name']}"}

@router.post("/{id}/kick/{user_id}")
async def kick_team_member(
    id: str,
    user_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Captain or Super Admin privilege: Remove a member from the team roster.
    """
    team = await db.teams.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.get("captain_id") != current_user.id and current_user.role != "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the team captain or platform admin can remove roster members.",
        )

    if user_id == team.get("captain_id"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot kick the team captain.",
        )

    members = team.get("members", [])
    if not any((m.get("user_id") if isinstance(m, dict) else m.user_id) == user_id for m in members):
        raise HTTPException(status_code=404, detail="Member not found on this team roster.")

    await db.teams.update_one(
        {"_id": team["_id"]},
        {"$pull": {"members": {"user_id": user_id}}}
    )
    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="KICK_TEAM_MEMBER",
        target=f"User {user_id} <- Team {team['name']} ({str(team['_id'])})",
        details=f"Captain {current_user.username} removed user {user_id}",
    )
    return {"status": "success", "message": "Member removed from team roster"}

@router.delete("/{id}")
async def delete_team(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Captain or Super Admin privilege: Disband the team.
    """
    team = await db.teams.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    tid = str(team.get("_id", team.get("id")))
    if team.get("captain_id") != current_user.id and current_user.role != "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the team captain or platform admin can disband the team.",
        )

    # Check for active tournament registrations
    regs = await db.tournament_registrations.find({"$or": [{"team_id": tid}, {"team_id": team.get("id")}]}).to_list(100)
    if regs:
        tourn_ids = [r["tournament_id"] for r in regs]
        ongoing = await db.tournaments.find_one({
            "$and": [
                {"$or": [{"_id": {"$in": tourn_ids}}, {"id": {"$in": tourn_ids}}]},
                {"status": "ONGOING"},
            ]
        })
        if ongoing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot disband team while participating in an active ongoing tournament.",
            )

    await db.teams.delete_one({"_id": team["_id"]})
    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="DISBAND_TEAM",
        target=f"Team {team['name']} ({tid})",
        details=f"Team {team['name']} disbanded by {current_user.username}",
    )
    return {"status": "success", "message": f"Team '{team['name']}' has been disbanded"}
