import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.core.config import settings
from app.models.user import User
from app.schemas.email_settings import EmailSettingsUpdateRequest
from app.services.audit import record_audit_log

logger = logging.getLogger("ametheus.email_config")

# Fallback default constants
DEFAULT_OFFICIAL_EMAIL = getattr(settings, "OFFICIAL_EMAIL", "ametheus@gmail.com") or "ametheus@gmail.com"
DEFAULT_SUPPORT_EMAIL = getattr(settings, "SUPPORT_EMAIL", "ametheus.support@gmail.com") or "ametheus.support@gmail.com"
DEFAULT_AUTOMATED_EMAIL = getattr(settings, "AUTOMATED_EMAIL", "ametheus.noreply@gmail.com") or "ametheus.noreply@gmail.com"


async def get_email_settings(db: Optional[AsyncIOMotorDatabase] = None) -> Dict[str, Any]:
    """
    Centralized getter for dynamic email configuration identities.
    Database is the primary source of truth.
    Falls back gracefully if not yet configured or if db is None.
    """
    if db is None:
        try:
            from app.core.database import get_database
            db = get_database()
        except Exception:
            db = None

    if db is None:
        return {
            "official_email": DEFAULT_OFFICIAL_EMAIL,
            "support_email": DEFAULT_SUPPORT_EMAIL,
            "automated_email": DEFAULT_AUTOMATED_EMAIL,
            "updated_at": None,
            "updated_by": None,
        }

    try:
        doc = await db.platform_settings.find_one({"_id": "email_settings"})
        support_doc = await db.platform_settings.find_one({"_id": "support_settings"})

        support_from_db = (doc.get("support_email") if doc else None) or (
            support_doc.get("support_email") if support_doc else None
        )

        return {
            "official_email": (doc.get("official_email") if doc else None) or DEFAULT_OFFICIAL_EMAIL,
            "support_email": support_from_db or DEFAULT_SUPPORT_EMAIL,
            "automated_email": (doc.get("automated_email") if doc else None) or DEFAULT_AUTOMATED_EMAIL,
            "updated_at": doc.get("updated_at") if doc else None,
            "updated_by": doc.get("updated_by") if doc else None,
        }
    except Exception as e:
        logger.error(f"[Ametheus EmailConfig] Error fetching email settings from database: {e}")
        return {
            "official_email": DEFAULT_OFFICIAL_EMAIL,
            "support_email": DEFAULT_SUPPORT_EMAIL,
            "automated_email": DEFAULT_AUTOMATED_EMAIL,
            "updated_at": None,
            "updated_by": None,
        }


async def get_official_email(db: Optional[AsyncIOMotorDatabase] = None) -> str:
    """Returns the currently configured official email."""
    cfg = await get_email_settings(db)
    return cfg.get("official_email") or DEFAULT_OFFICIAL_EMAIL


async def get_support_email(db: Optional[AsyncIOMotorDatabase] = None) -> str:
    """Returns the currently configured support email."""
    cfg = await get_email_settings(db)
    return cfg.get("support_email") or DEFAULT_SUPPORT_EMAIL


async def get_automated_email(db: Optional[AsyncIOMotorDatabase] = None) -> str:
    """Returns the currently configured automated sender email."""
    cfg = await get_email_settings(db)
    return cfg.get("automated_email") or DEFAULT_AUTOMATED_EMAIL


async def update_email_settings(
    db: AsyncIOMotorDatabase,
    payload: EmailSettingsUpdateRequest,
    actor: User,
    ip_address: str = "127.0.0.1",
) -> Dict[str, Any]:
    """
    Updates the three email identities, logs audit trail for any changes,
    and returns the saved settings.
    """
    current_settings = await get_email_settings(db)
    now_iso = datetime.now(timezone.utc).isoformat()
    actor_name = actor.username or actor.id

    new_official = str(payload.official_email).strip().lower()
    new_support = str(payload.support_email).strip().lower()
    new_automated = str(payload.automated_email).strip().lower()

    # Track and log changes in audit trail
    changes = []
    if current_settings.get("official_email") != new_official:
        changes.append(("Official Email", "official_email", current_settings.get("official_email"), new_official))
    if current_settings.get("support_email") != new_support:
        changes.append(("Support Email", "support_email", current_settings.get("support_email"), new_support))
    if current_settings.get("automated_email") != new_automated:
        changes.append(("Automated Email", "automated_email", current_settings.get("automated_email"), new_automated))

    for label, key, old_v, new_v in changes:
        try:
            await record_audit_log(
                db=db,
                operator=actor_name,
                role=actor.role or "SUPER_ADMIN",
                action="EMAIL_SETTING_CHANGED",
                target=f"Email Configuration ({label})",
                details=f"Email Setting Changed | Setting: {label} | Old Value: {old_v} | New Value: {new_v}",
                ip_address=ip_address,
            )
        except Exception as e:
            logger.error(f"[Ametheus EmailConfig] Failed to record audit log for {key}: {e}")

    # Persist in db.platform_settings
    update_doc = {
        "official_email": new_official,
        "support_email": new_support,
        "automated_email": new_automated,
        "updated_at": now_iso,
        "updated_by": actor_name,
    }
    await db.platform_settings.update_one(
        {"_id": "email_settings"},
        {"$set": update_doc},
        upsert=True,
    )

    # Keep support_settings document in sync for backwards compatibility
    await db.platform_settings.update_one(
        {"_id": "support_settings"},
        {
            "$set": {
                "support_email": new_support,
                "updated_at": now_iso,
                "updated_by": actor_name,
            }
        },
        upsert=True,
    )

    logger.info(f"[Ametheus EmailConfig] Email settings updated by {actor_name}: {update_doc}")
    return update_doc
