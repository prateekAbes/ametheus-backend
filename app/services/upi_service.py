import urllib.parse
from typing import Optional, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase
from fastapi import HTTPException
from app.models.payment import UpiAccount

async def get_next_active_upi_account(db: AsyncIOMotorDatabase) -> Dict[str, Any]:
    """
    Selects one active UPI payment account using round-robin / balanced rotation
    (the active account with the lowest payment_count).
    Atomically increments its payment_count.
    """
    cursor = db.upi_accounts.find({"status": "ACTIVE", "is_dummy": {"$ne": True}}).sort("payment_count", 1)
    accounts = await cursor.to_list(length=10)

    if not accounts:
        # Fallback to any active account (including dummy if dev mode)
        cursor_any = db.upi_accounts.find({"status": "ACTIVE"}).sort("payment_count", 1)
        accounts = await cursor_any.to_list(length=10)

    if not accounts:
        # Provide default system account if none configured
        default_account = {
            "id": "upi_default_01",
            "code": "UPI001",
            "upi_id": "ametheus.esports@okhdfcbank",
            "payee_name": "AMETHEUS Gaming Platform",
            "qr_url": None,
            "status": "ACTIVE",
            "payment_count": 0,
        }
        await db.upi_accounts.update_one(
            {"code": "UPI001"},
            {"$setOnInsert": default_account},
            upsert=True
        )
        return default_account

    selected = accounts[0]
    sel_id = selected.get("_id", selected.get("id"))
    await db.upi_accounts.update_one(
        {"_id": sel_id},
        {"$inc": {"payment_count": 1}}
    )
    return selected

def build_upi_payment_uri(upi_id: str, payee_name: str, amount_inr: float, note: str = "Game Entry Fee") -> str:
    """
    Constructs a standard UPI payment URI:
    upi://pay?pa=<UPI_ID>&pn=<PAYEE_NAME>&am=<AMOUNT>&cu=INR&tn=<NOTE>
    """
    params = {
        "pa": upi_id,
        "pn": payee_name,
        "am": f"{amount_inr:.2f}",
        "cu": "INR",
        "tn": note,
    }
    return f"upi://pay?{urllib.parse.urlencode(params)}"
