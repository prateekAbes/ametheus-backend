from typing import Optional
from fastapi import Depends, HTTPException, status, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from app.core.database import get_db
from app.models.user import User
from app.models.tournament import Tournament
from app.models.admin import TournamentAdminAssignment
from app.core.security import decode_token

async def get_current_user(
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Bearer authentication token required",
        )

    token = authorization.replace("Bearer ", "").strip()
    payload = decode_token(token)
    if not payload or "sub" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Invalid or expired authentication token",
        )

    target_id = payload["sub"]

    # Authoritative DB lookup by clerk_user_id or user.id
    result = await db.execute(
        select(User).where(
            or_(User.id == target_id, User.clerk_user_id == target_id)
        )
    )
    user = result.scalars().first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: User not found in AMETHEUS. Please complete profile sync.",
        )

    return user

async def require_player(
    current_user: User = Depends(get_current_user),
) -> User:
    # All authenticated users have player privileges
    return current_user

async def require_organizer(
    current_user: User = Depends(get_current_user),
) -> User:
    if current_user.role not in ["ORGANIZER", "TOURNAMENT_ADMIN", "SUPER_ADMIN"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Operation requires ORGANIZER privilege (Rule 9)",
        )
    return current_user

async def require_super_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    if current_user.role != "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Operation requires SUPER_ADMIN privilege (Rule 10)",
        )
    return current_user

class RequireTournamentOwnershipOrAdmin:
    """
    Rule 20: Validates that an Organizer owns the tournament, or has an assignment, or is Super Admin.
    """
    def __init__(self, tournament_id_param: str = "id"):
        self.param = tournament_id_param

    async def __call__(
        self,
        id: str,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        if current_user.role == "SUPER_ADMIN":
            return current_user

        t_res = await db.execute(select(Tournament).where(Tournament.id == id))
        tourn = t_res.scalars().first()
        if not tourn:
            raise HTTPException(status_code=404, detail="Tournament not found")

        # Check ownership
        if tourn.organizer_id == current_user.id:
            return current_user

        # Check assignment
        assign_res = await db.execute(
            select(TournamentAdminAssignment).where(
                TournamentAdminAssignment.admin_user_id == current_user.id,
                TournamentAdminAssignment.tournament_id == id,
            )
        )
        if assign_res.scalars().first():
            return current_user

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You do not own or have administrative rights for this tournament (Rule 20)",
        )

# Backward-compatible alias
RequireTournamentAdmin = RequireTournamentOwnershipOrAdmin
