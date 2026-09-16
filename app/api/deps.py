from typing import Optional, AsyncGenerator
from fastapi import Depends, HTTPException, status, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.core.database import get_db, AsyncSessionLocal
from app.models.user import User
from app.models.admin import TournamentAdminAssignment
from app.core.security import decode_token

async def get_current_user(
    authorization: Optional[str] = Header(None),
    x_user_id: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
) -> User:
    # 1. Direct header for dev/test environments
    target_id = x_user_id
    if not target_id and authorization:
        token = authorization.replace("Bearer ", "").strip()
        payload = decode_token(token)
        if payload and "sub" in payload:
            target_id = payload["sub"]

    if not target_id:
        # Fallback to demo default user
        target_id = "usr_vortex_lead"

    result = await db.execute(select(User).where(User.id == target_id))
    user = result.scalars().first()
    if not user:
        # Create on the fly for seamless dev
        user = User(
            id=target_id,
            email=f"{target_id}@ametheus.gg",
            username=target_id.replace("usr_", ""),
            role="SUPER_ADMIN" if "super" in target_id else "PLAYER",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user

async def require_super_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    if current_user.role != "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Operation requires SUPER_ADMIN privilege (Rule 2.1)",
        )
    return current_user

class RequireTournamentAdmin:
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

        # Check tournament admin assignment
        result = await db.execute(
            select(TournamentAdminAssignment).where(
                TournamentAdminAssignment.admin_user_id == current_user.id,
                TournamentAdminAssignment.tournament_id == id,
            )
        )
        assignment = result.scalars().first()
        if not assignment:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You are not assigned to manage this tournament (Rule 2.2)",
            )
        return current_user
