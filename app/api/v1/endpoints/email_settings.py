import logging
from fastapi import APIRouter, Depends, HTTPException, Request, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_db, require_super_admin
from app.models.user import User
from app.schemas.email_settings import (
    EmailSettingsResponse,
    EmailSettingsUpdateRequest,
    SmtpStatusResponse,
    SendTestEmailRequest,
    SendTestEmailResponse,
)
from app.services.email_config_service import (
    get_email_settings,
    update_email_settings,
)
from app.services.email_service import (
    check_smtp_health,
    send_test_email,
)

logger = logging.getLogger("ametheus.api.email_settings")

# Dedicated router for public/platform configuration lookups
public_router = APIRouter()

# Admin-only router for Super Admin configuration & SMTP diagnostics
admin_router = APIRouter()


@public_router.get("/email-settings", response_model=EmailSettingsResponse)
async def get_public_email_settings(
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Public endpoint for frontend clients to dynamically retrieve official,
    support, and automated email identities without exposing any server secrets.
    """
    settings_dict = await get_email_settings(db)
    return EmailSettingsResponse(
        official_email=settings_dict["official_email"],
        support_email=settings_dict["support_email"],
        automated_email=settings_dict["automated_email"],
        updated_at=settings_dict.get("updated_at"),
        updated_by=None,  # Do not leak operator names publicly
    )


@admin_router.get("/settings", response_model=EmailSettingsResponse)
async def get_admin_email_settings(
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Super Admin endpoint to inspect current email identities, last updated timestamp,
    and modifying operator.
    """
    settings_dict = await get_email_settings(db)
    return EmailSettingsResponse(**settings_dict)


@admin_router.put("/settings", response_model=EmailSettingsResponse)
async def update_admin_email_settings(
    payload: EmailSettingsUpdateRequest,
    request: Request,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Super Admin endpoint to modify Official Email, Support Email, and Automated Email.
    Persists changes directly in the database and records a cryptographic audit log.
    """
    forwarded_for = request.headers.get("x-forwarded-for")
    ip_address = forwarded_for.split(",")[0].strip() if forwarded_for else (request.client.host if request.client else "127.0.0.1")

    updated = await update_email_settings(
        db=db,
        payload=payload,
        actor=admin_user,
        ip_address=ip_address,
    )
    return EmailSettingsResponse(**updated)


@admin_router.get("/smtp-status", response_model=SmtpStatusResponse)
async def get_smtp_health_status(
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Live diagnostic health check of the backend SMTP configuration.
    Verifies host resolution, TCP connectivity, and authentication without exposing credentials.
    """
    return await check_smtp_health(db)


@admin_router.post("/send-test", response_model=SendTestEmailResponse)
async def send_diagnostic_test_email(
    payload: SendTestEmailRequest,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Sends a live diagnostic test message using the configured Automated Email to the specified destination.
    """
    result = await send_test_email(str(payload.to_email), db)
    return result
