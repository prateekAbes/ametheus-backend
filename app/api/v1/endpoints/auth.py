import logging
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header, status, Request
import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from app.api.deps import get_db, get_current_user
from app.core.config import settings
from app.core.security import decode_token
from app.models.user import User
from app.services.audit import record_audit_log
from app.schemas import UserResponse

logger = logging.getLogger("ametheus.auth")

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

async def _generate_unique_placeholder(db: AsyncIOMotorDatabase, clerk_user_id: str) -> str:
    """Generate a collision-free temporary username handle like player_a1b2c3d4."""
    import re, secrets
    clean_id = re.sub(r'[^a-zA-Z0-9]', '', str(clerk_user_id)).lower()[-8:] or "contender"
    candidate = f"player_{clean_id}"

    # Check if candidate is already taken
    existing = await db.users.find_one({"username_lower": candidate.lower(), "_id": {"$ne": clerk_user_id}})
    if not existing:
        return candidate

    for _ in range(10):
        candidate = f"player_{clean_id[:4]}_{secrets.token_hex(2)}"
        existing = await db.users.find_one({"username_lower": candidate.lower(), "_id": {"$ne": clerk_user_id}})
        if not existing:
            return candidate

    return f"player_{secrets.token_hex(4)}"

class UserSyncRequest(BaseModel):
    clerk_user_id: str
    email: str
    username: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    avatar_source: Optional[str] = None
    google_avatar_url: Optional[str] = None
    has_chosen_username: Optional[bool] = None

