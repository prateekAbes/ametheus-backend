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

    # Authoritative DB lookup by clerk_user_id or _id / id
    user_doc = await db.users.find_one({
        "$or": [{"_id": target_id}, {"clerk_user_id": target_id}, {"id": target_id}]
    })
    if not user_doc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: User not found in AMETHEUS. Please complete profile sync.",
        )

    user = User.from_mongo(user_doc)
    admin_emails = {"pk.4081988@gmail.com", "admin@ametheus.gg"}
    if user.email and user.email.lower() in admin_emails:
        if user.role != "SUPER_ADMIN":
            user.role = "SUPER_ADMIN"
            await db.users.update_one({"_id": user.id}, {"$set": {"role": "SUPER_ADMIN"}})
    return user

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

    if current_user.role not in ["ORGANIZER", "TOURNAMENT_ADMIN", "SUPER_ADMIN", "MATCH_ARBITER"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Operation requires ORGANIZER or TOURNAMENT_ADMIN privilege (Rule 9)",
        )
    return current_user

async def require_match_arbiter(
    current_user: User = Depends(get_current_user),
) -> User:
    admin_emails = {"pk.4081988@gmail.com", "admin@ametheus.gg"}
    if current_user.email and current_user.email.lower() in admin_emails:
        current_user.role = "SUPER_ADMIN"
        return current_user

    if current_user.role not in ["MATCH_ARBITER", "TOURNAMENT_ADMIN", "SUPER_ADMIN"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Operation requires MATCH_ARBITER, TOURNAMENT_ADMIN, or SUPER_ADMIN privilege",
        )
    return current_user

async def require_community_moderator(
    current_user: User = Depends(get_current_user),
) -> User:
    admin_emails = {"pk.4081988@gmail.com", "admin@ametheus.gg"}
    if current_user.email and current_user.email.lower() in admin_emails:
        current_user.role = "SUPER_ADMIN"
        return current_user

    if current_user.role not in ["COMMUNITY_MODERATOR", "TOURNAMENT_ADMIN", "SUPER_ADMIN"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Operation requires COMMUNITY_MODERATOR or SUPER_ADMIN privilege",
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

