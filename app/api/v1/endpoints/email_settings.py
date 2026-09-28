import logging
from typing import Optional
from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_db, require_super_admin
from app.models.user import User
from app.schemas.email_settings import (
    EmailSettingsResponse,
    EmailSystemStatusResponse,
    SmtpStatusResponse,
    SendTestEmailRequest,
    SendTestEmailResponse,
)
from app.services.email_service import (
    get_email_system_status,
    check_smtp_health,
    send_test_email,
)
from app.core.config import settings

logger = logging.getLogger("ametheus.api.email_settings")

# Dedicated router for public/platform configuration lookups
public_router = APIRouter()

# Admin-only router for Super Admin status & diagnostics (Strictly Read-Only + Test Dispatch)
admin_router = APIRouter()


@public_router.get("/email-settings", response_model=EmailSettingsResponse)
async def get_public_email_settings():
    """
    Public endpoint for frontend clients to dynamically retrieve official,
    support, and automated email identities configured in the server environment.
    Never exposes credentials or backend infrastructure details.
    """
    return EmailSettingsResponse(
        official_email=settings.OFFICIAL_EMAIL,
        support_email=settings.SUPPORT_EMAIL,
        automated_email=settings.NOREPLY_EMAIL,
        updated_at=None,
        updated_by=None,
    )


@admin_router.get("/status", response_model=EmailSystemStatusResponse)
async def get_admin_email_system_status(
    admin_user: User = Depends(require_super_admin),
):
    """
    Super Admin endpoint to inspect the read-only configuration status of all three
    Gmail SMTP identities (support, noreply, official).
    Credentials, App Passwords, and raw tokens are NEVER returned.
    """
    return get_email_system_status()


@admin_router.get("/settings", response_model=EmailSettingsResponse)
async def get_admin_email_settings(
    admin_user: User = Depends(require_super_admin),
):
    """
    Super Admin read-only endpoint returning currently active email identities.
    (PUT endpoint has been completely removed to prevent unauthorized mutations).
    """
    return EmailSettingsResponse(
        official_email=settings.OFFICIAL_EMAIL,
        support_email=settings.SUPPORT_EMAIL,
        automated_email=settings.NOREPLY_EMAIL,
        updated_at=None,
        updated_by="Server Environment",
    )


@admin_router.get("/smtp-status", response_model=SmtpStatusResponse)
async def get_smtp_health_status(
    service: str = Query("support", description="Service identity to test: 'support', 'noreply', or 'official'"),
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Live diagnostic health check of the backend Gmail SMTP connection for a specific identity.
    Verifies TCP connectivity and App Password authentication without exposing credentials.
    """
    return await check_smtp_health(service, db)


@admin_router.post("/send-test", response_model=SendTestEmailResponse)
async def send_diagnostic_test_email(
    payload: SendTestEmailRequest,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Sends a live diagnostic test message using the specified service identity ('support', 'noreply', or 'official')
    to the designated destination address.
    """
    return await send_test_email(
        to_email=str(payload.to_email),
        service=str(payload.service),
        db=db,
    )
