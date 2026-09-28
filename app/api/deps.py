from typing import Optional
from fastapi import Depends, HTTPException, status, Header
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.core.database import get_db
from app.models.user import User
from app.models.tournament import Tournament
from app.models.admin import TournamentAdminAssignment
from app.core.security import decode_token

async def get_current_user(
    authorization: Optional[str] = Header(None),
    db: AsyncIOMotorDatabase = Depends(get_db),
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

    from bson import ObjectId
    user_or = [{"_id": target_id}, {"clerk_user_id": target_id}, {"id": target_id}]
    if ObjectId.is_valid(str(target_id)):
        user_or.append({"_id": ObjectId(str(target_id))})

    # Authoritative DB lookup by clerk_user_id or _id / id
    user_doc = await db.users.find_one({"$or": user_or})

    email = payload.get("email") or payload.get("primary_email_address")
    if not user_doc and email:
        user_doc = await db.users.find_one({"email": email})
        if user_doc:
            await db.users.update_one({"_id": user_doc["_id"]}, {"$set": {"clerk_user_id": target_id}})
            user_doc = await db.users.find_one({"_id": user_doc["_id"]})

    if not user_doc:
        # If cryptographically verified by Clerk but not yet synced in DB, auto-provision
        # This prevents 401 race conditions on initial page load (e.g. /notifications, /leaderboard)
        user_email = (email or f"{target_id}@ametheus.gg").strip().lower()
        admin_emails = {"pk.4081988@gmail.com", "admin@ametheus.gg"}
        from app.core.config import settings
        admin_emails.add(settings.SUPER_ADMIN_EMAIL.lower())
        is_super_admin = user_email in admin_emails
        initial_role = "SUPER_ADMIN" if is_super_admin else "PLAYER"

        from app.models.base import get_utc_now
        now = get_utc_now()
        raw_username = payload.get("username") or payload.get("preferred_username") or target_id[:12]
        clean_user = raw_username.strip().lower().replace(" ", "_")

        set_on_insert_fields = {
            "role": initial_role,
            "username": clean_user,
            "username_lower": clean_user.lower(),
            "display_name": payload.get("name") or payload.get("username") or "Contender",
            "avatar_url": payload.get("picture"),
            "riot_id": None,
            "steam_id": None,
            "inr_balance_paise": 0,
            "inr_reserved_paise": 0,
            "inr_balance": "0.00",
            "is_dummy": False,
            "created_at": now,
        }
        set_fields = {
            "clerk_user_id": target_id,
            "email": user_email,
            "updated_at": now,
        }
        if is_super_admin:
            set_fields["role"] = "SUPER_ADMIN"

        try:
            await db.users.update_one(
                {"_id": target_id},
                {
                    "$set": set_fields,
                    "$setOnInsert": set_on_insert_fields,
                },
                upsert=True,
            )
        except Exception:
            # Another request won the concurrent provisioning race
            pass

        user_doc = await db.users.find_one({"$or": user_or})

    user = User.from_mongo(user_doc)
    admin_emails = {"pk.4081988@gmail.com", "admin@ametheus.gg"}
    if user.email and user.email.lower() in admin_emails:
        if user.role != "SUPER_ADMIN":
            user.role = "SUPER_ADMIN"
            await db.users.update_one({"_id": user.id}, {"$set": {"role": "SUPER_ADMIN"}})
    return user

async def get_current_user_optional(
    authorization: Optional[str] = Header(None),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> Optional[User]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        return await get_current_user(authorization=authorization, db=db)
    except HTTPException:
        return None

async def require_player(
    current_user: User = Depends(get_current_user),
) -> User:
    # All authenticated users have player privileges
    return current_user

async def require_organizer(
    current_user: User = Depends(get_current_user),
) -> User:
    admin_emails = {"pk.4081988@gmail.com", "admin@ametheus.gg"}
    if current_user.email and current_user.email.lower() in admin_emails:
        current_user.role = "SUPER_ADMIN"
        return current_user

    if current_user.role not in ["ORGANIZER", "SUPER_ADMIN"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Operation requires ORGANIZER or SUPER_ADMIN privilege",
        )
    return current_user


async def require_super_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    admin_emails = {"pk.4081988@gmail.com", "admin@ametheus.gg"}
    if current_user.email and current_user.email.lower() in admin_emails:
        current_user.role = "SUPER_ADMIN"
        return current_user

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
        db: AsyncIOMotorDatabase = Depends(get_db),
    ) -> User:
        admin_emails = {"pk.4081988@gmail.com", "admin@ametheus.gg"}
        if current_user.role == "SUPER_ADMIN" or (current_user.email and current_user.email.lower() in admin_emails):
            return current_user

        tourn_doc = await db.tournaments.find_one({"_id": id})
        if not tourn_doc:
            tourn_doc = await db.tournaments.find_one({"id": id})
        if not tourn_doc:
            raise HTTPException(status_code=404, detail="Tournament not found")

        # Check ownership
        if tourn_doc.get("organizer_id") == current_user.id:
            return current_user

        # Check assignment
        assign_doc = await db.tournament_admin_assignments.find_one({
            "admin_user_id": current_user.id,
            "tournament_id": id,
        })
        if assign_doc:
            return current_user

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: You do not own or have administrative rights for this tournament (Rule 20)",
        )

# Backward-compatible alias
RequireTournamentAdmin = RequireTournamentOwnershipOrAdmin

