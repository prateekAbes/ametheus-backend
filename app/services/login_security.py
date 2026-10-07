import logging
import asyncio
from datetime import datetime, timezone
from typing import Tuple, Optional
from fastapi import Request
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.services.email_service import send_security_alert_email

logger = logging.getLogger("ametheus.security")

def get_client_ip(request: Request) -> str:
    """
    Extracts client IP address respecting reverse proxies, Cloudflare headers, and forwarded lists.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # First entry in comma-separated chain is the client IP
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def format_device_info(user_agent: Optional[str]) -> str:
    """
    Parses User-Agent into a clean and human-readable device and browser summary.
    """
    ua = (user_agent or "").lower()
    os_name = "Desktop Client"
    if "windows" in ua:
        os_name = "Windows"
    elif "macintosh" in ua or "mac os" in ua:
        os_name = "macOS"
    elif "android" in ua:
        os_name = "Android"
    elif "iphone" in ua or "ipad" in ua:
        os_name = "iOS"
    elif "linux" in ua:
        os_name = "Linux"

    browser = "Web Browser"
    if "edg" in ua:
        browser = "Microsoft Edge"
    elif "chrome" in ua and "safari" in ua:
        browser = "Google Chrome"
    elif "safari" in ua and "chrome" not in ua:
        browser = "Apple Safari"
    elif "firefox" in ua:
        browser = "Mozilla Firefox"

    return f"{browser} on {os_name}"


async def process_login_security_check(
    db: AsyncIOMotorDatabase,
    user_doc: dict,
    request: Request,
) -> Tuple[bool, str]:
    """
    Monitors login IP and triggers security alert if an unrecognized/new IP signs in.
    1. Detects client IP from incoming HTTP request.
    2. Checks if the IP is already in user's known_ips set.
    3. If new IP:
       - Checks user's login_alerts preference (default: True).
       - If enabled, creates in-app security notification and dispatches security alert email.
       - Stores client_ip into user's known_ips set so subsequent logins do not repeat the alert.
       - Updates last_login_ip and last_login_at.
    4. If known IP:
       - Updates last_login_ip and last_login_at without sending notification.
    Returns:
        (is_new_ip: bool, client_ip: str)
    """
    client_ip = get_client_ip(request)
    user_agent = request.headers.get("user-agent", "Unknown Device")
    device_info = format_device_info(user_agent)
    now = datetime.now(timezone.utc)
    target_id = user_doc["_id"]

    known_ips = user_doc.get("known_ips") or []

    # If first time tracking for a historical user account, initialize known_ips without alert
    if not known_ips:
        await db.users.update_one(
            {"_id": target_id},
            {
                "$set": {
                    "known_ips": [client_ip],
                    "last_login_ip": client_ip,
                    "last_login_at": now,
                }
            }
        )
        return False, client_ip

    if client_ip in known_ips:
        # Recognized IP — record timestamp and IP, no alert needed
        await db.users.update_one(
            {"_id": target_id},
            {
                "$set": {
                    "last_login_ip": client_ip,
                    "last_login_at": now,
                }
            }
        )
        return False, client_ip

    # NEW UNRECOGNIZED IP DETECTED!
    user_prefs = user_doc.get("preferences") or {}
    login_alerts_enabled = user_prefs.get("login_alerts", True)
    user_email = user_doc.get("email")

    if login_alerts_enabled and user_email:
        target_email = str(user_email).strip().lower()
        user_name = user_doc.get("display_name") or user_doc.get("username") or "Contender"
        time_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")

        # 1. In-App Security Notification
        try:
            from app.services.notification import create_notification
            await create_notification(
                db=db,
                user_id=str(target_id),
                title="Security Alert: New Sign-in Detected",
                message=f"A new sign-in was detected from unrecognized IP {client_ip} ({device_info}). If this wasn't you, revoke your active sessions and change your password immediately.",
                notification_type="SYSTEM",
                category="SYSTEM",
                link="/settings#Security",
                send_email_copy=False,
            )
        except Exception as e:
            logger.warning(f"[Security Check] In-app notification creation failed: {e}")

        # 2. Email Security Alert Dispatch
        try:
            asyncio.create_task(
                send_security_alert_email(
                    to_email=target_email,
                    user_name=user_name,
                    client_ip=client_ip,
                    device_info=device_info,
                    timestamp_str=time_str,
                )
            )
            logger.info(f"[Security Check] Dispatched new IP security email for {user_email} from {client_ip}")
        except Exception as e:
            logger.warning(f"[Security Check] Security email dispatch failed: {e}")

    # Add client_ip to known_ips set in MongoDB so subsequent logins from this IP are recognized
    await db.users.update_one(
        {"_id": target_id},
        {
            "$addToSet": {"known_ips": client_ip},
            "$set": {
                "last_login_ip": client_ip,
                "last_login_at": now,
            }
        }
    )

    return True, client_ip
