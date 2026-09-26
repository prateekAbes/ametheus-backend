from typing import Dict, Any, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from fastapi import HTTPException, status
from app.models.payment import WalletLedger

async def get_user_wallet_balances(db: AsyncIOMotorDatabase, user_id: str) -> Dict[str, Any]:
    user = await db.users.find_one({"$or": [{"_id": user_id}, {"id": user_id}]})
    if not user:
        raise HTTPException(status_code=404, detail="User account not found")

    avail_paise = int(user.get("inr_balance_paise", 0))
    if avail_paise == 0 and user.get("inr_balance"):
        try:
            avail_paise = int(float(user["inr_balance"]) * 100)
        except Exception:
            avail_paise = 0

    reserved_paise = int(user.get("inr_reserved_paise", 0))

    return {
        "available_paise": avail_paise,
        "available_inr": avail_paise / 100,
        "reserved_paise": reserved_paise,
        "reserved_inr": reserved_paise / 100,
        "total_paise": avail_paise + reserved_paise,
        "total_inr": (avail_paise + reserved_paise) / 100,
    }

async def credit_wallet_funds(
    db: AsyncIOMotorDatabase,
    user_id: str,
    amount_paise: int,
    txn_type: str,
    reference_id: Optional[str],
    description: str,
    admin_id: Optional[str] = None,
) -> int:
    """
    Credits funds to user's available wallet balance atomically and inserts ledger entry.
    """
    if amount_paise <= 0:
        raise HTTPException(status_code=400, detail="Credit amount must be greater than zero")

    user = await db.users.find_one({"$or": [{"_id": user_id}, {"id": user_id}]})
    if not user:
        raise HTTPException(status_code=404, detail="User account not found")

    current_avail = int(user.get("inr_balance_paise", 0))
    if current_avail == 0 and user.get("inr_balance"):
        try:
            current_avail = int(float(user["inr_balance"]) * 100)
            await db.users.update_one({"_id": user["_id"]}, {"$set": {"inr_balance_paise": current_avail}})
        except Exception:
            current_avail = 0

    updated = await db.users.find_one_and_update(
        {"_id": user["_id"]},
        {"$inc": {"inr_balance_paise": amount_paise}},
        return_document=True,
    )
    new_avail = int(updated.get("inr_balance_paise", current_avail + amount_paise))
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"inr_balance": f"{new_avail / 100:.2f}"}})

    ledger = WalletLedger(
        user_id=user_id,
        txn_type=txn_type,
        amount_paise=amount_paise,
        balance_before_paise=current_avail,
        balance_after_paise=new_avail,
        reference_id=reference_id,
        description=description,
        admin_id=admin_id,
    )
    await db.wallet_ledger.insert_one(ledger.to_mongo())
    return new_avail

async def debit_wallet_funds(
    db: AsyncIOMotorDatabase,
    user_id: str,
    amount_paise: int,
    txn_type: str,
    reference_id: Optional[str],
    description: str,
) -> int:
    """
    Debits funds from user's available balance atomically.
    Raises 400 if insufficient funds.
    """
    if amount_paise <= 0:
        raise HTTPException(status_code=400, detail="Debit amount must be greater than zero")

    user = await db.users.find_one({"$or": [{"_id": user_id}, {"id": user_id}]})
    if not user:
        raise HTTPException(status_code=404, detail="User account not found")

    # Idempotency check: if transaction already processed for this reference, do not debit twice
    if reference_id:
        existing_txn = await db.wallet_ledger.find_one({
            "user_id": user_id,
            "reference_id": reference_id,
            "txn_type": txn_type,
        })
        if existing_txn:
            return int(user.get("inr_balance_paise", 0))

    current_avail = int(user.get("inr_balance_paise", 0))
    if current_avail == 0 and user.get("inr_balance"):
        try:
            current_avail = int(float(user["inr_balance"]) * 100)
            await db.users.update_one({"_id": user["_id"]}, {"$set": {"inr_balance_paise": current_avail}})
        except Exception:
            current_avail = 0

    # Atomic condition: only decrement if inr_balance_paise >= amount_paise
    updated = await db.users.find_one_and_update(
        {"_id": user["_id"], "inr_balance_paise": {"$gte": amount_paise}},
        {"$inc": {"inr_balance_paise": -amount_paise}},
        return_document=True,
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Insufficient wallet balance. Required: ₹{amount_paise/100:.2f}, Available: ₹{current_avail/100:.2f}",
        )

    new_avail = int(updated.get("inr_balance_paise", 0))
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"inr_balance": f"{new_avail / 100:.2f}"}})

    ledger = WalletLedger(
        user_id=user_id,
        txn_type=txn_type,
        amount_paise=-amount_paise,
        balance_before_paise=current_avail,
        balance_after_paise=new_avail,
        reference_id=reference_id,
        description=description,
    )
    await db.wallet_ledger.insert_one(ledger.to_mongo())
    return new_avail

