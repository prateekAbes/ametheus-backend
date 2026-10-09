from fastapi import APIRouter, Depends, HTTPException, Header, status, Request
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
        # Merge duplicate accounts: keep the pre-existing email record, remove the orphaned auto-provisioned placeholder
        await db.users.delete_one({"_id": user_by_clerk["_id"]})
        user_doc = user_by_email
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
        # Only populate display_name if not already present in the user's database record
        if not user_doc.get("display_name") and payload.display_name:
            update_fields["display_name"] = payload.display_name.strip()
        # Only populate avatar_url if not already present in the user's database record
        if not user_doc.get("avatar_url") and payload.avatar_url:
            update_fields["avatar_url"] = payload.avatar_url.strip()
        # Enforce authoritative role
        if is_super_admin:
            update_fields["role"] = "SUPER_ADMIN"
        elif token_role in ["ORGANIZER", "TOURNAMENT_ADMIN"] and user_doc.get("role") != "SUPER_ADMIN":
            update_fields["role"] = "ORGANIZER"

        # Synchronize canonical username from Clerk authentication context
        existing_username = user_doc.get("username", "")
        clean_incoming_username = payload.username.strip() if payload.username else ""
        
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
                    not existing_username
                    or existing_username.lower().startswith("player_")
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
    clean_username = payload.username.strip().replace(" ", "_")
    clean_username_lower = clean_username.lower()
    if _is_reserved_username(clean_username_lower) and not is_super_admin:
        clean_username = f"player_{payload.clerk_user_id[-6:].lower()}"
        clean_username_lower = clean_username.lower()

    # Check for username collisions with other existing users
    username_taken = await db.users.find_one({
        "username_lower": clean_username_lower,
        "_id": {"$ne": payload.clerk_user_id},
    })
    if username_taken:
        clean_username = f"{clean_username[:18]}_{payload.clerk_user_id[-6:].lower()}"
        clean_username_lower = clean_username.lower()

    initial_role = "SUPER_ADMIN" if is_super_admin else ("ORGANIZER" if token_role in ["ORGANIZER", "TOURNAMENT_ADMIN"] else "PLAYER")

    # Application fields set ONLY upon initial insertion
    has_chosen = payload.has_chosen_username if payload.has_chosen_username is not None else True
    set_on_insert_fields = {
        "role": initial_role,
        "username": clean_username,
        "username_lower": clean_username_lower,
        "has_chosen_username": has_chosen,
        "needs_username": not has_chosen,
        "display_name": payload.display_name or clean_username,
        "avatar_url": payload.avatar_url,
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
    except Exception:
        # In case a concurrent request completed the insert/upsert simultaneously
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

@router.delete("/me")
@router.delete("/account")
async def delete_user_account(
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Permanently delete user account and orchestrate complete, reliable data cleanup:
    1. Verify server-side authenticated user context.
    2. Prevent concurrent / duplicate deletion calls.
    3. Reconcile wallet ledger: zero out balance, cancel pending withdrawals, de-identify transaction records.
    4. Handle team memberships:
       - Disband solo teams.
       - Promote next member to captain for multi-member teams.
       - Remove user from other teams' members and substitutes rosters.
    5. Cancel upcoming tournament registrations and de-identify historical ones.
    6. Clean up custom leaderboard entries.
    7. Remove user notifications and anonymize reviews / support tickets.
    8. Delete user document from MongoDB.
    9. Call Clerk Backend API to delete user if CLERK_SECRET_KEY is configured.
    10. Log audit trail.
    """
    from bson import ObjectId
    from app.core.config import settings
    from app.models.base import get_utc_now

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

    # Collect all possible identifier strings for this user to ensure complete relational cleanup
    user_identifiers = {str(target_mongo_id), str(current_user.id)}
    if current_user.clerk_user_id:
        user_identifiers.add(str(current_user.clerk_user_id))
    if target_user_doc.get("clerk_user_id"):
        user_identifiers.add(str(target_user_doc["clerk_user_id"]))
    if target_user_doc.get("id"):
        user_identifiers.add(str(target_user_doc["id"]))

    user_ids_list = list(user_identifiers)
    now = get_utc_now()

    # 1. FINANCIAL RECONCILIATION & WALLET SETTLEMENT
    try:
        # A. Cancel any in-flight pending withdrawal requests
        await db.payouts.update_many(
            {"user_id": {"$in": user_ids_list}, "status": "PENDING"},
            {"$set": {
                "status": "CANCELLED",
                "rejection_reason": "Account deleted by user",
                "updated_at": now,
            }}
        )

        # B. Retain financial transaction records for audit/accounting, but de-identify personal info
        await db.payouts.update_many(
            {"user_id": {"$in": user_ids_list}},
            {"$set": {
                "user_name": "Deleted Contender",
                "upi_id": "REDACTED",
            }}
        )
        await db.game_payments.update_many(
            {"user_id": {"$in": user_ids_list}},
            {"$set": {
                "user_name": "Deleted Contender",
            }}
        )

        # C. Wallet ledger: record final closing entry if non-zero balance existed
        current_avail_paise = int(target_user_doc.get("inr_balance_paise", 0))
        if current_avail_paise > 0:
            from app.models.payment import WalletLedger
            closing_ledger = WalletLedger(
                user_id=str(target_mongo_id),
                txn_type="ACCOUNT_DELETION_SETTLEMENT",
                amount_paise=-current_avail_paise,
                balance_before_paise=current_avail_paise,
                balance_after_paise=0,
                description="Wallet balance zeroed and account closed upon user account deletion",
            )
            await db.wallet_ledger.insert_one(closing_ledger.to_mongo())

        # Anonymize ledger user reference while preserving financial numbers
        await db.wallet_ledger.update_many(
            {"user_id": {"$in": user_ids_list}},
            {"$set": {"user_id": f"deleted_{str(target_mongo_id)[:8]}"}}
        )
    except Exception as fin_err:
        import logging
        logging.getLogger("ametheus.auth").warning(f"Wallet settlement cleanup note: {fin_err}")

    # 2. TEAM MEMBERSHIPS & CAPTAINCIES
    try:
        def _extract_id(item):
            if isinstance(item, str):
                return item
            if isinstance(item, dict):
                return item.get("user_id") or item.get("id") or item.get("_id")
            return getattr(item, "user_id", getattr(item, "id", None))

        # Teams where user is captain
        cap_teams_cursor = db.teams.find({
            "captain_id": {"$in": user_ids_list},
            "status": {"$ne": "DISBANDED"}
        })
        async for team in cap_teams_cursor:
            raw_members = team.get("members", [])
            raw_subs = team.get("substitutes", [])

            # Filter out deleted user
            remaining_members = [
                m for m in raw_members
                if _extract_id(m) not in user_ids_list
            ]
            remaining_subs = [
                s for s in raw_subs
                if _extract_id(s) not in user_ids_list
            ]

            if remaining_members:
                # Promote the next senior member to captain
                first_m = remaining_members[0]
                new_cap_id = _extract_id(first_m)
                if isinstance(first_m, dict):
                    first_m["role"] = "CAPTAIN"
                elif hasattr(first_m, "role"):
                    first_m.role = "CAPTAIN"

                await db.teams.update_one(
                    {"_id": team["_id"]},
                    {"$set": {
                        "captain_id": new_cap_id,
                        "members": remaining_members,
                        "substitutes": remaining_subs,
                        "updated_at": now,
                    }}
                )
            else:
                # Disband and delete solo team
                await db.teams.delete_one({"_id": team["_id"]})

        # Remove user from any other teams where they are regular member or substitute
        await db.teams.update_many(
            {"members.user_id": {"$in": user_ids_list}},
            {"$pull": {"members": {"user_id": {"$in": user_ids_list}}}}
        )
        await db.teams.update_many(
            {"members": {"$in": user_ids_list}},
            {"$pull": {"members": {"$in": user_ids_list}}}
        )
        await db.teams.update_many(
            {"substitutes.user_id": {"$in": user_ids_list}},
            {"$pull": {"substitutes": {"user_id": {"$in": user_ids_list}}}}
        )
        await db.teams.update_many(
            {"substitutes": {"$in": user_ids_list}},
            {"$pull": {"substitutes": {"$in": user_ids_list}}}
        )
    except Exception as team_err:
        import logging
        logging.getLogger("ametheus.auth").warning(f"Team cleanup note: {team_err}")

    # 3. TOURNAMENT REGISTRATIONS
    try:
        # Cancel upcoming active registrations to prevent ghost brackets
        await db.tournament_registrations.delete_many({
            "$or": [
                {"player_id": {"$in": user_ids_list}},
                {"user_id": {"$in": user_ids_list}},
            ],
            "status": {"$in": ["PENDING", "CONFIRMED"]}
        })
        # De-identify historical registrations
        await db.tournament_registrations.update_many(
            {
                "$or": [
                    {"player_id": {"$in": user_ids_list}},
                    {"user_id": {"$in": user_ids_list}},
                ]
            },
            {
                "$set": {
                    "player_name": "Former Contender",
                    "display_name": "Deleted User",
                    "player_id": None,
                    "user_id": None,
                }
            }
        )
    except Exception as tourn_err:
        import logging
        logging.getLogger("ametheus.auth").warning(f"Tournament registration cleanup note: {tourn_err}")

    # 4. CUSTOM LEADERBOARDS & RANKINGS
    try:
        await db.custom_leaderboard_entries.delete_many({
            "$or": [
                {"user_id": {"$in": user_ids_list}},
                {"entity_id": {"$in": user_ids_list}},
                {"id": {"$in": user_ids_list}},
            ]
        })
        await db.leaderboard_disqualifications.delete_many({
            "$or": [
                {"user_id": {"$in": user_ids_list}},
                {"entity_id": {"$in": user_ids_list}},
            ]
        })
    except Exception as lb_err:
        import logging
        logging.getLogger("ametheus.auth").warning(f"Leaderboard cleanup note: {lb_err}")

    # 5. NOTIFICATIONS, REVIEWS & SUPPORT TICKETS
    try:
        await db.notifications.delete_many({"user_id": {"$in": user_ids_list}})
        await db.reviews.update_many(
            {"user_id": {"$in": user_ids_list}},
            {"$set": {"user_name": "Former Contender"}}
        )
        await db.support_tickets.update_many(
            {"user_id": {"$in": user_ids_list}},
            {"$set": {
                "user_id": None,
                "user_name": "Deleted Contender",
                "user_email": "deleted@ametheus.games",
            }}
        )
    except Exception as comm_err:
        import logging
        logging.getLogger("ametheus.auth").warning(f"Support/reviews cleanup note: {comm_err}")

    # 6. CLERK SERVER-SIDE USER DELETION
    clerk_id = current_user.clerk_user_id or target_user_doc.get("clerk_user_id")
    if settings.CLERK_SECRET_KEY and clerk_id:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                clerk_res = await client.delete(
                    f"https://api.clerk.com/v1/users/{clerk_id}",
                    headers={"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}
                )
                if clerk_res.status_code not in (200, 204, 404):
                    import logging
                    logging.getLogger("ametheus.auth").warning(
                        f"Clerk deleteUser API status {clerk_res.status_code}: {clerk_res.text}"
                    )
        except Exception as clerk_err:
            import logging
            logging.getLogger("ametheus.auth").error(f"Clerk server deletion error: {clerk_err}")

    # 7. PERMANENTLY REMOVE MONGODB USER DOCUMENT
    await db.users.delete_one({"_id": target_mongo_id})

    # 8. AUDIT LOGGING
    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="DELETE_ACCOUNT",
        target=f"User {current_user.id} ({current_user.email})",
        details="User permanently deleted their own account and all associated platform data was cleansed.",
    )

    return {"status": "success", "deleted": True, "message": "Account and associated data successfully deleted."}

