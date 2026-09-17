from fastapi import APIRouter, Depends, HTTPException, Header, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_
from app.api.deps import get_db, get_current_user
from app.core.security import decode_token
from app.models.user import User
from app.models.admin import AuditLog
from app.services.audit import record_audit_log
from app.schemas import UserResponse
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
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Synchronize authenticated Clerk profile with AMETHEUS DB.
    Requires verified Clerk Bearer token matching payload.clerk_user_id.
    Default role is always PLAYER.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required to sync user profile",
        )

    token = authorization.replace("Bearer ", "").strip()
    token_claims = decode_token(token)
    if not token_claims or "sub" not in token_claims:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token",
        )

    # Security check: Token subject must match the clerk_user_id being synced
    token_sub = token_claims["sub"]
    if token_sub != payload.clerk_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Token subject does not match profile ID",
        )

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
        from app.core.config import settings
        # Assign SUPER_ADMIN for the designated admin email, PLAYER for everyone else
        initial_role = "SUPER_ADMIN" if payload.email == settings.SUPER_ADMIN_EMAIL else "PLAYER"
        user = User(
            id=payload.clerk_user_id,
            clerk_user_id=payload.clerk_user_id,
            email=payload.email,
            username=payload.username,
            display_name=payload.display_name or payload.username,
            avatar_url=payload.avatar_url,
            role=initial_role,
        )
        db.add(user)
        await record_audit_log(
            db=db,
            operator=user.id,
            role=initial_role,
            action="USER_SYNCED",
            target=user.id,
            details=f"Clerk user {user.username} ({user.email}) registered as {initial_role}",
        )
        from app.services.notification import create_notification
        await create_notification(
            db=db,
            user_id=user.id,
            title="Welcome to AMETHEUS",
            message="Your competitive account is active. Register your team and join active tournament brackets.",
            notification_type="SYSTEM",
            link="/tournaments",
        )
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