async def reserve_wallet_funds(
    db: AsyncIOMotorDatabase,
    user_id: str,
    amount_paise: int,
) -> Dict[str, Any]:
    """
    Moves funds from available to reserved balance for pending withdrawal atomically.
    """
    if amount_paise <= 0:
        raise HTTPException(status_code=400, detail="Reservation amount must be greater than zero")

    user = await db.users.find_one({"$or": [{"_id": user_id}, {"id": user_id}]})
    if not user:
        raise HTTPException(status_code=404, detail="User account not found")

    current_avail = int(user.get("inr_balance_paise", 0))
    if current_avail == 0 and user.get("inr_balance"):
        try:
            current_avail = int(float(user["inr_balance"]) * 100)
            await db.users.update_one({"_id": user["_id"]}, {"$set": {"inr_balance_paise": current_avail}})
        except Exception:
            current_avail = 0

    # Atomic condition: only reserve if inr_balance_paise >= amount_paise
    updated = await db.users.find_one_and_update(
        {"_id": user["_id"], "inr_balance_paise": {"$gte": amount_paise}},
        {
            "$inc": {
                "inr_balance_paise": -amount_paise,
                "inr_reserved_paise": amount_paise,
            }
        },
        return_document=True,
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Insufficient available balance to reserve ₹{amount_paise/100:.2f}. Available: ₹{current_avail/100:.2f}",
        )

    new_avail = int(updated.get("inr_balance_paise", 0))
    new_reserved = int(updated.get("inr_reserved_paise", 0))
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"inr_balance": f"{new_avail / 100:.2f}"}})

    return {
        "available_paise": new_avail,
        "reserved_paise": new_reserved,
    }

async def release_reserved_funds(
    db: AsyncIOMotorDatabase,
    user_id: str,
    amount_paise: int,
) -> None:
    """
    Returns reserved funds back to available balance upon payout/withdrawal rejection atomically.
    """
    user = await db.users.find_one({"$or": [{"_id": user_id}, {"id": user_id}]})
    if not user:
        return

    current_res = int(user.get("inr_reserved_paise", 0))
    deduct_res = min(current_res, amount_paise)

    updated = await db.users.find_one_and_update(
        {"_id": user["_id"]},
        {
            "$inc": {
                "inr_balance_paise": amount_paise,
                "inr_reserved_paise": -deduct_res,
            }
        },
        return_document=True,
    )
    new_avail = int(updated.get("inr_balance_paise", 0))
    await db.users.update_one({"_id": user["_id"]}, {"$set": {"inr_balance": f"{new_avail / 100:.2f}"}})

async def finalize_reserved_deduction(
    db: AsyncIOMotorDatabase,
    user_id: str,
    amount_paise: int,
    reference_id: str,
    admin_id: str,
    description: str,
) -> None:
    """
    Permanently deducts reserved funds when admin completes a withdrawal payment.
    Inserts WALLET_WITHDRAWAL transaction into ledger.
    """
    user = await db.users.find_one({"$or": [{"_id": user_id}, {"id": user_id}]})
    if not user:
        return

    current_avail = int(user.get("inr_balance_paise", 0))
    current_reserved = int(user.get("inr_reserved_paise", 0))
    deduct_res = min(current_reserved, amount_paise)

    updated = await db.users.find_one_and_update(
        {"_id": user["_id"]},
        {"$inc": {"inr_reserved_paise": -deduct_res}},
        return_document=True,
    )
    new_reserved = int(updated.get("inr_reserved_paise", 0))

    ledger = WalletLedger(
        user_id=user_id,
        txn_type="WALLET_WITHDRAWAL",
        amount_paise=-amount_paise,
        balance_before_paise=current_avail + current_reserved,
        balance_after_paise=current_avail + new_reserved,
        reference_id=reference_id,
        description=description,
        admin_id=admin_id,
    )
    await db.wallet_ledger.insert_one(ledger.to_mongo())
