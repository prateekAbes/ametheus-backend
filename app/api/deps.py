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
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
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

    raw_email = payload.get("email") or payload.get("primary_email_address") or x_user_email
    clean_email = raw_email.strip().lower() if raw_email else None

    from app.core.config import settings
    admin_emails = {"pk.4081988@gmail.com", "admin@ametheus.gg"}
    if settings.SUPER_ADMIN_EMAIL:
        admin_emails.add(settings.SUPER_ADMIN_EMAIL.lower())

    token_metadata = payload.get("public_metadata") or payload.get("metadata") or {}
    token_role = str(token_metadata.get("role", "")).upper()

    is_placeholder = bool(
        clean_email and (
            clean_email.startswith(target_id.lower())
            or (clean_email.startswith("user_") and clean_email.endswith("@ametheus.gg"))
        )
    )

    # If user document not yet matched by clerk_user_id, search by clean_email
    if not user_doc and clean_email and "@" in clean_email and not is_placeholder:
        user_doc = await db.users.find_one({"email": clean_email})
        if user_doc:
            await db.users.update_one({"_id": user_doc["_id"]}, {"$set": {"clerk_user_id": target_id}})
            user_doc = await db.users.find_one({"_id": user_doc["_id"]})

    if not user_doc:
        # If cryptographically verified by Clerk but not yet synced in DB, auto-provision
        user_email = (clean_email or f"{target_id}@ametheus.gg").strip().lower()
        is_super_admin = (user_email in admin_emails) or (token_role in ["SUPER_ADMIN", "ADMIN"])
        initial_role = "SUPER_ADMIN" if is_super_admin else ("ORGANIZER" if token_role in ["ORGANIZER", "TOURNAMENT_ADMIN"] else "PLAYER")

        from app.models.base import get_utc_now
        now = get_utc_now()
        raw_username = payload.get("username") or payload.get("preferred_username")
        raw_clean = (raw_username or "").strip().replace(" ", "_")
        
        import re, secrets
        is_valid = bool(re.match(r"^[a-zA-Z0-9_\-\.]+$", raw_clean)) and 3 <= len(raw_clean) <= 30
        if is_valid:
            collision = await db.users.find_one({"username_lower": raw_clean.lower(), "_id": {"$ne": target_id}})
            if collision:
                clean_user = f"{raw_clean[:18]}_{secrets.token_hex(2)}"
            else:
                clean_user = raw_clean
            clean_user_lower = clean_user.lower()
            has_chosen = True
        else:
            clean_id = re.sub(r'[^a-zA-Z0-9]', '', str(target_id)).lower()[-8:] or "contender"
            candidate = f"player_{clean_id}"
            while await db.users.find_one({"username_lower": candidate.lower(), "_id": {"$ne": target_id}}):
                candidate = f"player_{clean_id[:4]}_{secrets.token_hex(2)}"
            clean_user = candidate
            clean_user_lower = candidate.lower()
            has_chosen = False

        from app.services.avatar_service import get_random_default_avatar
        chosen_avatar, avatar_source = await get_random_default_avatar(db)
        google_pic = payload.get("picture")

        set_on_insert_fields = {
            "role": initial_role,
            "username": clean_user,
            "username_lower": clean_user_lower,
            "has_chosen_username": has_chosen,
            "needs_username": not has_chosen,
            "display_name": payload.get("name") or clean_user or "Contender",
            "avatar_url": chosen_avatar,
            "avatar_source": avatar_source,
            "google_avatar_url": google_pic,
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
        if google_pic:
            set_fields["google_avatar_url"] = google_pic
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
            # Another request won the concurrent provisioning race, or retry with unique random suffix
            candidate = f"player_{secrets.token_hex(4)}"
            set_on_insert_fields["username"] = candidate
            set_on_insert_fields["username_lower"] = candidate.lower()
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
                pass

        user_doc = await db.users.find_one({"$or": user_or})

    # If user document has a placeholder email and a real email is now available, upgrade it
    if user_doc and clean_email and "@" in clean_email and not is_placeholder:
        current_email = str(user_doc.get("email", "")).strip().lower()
        curr_is_placeholder = bool(
            current_email.startswith(target_id.lower())
            or (current_email.startswith("user_") and current_email.endswith("@ametheus.gg"))
        )
        if not current_email or curr_is_placeholder:
            try:
                await db.users.update_one({"_id": user_doc["_id"]}, {"$set": {"email": clean_email}})
                user_doc["email"] = clean_email
            except Exception:
                pass

    if not user_doc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account could not be found or provisioned",
        )

    user = User.from_mongo(user_doc)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Failed to load user profile",
        )

    user_curr_email = (user.email or clean_email or "").strip().lower()

    is_super_admin = (
        (user_curr_email in admin_emails) or
        (clean_email and clean_email in admin_emails) or
        token_role in ["SUPER_ADMIN", "ADMIN"]
    )

    if is_super_admin:
        user.role = "SUPER_ADMIN"
        update_fields: dict = {"role": "SUPER_ADMIN"}
        if clean_email and not user_doc.get("email"):
            update_fields["email"] = clean_email
        await db.users.update_one({"_id": user_doc["_id"]}, {"$set": update_fields})
    elif token_role in ["ORGANIZER", "TOURNAMENT_ADMIN"] and user.role != "SUPER_ADMIN":
        user.role = "ORGANIZER"
        await db.users.update_one({"_id": user_doc["_id"]}, {"$set": {"role": "ORGANIZER"}})

    return user

async def get_current_user_optional(
    authorization: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> Optional[User]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        return await get_current_user(authorization=authorization, x_user_email=x_user_email, db=db)
    except Exception:
        return None

async def require_player(
    current_user: User = Depends(get_current_user),
) -> User:
    # All authenticated users have player privileges
    return current_user

def normalize_backend_role(role_val: Optional[str]) -> str:
    if not role_val:
        return "PLAYER"
    clean = str(role_val).strip().upper().replace("-", "_").replace(" ", "_")
    if clean in ["SUPER_ADMIN", "SUPERADMIN", "ADMIN", "ROOT"]:
        return "SUPER_ADMIN"
    if clean in ["ORGANIZER", "TOURNAMENT_ADMIN", "MATCH_ARBITER", "COMMUNITY_MODERATOR", "MODERATOR"]:
        return "ORGANIZER"
    return "PLAYER"

async def require_organizer(
    current_user: User = Depends(get_current_user),
) -> User:
    from app.core.config import settings
    admin_emails = {"pk.4081988@gmail.com", "admin@ametheus.gg"}
    if settings.SUPER_ADMIN_EMAIL:
        admin_emails.add(settings.SUPER_ADMIN_EMAIL.lower())
    if current_user.email and current_user.email.lower() in admin_emails:
        current_user.role = "SUPER_ADMIN"
        return current_user

    norm_role = normalize_backend_role(current_user.role)
    if norm_role not in ["ORGANIZER", "SUPER_ADMIN"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Operation requires ORGANIZER or SUPER_ADMIN privilege",
        )
    return current_user


async def require_super_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    from app.core.config import settings
    admin_emails = {"pk.4081988@gmail.com", "admin@ametheus.gg"}
    if settings.SUPER_ADMIN_EMAIL:
        admin_emails.add(settings.SUPER_ADMIN_EMAIL.lower())
    if current_user.email and current_user.email.lower() in admin_emails:
        current_user.role = "SUPER_ADMIN"
        return current_user

    norm_role = normalize_backend_role(current_user.role)
    if norm_role != "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Operation requires SUPER_ADMIN privilege (Rule 10)",
        )
    current_user.role = "SUPER_ADMIN"
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

