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
    raw_substitutes = team_dict.get("substitutes", [])
    user_ids = [m.get("user_id") if isinstance(m, dict) else m.user_id for m in (raw_members + raw_substitutes)]

    users_dict = {}
    if user_ids:
        cursor = db.users.find({"$or": [{"_id": {"$in": user_ids}}, {"id": {"$in": user_ids}}]})
        async for u in cursor:
            uid = str(u.get("_id", u.get("id")))
            users_dict[uid] = u.get("display_name") or u.get("username")

    def map_member(m, default_role="MEMBER"):
        mid = m.get("id", m.get("_id")) if isinstance(m, dict) else getattr(m, "id", None)
        muid = m.get("user_id") if isinstance(m, dict) else m.user_id
        mrole = m.get("role", default_role) if isinstance(m, dict) else m.role
        mhandle = m.get("game_handle", "") if isinstance(m, dict) else m.game_handle
        return TeamMemberResponse(
            id=str(mid),
            user_id=muid,
            username=users_dict.get(muid, "Gamer"),
            role=mrole,
            game_handle=mhandle,
        )

    cap_name = "Captain"
    if captain:
        cap_name = captain.get("display_name") or captain.get("username") or "Captain"

    return TeamResponse(
        id=team_id,
        game_slug=game["slug"] if game else "game",
        name=team_dict["name"],
        tag=team_dict["tag"],
        invite_code=team_dict["invite_code"],
        captain_id=captain_id,
        captain_username=cap_name,
        max_members=team_dict.get("max_members", 5),
        max_substitutes=team_dict.get("max_substitutes", 1),
        payment_status=team_dict.get("payment_status", "UNPAID"),
        payment_id=team_dict.get("payment_id"),
        members=[map_member(m, "MEMBER") for m in raw_members],
        substitutes=[map_member(m, "SUBSTITUTE") for m in raw_substitutes],
    )

@router.post("", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
async def create_team(
    payload: TeamCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    game = await db.games.find_one({"$or": [{"slug": payload.game_slug}, {"_id": payload.game_slug}, {"id": payload.game_slug}]})
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    is_team_game = game.get("is_team_game", True)
    max_size = game.get("max_team_size", 5)
    max_subs = game.get("max_substitutes", 1)

    if not is_team_game or max_size <= 1:
        raise HTTPException(
            status_code=400,
            detail=f"{game.get('name', 'This game')} is a solo title and does not support team creation.",
        )

    gid = str(game.get("_id", game.get("id")))

    # Enforce one-team-per-game rule (Requirement 19)
    existing_team = await db.teams.find_one({
        "game_id": gid,
        "is_dummy": {"$ne": True},
        "$or": [
            {"captain_id": current_user.id},
            {"members.user_id": current_user.id},
            {"substitutes.user_id": current_user.id},
        ],
    })
    if existing_team:
        raise HTTPException(
            status_code=400,
            detail=f"You already belong to team '{existing_team.get('name')}' for {game.get('name', 'this game')}. Each contender can only lead or join one team per game discipline.",
        )

    captain_member = TeamMember(
        user_id=current_user.id,
        role="CAPTAIN",
        game_handle=payload.captain_handle,
    )

    initial_payment_status = "UNPAID" if game.get("entry_fee_inr", 0) > 0 else "PAID"
    team = Team(
        game_id=gid,
        captain_id=current_user.id,
        name=payload.name,
        tag=payload.tag.upper(),
        max_members=max_size,
        max_substitutes=max_subs,
        members=[captain_member],
        substitutes=[],
        payment_status=initial_payment_status,
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
    clean_code = payload.invite_code.strip().upper()
    team = await db.teams.find_one({"invite_code": clean_code})
    if not team:
        raise HTTPException(status_code=404, detail="Invalid invite code. No team found.")

    game_id = team.get("game_id")
    game = await db.games.find_one({"$or": [{"_id": game_id}, {"id": game_id}, {"slug": game_id}]})
    if game and (not game.get("is_team_game", True) or game.get("max_team_size", 5) <= 1):
        raise HTTPException(
            status_code=400,
            detail="Cannot join a team for a solo discipline.",
        )

    # Enforce one-team-per-game rule (Requirement 19)
    existing_team = await db.teams.find_one({
        "game_id": game_id,
        "is_dummy": {"$ne": True},
        "$or": [
            {"captain_id": current_user.id},
            {"members.user_id": current_user.id},
            {"substitutes.user_id": current_user.id},
        ],
    })
    if existing_team:
        raise HTTPException(
            status_code=400,
            detail=f"You already belong to team '{existing_team.get('name')}' for this game discipline. Each contender can only lead or join one team per game.",
        )

    members = team.get("members", [])
    substitutes = team.get("substitutes", [])

    all_user_ids = [
        (m.get("user_id") if isinstance(m, dict) else m.user_id) for m in (members + substitutes)
    ]
    if current_user.id in all_user_ids:
        raise HTTPException(status_code=400, detail="User is already on this team roster.")

    team_id = str(team.get("_id", team.get("id")))

    if payload.is_substitute:
        max_subs = team.get("max_substitutes", 1)
        if len(substitutes) >= max_subs:
            raise HTTPException(status_code=400, detail="Team substitutes roster is already full.")
        new_sub = TeamMember(
            team_id=team_id,
            user_id=current_user.id,
            role="SUBSTITUTE",
            game_handle=payload.player_handle,
        )
        await db.teams.update_one(
            {"_id": team["_id"]},
            {"$push": {"substitutes": new_sub.model_dump()}}
        )
    else:
        max_cap = team.get("max_members") or (game.get("max_team_size", 5) if game else 5)
        if len(members) >= max_cap:
            raise HTTPException(status_code=400, detail="Team main roster is already full.")
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
    game_slug: str = None,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query = {
        "$or": [
            {"captain_id": current_user.id},
            {"members.user_id": current_user.id},
            {"substitutes.user_id": current_user.id},
        ],
        "is_dummy": {"$ne": True},
    }
    if game_slug:
        game = await db.games.find_one({"slug": game_slug})
        if game:
            gid = str(game.get("_id", game.get("id")))
            query["game_id"] = gid

    cursor = db.teams.find(query)
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
    team = await db.teams.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.get("captain_id") == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The captain cannot leave the team. Disband the team or transfer leadership.",
        )

    await db.teams.update_one(
        {"_id": team["_id"]},
        {
            "$pull": {
                "members": {"user_id": current_user.id},
                "substitutes": {"user_id": current_user.id},
            }
        }
    )
    return {"status": "success", "message": f"Successfully left team {team['name']}"}

@router.post("/{id}/kick/{user_id}")
async def kick_team_member(
    id: str,
    user_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
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

    await db.teams.update_one(
        {"_id": team["_id"]},
        {
            "$pull": {
                "members": {"user_id": user_id},
                "substitutes": {"user_id": user_id},
            }
        }
    )
    return {"status": "success", "message": "Member removed from team roster"}

@router.delete("/{id}")
async def delete_team(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    team = await db.teams.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.get("captain_id") != current_user.id and current_user.role != "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the team captain or platform admin can disband the team.",
        )

    await db.teams.delete_one({"_id": team["_id"]})
    return {"status": "success", "message": f"Team '{team['name']}' has been disbanded"}
