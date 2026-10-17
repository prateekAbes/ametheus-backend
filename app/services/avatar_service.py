import logging
import secrets
from typing import Tuple, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.v1.endpoints.crests import DEFAULT_CRESTS, seed_crests_if_needed

logger = logging.getLogger("ametheus.avatar")

async def get_random_default_avatar(db: AsyncIOMotorDatabase) -> Tuple[str, str]:
    """
    Randomly select an avatar from the active Ametheus operator crests / gaming avatars collection.
    
    Source of truth:
    1. Active crests stored in db.crests ({"is_active": True}).
    2. Fallback to curated DEFAULT_CRESTS if database collection is temporarily empty.
    
    Guarantees:
    - Never uses or falls back to a Google/OAuth profile picture.
    - Adding new avatars/crests in admin panel immediately makes them eligible for future random assignment.
    - Removing or disabling an avatar/crest immediately stops it from being assigned to future users.
    
    Returns:
        (avatar_url, avatar_source) where avatar_source is "DEFAULT".
    """
    try:
        await seed_crests_if_needed(db)
        cursor = db.crests.find({"is_active": True})
        active_crests = await cursor.to_list(length=200)
        if active_crests:
            chosen = secrets.choice(active_crests)
            return chosen["image_url"], "DEFAULT"
    except Exception as e:
        logger.warning(f"[AvatarService] Error querying active crests collection: {e}")

    # Safe fallback to curated default gaming avatars
    chosen = secrets.choice(DEFAULT_CRESTS)
    return chosen["image_url"], "DEFAULT"
