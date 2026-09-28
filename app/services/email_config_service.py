"""
Centralized getter for email configuration identities.
The server environment is the authoritative source of truth.
"""
import logging
from typing import Optional, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.core.config import settings

logger = logging.getLogger("ametheus.email_config")

DEFAULT_OFFICIAL_EMAIL = settings.OFFICIAL_EMAIL
DEFAULT_SUPPORT_EMAIL = settings.SUPPORT_EMAIL
DEFAULT_AUTOMATED_EMAIL = settings.NOREPLY_EMAIL


async def get_email_settings(db: Optional[AsyncIOMotorDatabase] = None) -> Dict[str, Any]:
    """
    Centralized getter for email configuration identities.
    Environment variables are the single authoritative source of truth.
    """
    return {
        "official_email": settings.OFFICIAL_EMAIL,
        "support_email": settings.SUPPORT_EMAIL,
        "automated_email": settings.NOREPLY_EMAIL,
        "updated_at": None,
        "updated_by": "Server Environment",
    }


async def get_official_email(db: Optional[AsyncIOMotorDatabase] = None) -> str:
    """Returns the official email from environment."""
    return settings.OFFICIAL_EMAIL


async def get_support_email(db: Optional[AsyncIOMotorDatabase] = None) -> str:
    """Returns the support email from environment."""
    return settings.SUPPORT_EMAIL


async def get_automated_email(db: Optional[AsyncIOMotorDatabase] = None) -> str:
    """Returns the automated/no-reply sender email from environment."""
    return settings.NOREPLY_EMAIL
