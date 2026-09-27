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

# Usernames that are permanently reserved — case-insensitive
RESERVED_USERNAMES = {"admin", "administrator", "ametheus", "superadmin", "super_admin", "mod", "moderator", "support", "staff", "system", "root", "owner"}

def _is_reserved_username(username: str) -> bool:
    """Returns True if the username is a reserved/forbidden name in any casing variant or formatting."""
    clean = username.strip().lower()
    if clean in RESERVED_USERNAMES:
        return True
    normalized = clean.replace(" ", "").replace("_", "").replace("-", "").replace(".", "")
    if normalized in {"admin", "administrator", "ametheus", "superadmin", "system", "support", "staff", "root"}:
        return True
    return False

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

    # Validate username — reserved names are only allowed for super-admin emails
    clean_username = payload.username.strip().lower().replace(" ", "_")
    if _is_reserved_username(clean_username) and not is_super_admin:
        # Assign a safe username instead of failing sync (Clerk may pass 'admin' as username)
        clean_username = f"player_{payload.clerk_user_id[-6:].lower()}"

    if user_doc:
        # Update metadata and ensure super admin role if designated email
        update_fields = {"clerk_user_id": payload.clerk_user_id}
        # Only populate display_name if not already present in the user's database record
        if not user_doc.get("display_name") and payload.display_name:
            update_fields["display_name"] = payload.display_name
        # Only populate avatar_url if not already present in the user's database record (preserve custom user avatars)
        if not user_doc.get("avatar_url") and payload.avatar_url:
            update_fields["avatar_url"] = payload.avatar_url
        if is_super_admin:
            update_fields["role"] = "SUPER_ADMIN"
        # Keep username_lower in sync
        existing_username = user_doc.get("username", "")
        update_fields["username_lower"] = existing_username.lower()
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
            username=clean_username,
            display_name=payload.display_name or clean_username,
            avatar_url=payload.avatar_url,
            role=initial_role,
        )
        user_dict = user.to_mongo()
        user_dict["username_lower"] = clean_username.lower()
        await db.users.insert_one(user_dict)
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
    username: Optional[str] = None
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
    Update competitive player profile handles (Username, Display Name, Riot ID, Steam ID, Avatar).
    Username changes are validated for case-insensitive uniqueness and reserved name restrictions.
    """
    update_data = {}

    if payload.username is not None:
        new_username = payload.username.strip()
        new_username_lower = new_username.lower()

        if len(new_username) < 3:
            raise HTTPException(status_code=400, detail="Username must be at least 3 characters.")
        if len(new_username) > 30:
            raise HTTPException(status_code=400, detail="Username must be 30 characters or fewer.")
        import re
        if not re.match(r'^[a-zA-Z0-9_\-\.]+$', new_username):
            raise HTTPException(status_code=400, detail="Username can only contain letters, numbers, underscores, hyphens, and dots.")

        # Enforce admin username reservation — non-admins cannot claim reserved names
        admin_emails_set = {"admin@ametheus.gg", "pk.4081988@gmail.com"}
        is_admin = current_user.email and current_user.email.lower() in admin_emails_set or current_user.role == "SUPER_ADMIN"
        if _is_reserved_username(new_username_lower) and not is_admin:
            raise HTTPException(status_code=400, detail=f"The username '{new_username}' is reserved and cannot be used.")

        # Case-insensitive uniqueness check
        existing = await db.users.find_one({
            "username_lower": new_username_lower,
            "_id": {"$ne": current_user.id},
        })
        if existing:
            raise HTTPException(status_code=409, detail=f"Username '{new_username}' is already taken. Please choose a different one.")

        update_data["username"] = new_username
        update_data["username_lower"] = new_username_lower
        current_user.username = new_username

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
        from bson import ObjectId

        # Resolve authoritative document in MongoDB preserving its native _id type
        query_clauses = [
            {"_id": current_user.id},
            {"id": current_user.id},
        ]
        if current_user.clerk_user_id:
            query_clauses.extend([
                {"clerk_user_id": current_user.clerk_user_id},
                {"_id": current_user.clerk_user_id},
            ])
        if current_user.email:
            query_clauses.append({"email": current_user.email})
        if ObjectId.is_valid(str(current_user.id)):
            query_clauses.append({"_id": ObjectId(str(current_user.id))})

        target_user_doc = await db.users.find_one({"$or": query_clauses})
        if not target_user_doc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User account record not found in database",
            )

        target_mongo_id = target_user_doc["_id"]

        update_res = await db.users.update_one(
            {"_id": target_mongo_id},
            {"$set": update_data}
        )
        if update_res.matched_count == 0:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to persist profile update to database",
            )

        fresh_doc = await db.users.find_one({"_id": target_mongo_id})
        return User.from_mongo(fresh_doc)

    return current_user

