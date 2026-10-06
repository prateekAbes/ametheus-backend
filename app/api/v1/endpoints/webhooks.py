import json
import logging
from typing import Any, Dict
from fastapi import APIRouter, Depends, HTTPException, Request, status
from motor.motor_asyncio import AsyncIOMotorDatabase
import httpx

from app.api.deps import get_db
from app.core.config import settings
from app.services.resend_inbound_service import (
    verify_resend_webhook_signature,
    process_resend_inbound_email,
)

logger = logging.getLogger("ametheus.webhooks")

router = APIRouter()


@router.post("/resend/inbound")
async def resend_inbound_webhook(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> Dict[str, Any]:
    """
    Public inbound webhook receiver for Resend email events.
    - Validates Svix webhook signature using RESEND_WEBHOOK_SECRET
    - Handles 'email.received' event type
    - Pulls full email content from Resend Receiving API if payload contains metadata only
    - Matches or creates ticket thread
    - Saves user reply and updates conversation status
    """
    body_bytes = await request.body()
    headers = dict(request.headers)

    # 1. Svix Webhook Signature Verification
    if not verify_resend_webhook_signature(headers, body_bytes, settings.RESEND_WEBHOOK_SECRET):
        logger.warning("[RESEND WEBHOOK] Rejected unauthorized webhook request: invalid signature.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    # 2. JSON Payload Parsing
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except Exception as e:
        logger.warning(f"[RESEND WEBHOOK] Malformed JSON payload received: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed JSON payload",
        )

    event_type = payload.get("type")
    # Resend inbound email notifications have type 'email.received'
    if event_type and event_type != "email.received":
        logger.info(f"[RESEND WEBHOOK] Ignored non-inbound event type: '{event_type}'")
        return {"status": "ignored", "event_type": event_type}

    data = payload.get("data") or payload
    email_id = data.get("email_id") or data.get("id")

    # 3. If payload contains only metadata (typical for Resend webhooks), fetch full body via Resend API
    if (not data.get("text") and not data.get("html")) and email_id and settings.RESEND_API_KEY:
        try:
            logger.info(f"[RESEND WEBHOOK] Fetching full email content from Resend API for email_id: {email_id}")
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://api.resend.com/emails/receiving/{email_id}",
                    headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
                )
                if resp.status_code == 200:
                    api_data = resp.json()
                    for k, v in api_data.items():
                        if k not in data or not data[k]:
                            data[k] = v
                else:
                    logger.warning(f"[RESEND WEBHOOK] Resend Receiving API returned status {resp.status_code}: {resp.text}")
        except Exception as e:
            logger.warning(f"[RESEND WEBHOOK] Failed to fetch email from Resend Receiving API: {e}")

    # 4. Process inbound email
    result = await process_resend_inbound_email(db, data)
    return result
