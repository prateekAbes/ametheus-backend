from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.schemas import UserResponse, UserBase
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

class UserSyncRequest(BaseModel):
    clerk_user_id: str
    email: str
    username: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None

@router.post("/sync", response_model=UserResponse)
async def sync_clerk_user(
    payload: UserSyncRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Synchronize authenticated Clerk profile with AMETHEUS DB (Rule 21).
    Default role is always PLAYER.
    """
    result = await db.execute(
        select(User).where(
            or_(
                User.clerk_user_id == payload.clerk_user_id,
                User.id == payload.clerk_user_id,
                User.email == payload.email,
            )
        )
    )
    user = result.scalars().first()

    if user:
        # Update metadata without overwriting authoritative role
        user.clerk_user_id = payload.clerk_user_id
        if payload.display_name:
            user.display_name = payload.display_name
        if payload.avatar_url:
            user.avatar_url = payload.avatar_url
        await db.commit()
        await db.refresh(user)
    else:
        # Create new user with default PLAYER role
        user = User(
            id=payload.clerk_user_id,
            clerk_user_id=payload.clerk_user_id,
            email=payload.email,
            username=payload.username,
            display_name=payload.display_name or payload.username,
            avatar_url=payload.avatar_url,
            role="PLAYER",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    return user

@router.get("/me", response_model=UserResponse)
async def get_current_user_profile(
    current_user: User = Depends(get_current_user),
):
    """
    Fetch authoritative profile and role of authenticated user.
    """
    return current_user