@router.get("/check-username")
async def check_username_availability(
    username: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Validate real-time username availability, format rules, and reserved status.
    """
    clean = username.strip()
    if len(clean) < 3:
        return {"available": False, "username": clean, "message": "Username must be at least 3 characters."}
    if len(clean) > 30:
        return {"available": False, "username": clean, "message": "Username must be 30 characters or fewer."}
    import re
    if not re.match(r'^[a-zA-Z0-9_\-\.]+$', clean):
        return {"available": False, "username": clean, "message": "Letters, numbers, underscores, hyphens, and dots only."}
    if _is_reserved_username(clean):
        return {"available": False, "username": clean, "message": "This username is reserved."}

    existing = await db.users.find_one({"username_lower": clean.lower()})
    if existing:
        return {"available": False, "username": clean, "message": "Username is already taken."}

    return {"available": True, "username": clean, "message": "Username available"}

@router.post("/sync", response_model=UserResponse)
async def sync_clerk_user(
    payload: UserSyncRequest,
    request: Request,
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

    clean_email = payload.email.strip().lower()

    from app.core.config import settings
    admin_emails = {
        settings.SUPER_ADMIN_EMAIL.lower(),
        "admin@ametheus.gg",
        "pk.4081988@gmail.com",
    }
    token_metadata = token_claims.get("public_metadata") or token_claims.get("metadata") or {}
    token_role = str(token_metadata.get("role", "")).upper()
    is_super_admin = (clean_email in admin_emails) or (token_role in ["SUPER_ADMIN", "ADMIN"])

    # 1. Search for existing user records by Clerk user ID and/or email
    user_by_clerk = await db.users.find_one({
        "$or": [
            {"_id": payload.clerk_user_id},
            {"clerk_user_id": payload.clerk_user_id},
        ]
    })
    user_by_email = await db.users.find_one({
        "$or": [
            {"email": clean_email},
            {"email": payload.email.strip()},
        ]
    })

    from app.models.base import get_utc_now
    now = get_utc_now()

    if user_by_clerk and user_by_email and str(user_by_clerk["_id"]) != str(user_by_email["_id"]):
        # Merge duplicate accounts: preserve custom uploaded avatar and profile data
        merge_updates = {}
        for k in ["avatar_url", "avatar_source", "google_avatar_url", "display_name", "username", "username_lower", "has_chosen_username", "riot_id", "steam_id", "preferences", "privacy_settings"]:
            val = user_by_clerk.get(k)
            if val is not None and not user_by_email.get(k):
                merge_updates[k] = val
            elif val is not None and k == "avatar_url" and (str(val).startswith("/api/v1/media/") or str(val).startswith("http")):
                merge_updates[k] = val
        merge_updates["clerk_user_id"] = payload.clerk_user_id
        if merge_updates:
            await db.users.update_one({"_id": user_by_email["_id"]}, {"$set": merge_updates})
        await db.users.delete_one({"_id": user_by_clerk["_id"]})
        user_doc = await db.users.find_one({"_id": user_by_email["_id"]})
    else:
        user_doc = user_by_clerk or user_by_email

    if user_doc:
        target_id = user_doc["_id"]
        # Update permitted fields only — PROTECT application fields (balances, teams, etc.)
        update_fields = {
            "clerk_user_id": payload.clerk_user_id,
            "email": clean_email,
            "updated_at": now,
        }
        # Record incoming Google avatar if available
        incoming_google_avatar = payload.google_avatar_url or (
            payload.avatar_url if payload.avatar_url and not str(payload.avatar_url).startswith("/api/v1/media/") and not str(payload.avatar_url).startswith("/assets/crests/") else None
        )
        if incoming_google_avatar:
            update_fields["google_avatar_url"] = incoming_google_avatar.strip()

        # Only populate display_name if not already present in the user's database record
        if not user_doc.get("display_name") and payload.display_name:
            update_fields["display_name"] = payload.display_name.strip()

        # Avatar assignment & preservation rules:
        # Rule 1: If user already has an avatar_url, NEVER overwrite it with an incoming Google profile picture.
        # Rule 2: If user does not have an avatar_url, assign a random gaming avatar from active crests collection.
        # Rule 3: If an explicit avatar_source is passed (e.g. from profile update or user explicitly choosing GOOGLE / CREST / UPLOADED), respect it.
        # Rule 4: If payload has an uploaded media path (/api/v1/media/), respect it as UPLOADED.
        if not user_doc.get("avatar_url"):
            from app.services.avatar_service import get_random_default_avatar
            chosen_avatar, avatar_source = await get_random_default_avatar(db)
            update_fields["avatar_url"] = chosen_avatar
            update_fields["avatar_source"] = avatar_source
        elif payload.avatar_source:
            update_fields["avatar_source"] = payload.avatar_source.strip().upper()
            if payload.avatar_url:
                update_fields["avatar_url"] = payload.avatar_url.strip()
        elif payload.avatar_url and str(payload.avatar_url).startswith("/api/v1/media/"):
            update_fields["avatar_url"] = payload.avatar_url.strip()
            update_fields["avatar_source"] = "UPLOADED"

        # Enforce authoritative role
        if is_super_admin:
            update_fields["role"] = "SUPER_ADMIN"
        elif token_role in ["ORGANIZER", "TOURNAMENT_ADMIN"] and user_doc.get("role") != "SUPER_ADMIN":
            update_fields["role"] = "ORGANIZER"

        # Synchronize canonical username from Clerk authentication context
        existing_username = (user_doc.get("username") or "").strip()
        clean_incoming_username = (payload.username or "").strip()

        # If existing record has an empty or missing username, heal it with a guaranteed placeholder
        if not existing_username:
            placeholder = await _generate_unique_placeholder(db, target_id)
            update_fields["username"] = placeholder
            update_fields["username_lower"] = placeholder.lower()
            update_fields["has_chosen_username"] = False
            update_fields["needs_username"] = True
            existing_username = placeholder

        if clean_incoming_username:
            import re
            is_valid_format = bool(re.match(r"^[a-zA-Z0-9_\-\.]+$", clean_incoming_username)) and 3 <= len(clean_incoming_username) <= 30
            is_not_reserved = not _is_reserved_username(clean_incoming_username) or is_super_admin
            
            if is_valid_format and is_not_reserved:
                # Check if incoming username is claimed by a different user
                collision = await db.users.find_one({
                    "username_lower": clean_incoming_username.lower(),
                    "_id": {"$ne": target_id},
                    "clerk_user_id": {"$ne": payload.clerk_user_id},
                })
                is_placeholder = (
                    existing_username.lower().startswith("player_")
                    or user_doc.get("has_chosen_username") is False
                    or user_doc.get("needs_username") is True
                )
                if not collision and (is_placeholder or clean_incoming_username.lower() != existing_username.lower() or clean_incoming_username != existing_username):
                    update_fields["username"] = clean_incoming_username
                    update_fields["username_lower"] = clean_incoming_username.lower()
                    update_fields["has_chosen_username"] = True
                    update_fields["needs_username"] = False
                elif not collision and clean_incoming_username != existing_username:
                    update_fields["username"] = clean_incoming_username
                    update_fields["username_lower"] = clean_incoming_username.lower()
                elif existing_username:
                    update_fields["username_lower"] = existing_username.lower()
            elif existing_username:
                update_fields["username_lower"] = existing_username.lower()
        elif existing_username:
            update_fields["username_lower"] = existing_username.lower()

        await db.users.update_one({"_id": target_id}, {"$set": update_fields})
        updated_doc = await db.users.find_one({"_id": target_id})
        return User.from_mongo(updated_doc)

    # 2. New user flow — Validate username and prepare atomic upsert
    raw_username = (payload.username or "").strip().replace(" ", "_")
    is_explicitly_unassigned = (payload.has_chosen_username is False) or (not raw_username) or (len(raw_username) < 3)

    import re
    is_valid_format = bool(re.match(r"^[a-zA-Z0-9_\-\.]+$", raw_username)) and 3 <= len(raw_username) <= 30
    is_not_reserved = not _is_reserved_username(raw_username.lower()) or is_super_admin

    if not is_explicitly_unassigned and is_valid_format and is_not_reserved:
        clean_username = raw_username
        clean_username_lower = clean_username.lower()
        # Check collision with other users
        username_taken = await db.users.find_one({
            "username_lower": clean_username_lower,
            "_id": {"$ne": payload.clerk_user_id},
        })
        if username_taken:
            clean_username = f"{clean_username[:18]}_{payload.clerk_user_id[-6:].lower()}"
            clean_username_lower = clean_username.lower()
            # Double check if still taken
            if await db.users.find_one({"username_lower": clean_username_lower, "_id": {"$ne": payload.clerk_user_id}}):
                import secrets
                clean_username = f"{clean_username[:14]}_{secrets.token_hex(2)}"
                clean_username_lower = clean_username.lower()
        has_chosen = True
    else:
        # Username was empty, invalid, reserved, or has_chosen_username is False (e.g., fresh Google OAuth signup)
        clean_username = await _generate_unique_placeholder(db, payload.clerk_user_id)
        clean_username_lower = clean_username.lower()
        has_chosen = False

    initial_role = "SUPER_ADMIN" if is_super_admin else ("ORGANIZER" if token_role in ["ORGANIZER", "TOURNAMENT_ADMIN"] else "PLAYER")

    from app.services.avatar_service import get_random_default_avatar
    chosen_avatar, avatar_source = await get_random_default_avatar(db)
    google_pic = payload.google_avatar_url or (
        payload.avatar_url if payload.avatar_url and not str(payload.avatar_url).startswith("/api/v1/media/") and not str(payload.avatar_url).startswith("/assets/crests/") else None
    )

    # Application fields set ONLY upon initial insertion
    set_on_insert_fields = {
        "role": initial_role,
        "username": clean_username,
        "username_lower": clean_username_lower,
        "has_chosen_username": has_chosen,
        "needs_username": not has_chosen,
        "display_name": (payload.display_name or "").strip() or clean_username,
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

    # Permitted sync fields
    set_fields = {
        "clerk_user_id": payload.clerk_user_id,
        "email": clean_email,
        "updated_at": now,
    }
    if google_pic:
        set_fields["google_avatar_url"] = google_pic
    if is_super_admin:
        set_fields["role"] = "SUPER_ADMIN"

    is_newly_created = False
    try:
        # Atomic update with upsert=True eliminates the find-then-insert race condition
        upsert_res = await db.users.update_one(
            {"_id": payload.clerk_user_id},
            {
                "$set": set_fields,
                "$setOnInsert": set_on_insert_fields,
            },
            upsert=True,
        )
        is_newly_created = (upsert_res.upserted_id is not None)
    except Exception as exc:
        import logging
        logging.getLogger("ametheus.auth").warning(f"Upsert collision retry triggered for {payload.clerk_user_id}: {exc}")
        clean_username = await _generate_unique_placeholder(db, payload.clerk_user_id)
        clean_username_lower = clean_username.lower()
        set_on_insert_fields["username"] = clean_username
        set_on_insert_fields["username_lower"] = clean_username_lower
        try:
            upsert_res = await db.users.update_one(
                {"_id": payload.clerk_user_id},
                {
                    "$set": set_fields,
                    "$setOnInsert": set_on_insert_fields,
                },
                upsert=True,
            )
            is_newly_created = (upsert_res.upserted_id is not None)
        except Exception:
            is_newly_created = False

    # Fetch the authoritative document
    final_doc = await db.users.find_one({
        "$or": [
            {"_id": payload.clerk_user_id},
            {"clerk_user_id": payload.clerk_user_id},
            {"email": clean_email},
        ]
    })

    if not final_doc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve user profile after synchronization.",
        )

    # Trigger audit log for fresh registrations
    if is_newly_created:
        try:
            await record_audit_log(
                db=db,
                operator=payload.clerk_user_id,
                role=initial_role,
                action="USER_SYNCED",
                target=payload.clerk_user_id,
                details=f"Clerk user {clean_username} ({clean_email}) registered as {initial_role}",
            )
        except Exception:
            pass

    # Ensure welcome notification is issued reliably for any new user or user lacking it
    try:
        from app.services.notification import ensure_welcome_notification
        await ensure_welcome_notification(db=db, user_id=payload.clerk_user_id)
    except Exception:
        pass

    # Login Security & IP Detection
    try:
        from app.services.login_security import process_login_security_check, get_client_ip
        client_ip = get_client_ip(request)

        if is_newly_created:
            await db.users.update_one(
                {"_id": final_doc["_id"]},
                {
                    "$set": {
                        "known_ips": [client_ip],
                        "last_login_ip": client_ip,
                        "last_login_at": now,
                    }
                }
            )
        else:
            await process_login_security_check(db=db, user_doc=final_doc, request=request)
    except Exception as e:
        import logging
        logging.getLogger("ametheus.auth").warning(f"Login security check error: {e}")

    # Re-fetch after security IP updates
    fresh_user_doc = await db.users.find_one({"_id": final_doc["_id"]})
    return User.from_mongo(fresh_user_doc or final_doc)

@router.get("/me", response_model=UserResponse)
async def get_current_user_profile(
    request: Request,
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

    try:
        from app.services.login_security import process_login_security_check
        user_doc = await db.users.find_one({"$or": [{"_id": current_user.id}, {"id": current_user.id}]})
        if user_doc:
            await process_login_security_check(db=db, user_doc=user_doc, request=request)
            fresh_doc = await db.users.find_one({"_id": user_doc["_id"]})
            if fresh_doc:
                return User.from_mongo(fresh_doc)
    except Exception:
        pass

    return current_user

class UserProfileUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    username: Optional[str] = None
    riot_id: Optional[str] = None
    steam_id: Optional[str] = None
    avatar_url: Optional[str] = None
    avatar_source: Optional[str] = None
    google_avatar_url: Optional[str] = None
    preferences: Optional[dict] = None
    privacy_settings: Optional[dict] = None

@router.put("/profile", response_model=UserResponse)
@router.patch("/profile", response_model=UserResponse)
@router.put("/me", response_model=UserResponse)
@router.patch("/me", response_model=UserResponse)
async def update_user_profile(
    payload: UserProfileUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Update competitive player profile handles (Username, Display Name, Riot ID, Steam ID, Avatar, Preferences, Privacy).
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
        update_data["has_chosen_username"] = True
        update_data["needs_username"] = False
        current_user.username = new_username
        current_user.has_chosen_username = True
        current_user.needs_username = False

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
    if payload.avatar_source is not None:
        update_data["avatar_source"] = payload.avatar_source.strip().upper()
        current_user.avatar_source = update_data["avatar_source"]
    if payload.google_avatar_url is not None:
        update_data["google_avatar_url"] = payload.google_avatar_url.strip()
        current_user.google_avatar_url = update_data["google_avatar_url"]
    if payload.preferences is not None:
        update_data["preferences"] = payload.preferences
        current_user.preferences = payload.preferences
    if payload.privacy_settings is not None:
        update_data["privacy_settings"] = payload.privacy_settings
        current_user.privacy_settings = payload.privacy_settings

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


async def delete_clerk_user(clerk_user_id: str) -> bool:
    """
    Deletes the user identity from Clerk using Clerk's Backend API and CLERK_SECRET_KEY.
    Returns True on success or if user is already deleted (404).
    Raises HTTPException if Clerk returns an unexpected error.
    """
    if not settings.CLERK_SECRET_KEY or not settings.CLERK_SECRET_KEY.strip():
        logger.warning("[AUTH] CLERK_SECRET_KEY not configured, skipping remote Clerk user deletion.")
        return True

    clerk_api_url = f"https://api.clerk.com/v1/users/{clerk_user_id}"
    headers = {
        "Authorization": f"Bearer {settings.CLERK_SECRET_KEY.strip()}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(clerk_api_url, headers=headers)
            if resp.status_code in (200, 204):
                logger.info(f"[AUTH] Successfully deleted Clerk user {clerk_user_id}")
                return True
            elif resp.status_code == 404:
                logger.info(f"[AUTH] Clerk user {clerk_user_id} already deleted from Clerk (404)")
                return True
            else:
                logger.error(f"[AUTH] Clerk API returned HTTP {resp.status_code} during user deletion: {resp.text}")
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Authentication identity provider failed to delete user account. Please try again.",
                )
    except httpx.RequestError as exc:
        logger.error(f"[AUTH] Network error communicating with Clerk during user deletion: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Authentication identity provider network error. Please try again.",
        )


@router.delete("/me")
@router.delete("/account")
async def delete_user_account(
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Permanently delete user account from MongoDB, synchronize with Clerk server-side,
    safely clean up dependent relationships and wallet balances, and record audit log.
    """
    from bson import ObjectId

    # 1. Authoritative lookup of target user document
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
        # Idempotent response if account was already removed
        return {"status": "success", "message": "Account already deleted."}

    target_mongo_id = target_user_doc["_id"]

    # 2. Financial & Wallet Safety Invariants
    # Check if user has an unwithdrawn wallet balance
    avail_paise = int(target_user_doc.get("inr_balance_paise", 0))
    reserved_paise = int(target_user_doc.get("inr_reserved_paise", 0))
    if avail_paise == 0 and target_user_doc.get("inr_balance"):
        try:
            avail_paise = int(float(target_user_doc["inr_balance"]) * 100)
        except Exception:
            avail_paise = 0

    total_paise = avail_paise + reserved_paise
    if total_paise > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete account with an active wallet balance (₹{total_paise / 100:.2f}). Please withdraw or settle all remaining funds before deleting your account.",
        )

    # Check for pending payments/transactions
    user_ids_to_clean = list(set([
        str(current_user.id),
        str(target_mongo_id),
        str(target_user_doc.get("id", "")),
    ] + ([str(current_user.clerk_user_id)] if current_user.clerk_user_id else [])))
    user_ids_to_clean = [uid for uid in user_ids_to_clean if uid]

    pending_payment = await db.payments.find_one({
        "user_id": {"$in": user_ids_to_clean},
        "status": {"$in": ["INITIATED", "PENDING", "SUBMITTED"]},
    })
    if not pending_payment:
        pending_payment = await db.game_payments.find_one({
            "user_id": {"$in": user_ids_to_clean},
            "status": {"$in": ["INITIATED", "PENDING", "SUBMITTED"]},
        })
    if pending_payment:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete account with pending payment transactions. Please wait for pending transactions to complete or contact support.",
        )

    # 3. Clean up or de-identify dependent application records
    # 3a. Remove user from team memberships and substitutes
    await db.teams.update_many(
        {"members.user_id": {"$in": user_ids_to_clean}},
        {"$pull": {"members": {"user_id": {"$in": user_ids_to_clean}}}},
    )
    await db.teams.update_many(
        {"substitutes.user_id": {"$in": user_ids_to_clean}},
        {"$pull": {"substitutes": {"user_id": {"$in": user_ids_to_clean}}}},
    )

    # 3b. Handle team captaincies
    captain_teams = db.teams.find({
        "captain_id": {"$in": user_ids_to_clean},
        "is_disbanded": {"$ne": True},
    })
    async for team in captain_teams:
        team_id = team["_id"]
        remaining_members = [
            m for m in team.get("members", [])
            if (m.get("user_id") if isinstance(m, dict) else getattr(m, "user_id", None)) not in user_ids_to_clean
        ]
        if remaining_members:
            new_captain_id = remaining_members[0].get("user_id") if isinstance(remaining_members[0], dict) else remaining_members[0].user_id
            for m in remaining_members:
                if (m.get("user_id") if isinstance(m, dict) else getattr(m, "user_id", None)) == new_captain_id:
                    if isinstance(m, dict):
                        m["role"] = "CAPTAIN"
                    else:
                        m.role = "CAPTAIN"
            await db.teams.update_one(
                {"_id": team_id},
                {
                    "$set": {
                        "captain_id": new_captain_id,
                        "members": remaining_members,
                    }
                },
            )
        else:
            await db.teams.update_one(
                {"_id": team_id},
                {
                    "$set": {
                        "status": "DISBANDED",
                        "is_disbanded": True,
                        "is_ineligible": True,
                        "is_deleted_account": True,
                        "disbanded_at": datetime.now(timezone.utc),
                        "disbanded_by": current_user.username or "deleted_user",
                    }
                },
            )

    # 3c. Clean up custom leaderboard entries for deleted user
    await db.custom_leaderboard_entries.delete_many({
        "$or": [
            {"captain_id": {"$in": user_ids_to_clean}},
            {"user_id": {"$in": user_ids_to_clean}},
            {"entity_id": {"$in": user_ids_to_clean}},
        ]
    })

    # 3d. Delete user notifications
    await db.notifications.delete_many({"user_id": {"$in": user_ids_to_clean}})

    # 3e. Invalidate user sessions
    await db.user_sessions.delete_many({
        "$or": [
            {"user_id": {"$in": user_ids_to_clean}},
            {"clerk_user_id": current_user.clerk_user_id},
        ]
    })

    # 3e. De-identify support tickets
    await db.support_tickets.update_many(
        {"user_id": {"$in": user_ids_to_clean}},
        {
            "$set": {
                "user_email": "deleted_user@ametheus.gg",
                "user_name": "Deleted Contender",
                "is_user_deleted": True,
            }
        },
    )

    # 4. Clerk Server-side Identity Deletion
    clerk_id = current_user.clerk_user_id or target_user_doc.get("clerk_user_id")
    if clerk_id:
        await delete_clerk_user(clerk_id)

    # 5. Record Audit Log before final purge
    await record_audit_log(
        db=db,
        operator=current_user.username or "deleted_user",
        role=current_user.role,
        action="DELETE_ACCOUNT",
        target=f"User {current_user.id} ({current_user.email})",
        details="User permanently deleted their own account.",
    )

    # 6. Final MongoDB user purge
    await db.users.delete_many({"$or": query_clauses})

    return {"status": "success", "message": "Account successfully deleted."}

