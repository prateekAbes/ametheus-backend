from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.game import Game
from app.models.team import Team, TeamMember
from app.schemas import TeamCreate, TeamJoin, TeamResponse, TeamMemberResponse

router = APIRouter()

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

    return TeamResponse(
        id=team.id,
        game_slug=game.slug,
        name=team.name,
        tag=team.tag,
        invite_code=team.invite_code,
        captain_id=team.captain_id,
        captain_username=current_user.username,
        max_members=team.max_members,
        members=[
            TeamMemberResponse(
                id=member.id,
                user_id=current_user.id,
                username=current_user.username,
                role="CAPTAIN",
                game_handle=member.game_handle,
            )
        ],
    )

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

    # Get game
    g_res = await db.execute(select(Game).where(Game.id == team.game_id))
    game = g_res.scalars().first()

    # Get updated members
    updated_mems = (await db.execute(select(TeamMember).where(TeamMember.team_id == team.id))).scalars().all()

    return TeamResponse(
        id=team.id,
        game_slug=game.slug if game else "valorant",
        name=team.name,
        tag=team.tag,
        invite_code=team.invite_code,
        captain_id=team.captain_id,
        captain_username=current_user.username,
        max_members=team.max_members,
        members=[
            TeamMemberResponse(
                id=m.id,
                user_id=m.user_id,
                username=current_user.username if m.user_id == current_user.id else "Teammate",
                role=m.role,
                game_handle=m.game_handle,
            )
            for m in updated_mems
        ],
    )

@router.get("/{id}", response_model=TeamResponse)
async def get_team(id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Team).where(Team.id == id))
    team = result.scalars().first()
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    g_res = await db.execute(select(Game).where(Game.id == team.game_id))
    game = g_res.scalars().first()

    mem_res = await db.execute(select(TeamMember).where(TeamMember.team_id == team.id))
    members = mem_res.scalars().all()

    return TeamResponse(
        id=team.id,
        game_slug=game.slug if game else "valorant",
        name=team.name,
        tag=team.tag,
        invite_code=team.invite_code,
        captain_id=team.captain_id,
        captain_username="Captain",
        max_members=team.max_members,
        members=[
            TeamMemberResponse(
                id=m.id,
                user_id=m.user_id,
                username="RosterMember",
                role=m.role,
                game_handle=m.game_handle,
            )
            for m in members
        ],
    )
