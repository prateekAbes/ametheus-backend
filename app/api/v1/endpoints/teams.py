from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.game import Game
from app.models.team import Team, TeamMember
from app.models.tournament import Tournament, TournamentRegistration
from app.services.audit import record_audit_log
from app.schemas import TeamCreate, TeamJoin, TeamResponse, TeamMemberResponse

router = APIRouter()

async def serialize_team(team: Team, db: AsyncSession) -> TeamResponse:
    g_res = await db.execute(select(Game).where(Game.id == team.game_id))
    game = g_res.scalars().first()

    cap_res = await db.execute(select(User).where(User.id == team.captain_id))
    captain = cap_res.scalars().first()

    mem_res = await db.execute(select(TeamMember).where(TeamMember.team_id == team.id))
    members = mem_res.scalars().all()

    user_ids = [m.user_id for m in members]
    users_dict = {}
    if user_ids:
        u_res = await db.execute(select(User).where(User.id.in_(user_ids)))
        for u in u_res.scalars().all():
            users_dict[u.id] = u.display_name or u.username

    return TeamResponse(
        id=team.id,
        game_slug=game.slug if game else "valorant",
        name=team.name,
        tag=team.tag,
        invite_code=team.invite_code,
        captain_id=team.captain_id,
        captain_username=captain.display_name or captain.username if captain else "Captain",
        max_members=team.max_members,
        members=[
            TeamMemberResponse(
                id=m.id,
                user_id=m.user_id,
                username=users_dict.get(m.user_id, "Member"),
                role=m.role,
                game_handle=m.game_handle,
            )
            for m in members
        ],
    )

@router.post("", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
async def create_team(
    payload: TeamCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Check game exists
    game_res = await db.execute(select(Game).where(Game.slug == payload.game_slug))
    game = game_res.scalars().first()
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    # Create team
    team = Team(
        game_id=game.id,
        captain_id=current_user.id,
        name=payload.name,
        tag=payload.tag.upper(),
        max_members=game.max_team_size,
    )
    db.add(team)
    await db.flush()

    # Add captain as member
    member = TeamMember(
        team_id=team.id,
        user_id=current_user.id,
        role="CAPTAIN",
        game_handle=payload.captain_handle,
    )
    db.add(member)
    await db.commit()
    await db.refresh(team)

    return await serialize_team(team, db)

@router.post("/join", response_model=TeamResponse)
async def join_team(
    payload: TeamJoin,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Rule 3.3: Team Join is ALWAYS ₹0. No payment checks.
    clean_code = payload.invite_code.strip().upper()
    team_res = await db.execute(select(Team).where(Team.invite_code == clean_code))
    team = team_res.scalars().first()
    if not team:
        raise HTTPException(status_code=404, detail="Invalid invite code. No squad found.")

    # Check members count
    mem_res = await db.execute(select(TeamMember).where(TeamMember.team_id == team.id))
    members = mem_res.scalars().all()
    if len(members) >= team.max_members:
        raise HTTPException(status_code=400, detail="Team roster is already full.")

    # Check already joined
    if any(m.user_id == current_user.id for m in members):
        raise HTTPException(status_code=400, detail="User is already on this team roster.")

    # Add member
    new_member = TeamMember(
        team_id=team.id,
        user_id=current_user.id,
        role="MEMBER",
        game_handle=payload.player_handle,
    )
    db.add(new_member)
    await db.commit()
    await db.refresh(team)

    return await serialize_team(team, db)

@router.get("/my-teams", response_model=List[TeamResponse])
async def get_my_teams(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    mem_res = await db.execute(select(TeamMember.team_id).where(TeamMember.user_id == current_user.id))
    member_team_ids = set(mem_res.scalars().all())

    cap_res = await db.execute(select(Team.id).where(Team.captain_id == current_user.id))
    cap_team_ids = set(cap_res.scalars().all())

    all_team_ids = list(member_team_ids.union(cap_team_ids))
    if not all_team_ids:
        return []

    result = await db.execute(select(Team).where(Team.id.in_(all_team_ids)))
    teams = result.scalars().all()
    return [await serialize_team(t, db) for t in teams]

@router.get("/code/{code}", response_model=TeamResponse)
async def get_team_by_code(code: str, db: AsyncSession = Depends(get_db)):
    clean_code = code.strip().upper()
    team_res = await db.execute(select(Team).where(Team.invite_code == clean_code))
    team = team_res.scalars().first()
    if not team:
        raise HTTPException(status_code=404, detail="Invalid invite code. No squad found.")
    return await serialize_team(team, db)

@router.get("/{id}", response_model=TeamResponse)
async def get_team(id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Team).where(Team.id == id))
    team = result.scalars().first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    return await serialize_team(team, db)

@router.post("/{id}/leave")
async def leave_team(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Allow a non-captain member to voluntarily leave the roster.
    """
    team_res = await db.execute(select(Team).where(Team.id == id))
    team = team_res.scalars().first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.captain_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The captain cannot leave the team. Disband the team or transfer leadership.",
        )

    mem_res = await db.execute(
        select(TeamMember).where(
            TeamMember.team_id == id,
            TeamMember.user_id == current_user.id,
        )
    )
    membership = mem_res.scalars().first()
    if not membership:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You are not a member of this team roster.",
        )

    await db.delete(membership)
    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="LEAVE_TEAM",
        target=f"Team {team.name} ({team.id})",
        details=f"User {current_user.username} left team {team.name}",
    )
    await db.commit()
    return {"status": "success", "message": f"Successfully left team {team.name}"}

@router.post("/{id}/kick/{user_id}")
async def kick_team_member(
    id: str,
    user_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Captain or Super Admin privilege: Remove a member from the team roster.
    """
    team_res = await db.execute(select(Team).where(Team.id == id))
    team = team_res.scalars().first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.captain_id != current_user.id and current_user.role != "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the team captain or platform admin can remove roster members.",
        )

    if user_id == team.captain_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot kick the team captain.",
        )

    mem_res = await db.execute(
        select(TeamMember).where(
            TeamMember.team_id == id,
            TeamMember.user_id == user_id,
        )
    )
    membership = mem_res.scalars().first()
    if not membership:
        raise HTTPException(status_code=404, detail="Member not found on this team roster.")

    await db.delete(membership)
    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="KICK_TEAM_MEMBER",
        target=f"User {user_id} <- Team {team.name} ({team.id})",
        details=f"Captain {current_user.username} removed user {user_id}",
    )
    await db.commit()
    return {"status": "success", "message": "Member removed from team roster"}

@router.delete("/{id}")
async def delete_team(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Captain or Super Admin privilege: Disband the team.
    """
    team_res = await db.execute(select(Team).where(Team.id == id))
    team = team_res.scalars().first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.captain_id != current_user.id and current_user.role != "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the team captain or platform admin can disband the team.",
        )

    # Check for active tournament registrations
    reg_res = await db.execute(
        select(TournamentRegistration)
        .join(Tournament, Tournament.id == TournamentRegistration.tournament_id)
        .where(
            TournamentRegistration.team_id == id,
            Tournament.status == "ONGOING",
        )
    )
    active_regs = reg_res.scalars().all()
    if active_regs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot disband team while participating in an active ongoing tournament.",
        )

    await db.delete(team)
    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="DISBAND_TEAM",
        target=f"Team {team.name} ({team.id})",
        details=f"Team {team.name} disbanded by {current_user.username}",
    )
    await db.commit()
    return {"status": "success", "message": f"Team '{team.name}' has been disbanded"}
