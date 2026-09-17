from fastapi import APIRouter, Depends, HTTPException, Header, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, get_current_user
from app.core.security import decode_token
from app.models.user import User
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
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Synchronize authenticated Clerk profile with AMETHEUS DB in MongoDB.
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

    user_doc = await db.users.find_one({
        "$or": [
            {"clerk_user_id": payload.clerk_user_id},
            {"_id": payload.clerk_user_id},
            {"email": payload.email},
        ]
    })

    from app.core.config import settings
    admin_emails = {settings.SUPER_ADMIN_EMAIL.lower(), "admin@ametheus.gg", "pk.4081988@gmail.com"}
    is_super_admin = payload.email.lower() in admin_emails

    if user_doc:
        # Update metadata and ensure super admin role if designated email
        update_fields = {"clerk_user_id": payload.clerk_user_id}
        if payload.display_name:
            update_fields["display_name"] = payload.display_name
        if payload.avatar_url:
            update_fields["avatar_url"] = payload.avatar_url
        if is_super_admin:
            update_fields["role"] = "SUPER_ADMIN"
        await db.users.update_one({"_id": user_doc["_id"]}, {"$set": update_fields})
        updated_doc = await db.users.find_one({"_id": user_doc["_id"]})
        return User.from_mongo(updated_doc)
    else:
        # Assign SUPER_ADMIN for designated admin emails, PLAYER for everyone else
        initial_role = "SUPER_ADMIN" if is_super_admin else "PLAYER"
        user = User(
            id=payload.clerk_user_id,
            clerk_user_id=payload.clerk_user_id,
            email=payload.email,
            username=payload.username,
            display_name=payload.display_name or payload.username,
            avatar_url=payload.avatar_url,
            role=initial_role,
        )
        await db.users.insert_one(user.to_mongo())
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
        return user

@router.get("/me", response_model=UserResponse)
async def get_current_user_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Fetch authoritative profile and role of authenticated user.
    """
    admin_emails = {"admin@ametheus.gg", "pk.4081988@gmail.com"}
    if current_user.email and current_user.email.lower() in admin_emails:
        if current_user.role != "SUPER_ADMIN":
            current_user.role = "SUPER_ADMIN"
            await db.users.update_one({"_id": current_user.id}, {"$set": {"role": "SUPER_ADMIN"}})
    return current_user

class UserProfileUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    riot_id: Optional[str] = None
    steam_id: Optional[str] = None
    avatar_url: Optional[str] = None

@router.put("/profile", response_model=UserResponse)
async def update_user_profile(
    payload: UserProfileUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Update competitive player profile handles (Riot ID, Steam ID, Display Name, Avatar).
    """
    update_data = {}
    if payload.display_name is not None:
        update_data["display_name"] = payload.display_name.strip()
        current_user.display_name = update_data["display_name"]
    if payload.riot_id is not None:
        update_data["riot_id"] = payload.riot_id.strip()
        current_user.riot_id = update_data["riot_id"]
    if payload.steam_id is not None:
        update_data["steam_id"] = payload.steam_id.strip()
        current_user.steam_id = update_data["steam_id"]
    if payload.avatar_url is not None:
        update_data["avatar_url"] = payload.avatar_url.strip()
        current_user.avatar_url = update_data["avatar_url"]

    if update_data:
        await db.users.update_one({"_id": current_user.id}, {"$set": update_data})

    return current_user

