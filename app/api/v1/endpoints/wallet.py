import uuid
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_db, get_current_user, require_super_admin
from app.models.user import User
from app.models.payment import Payout
from app.schemas import WalletBalanceResponse, WalletWithdrawRequest
from app.services.wallet_service import (
    get_user_wallet_balances,
    reserve_wallet_funds,
    release_reserved_funds,
    finalize_reserved_deduction,
)
from app.services.audit import record_audit_log
from app.services.notification import create_notification

router = APIRouter()

@router.get("", response_model=WalletBalanceResponse)
async def get_wallet(
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    balances = await get_user_wallet_balances(db, current_user.id)
    return WalletBalanceResponse(**balances)

@router.get("/ledger")
async def get_wallet_ledger(
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    cursor = db.wallet_ledger.find({
        "user_id": current_user.id,
        "is_dummy": {"$ne": True}
    }).sort("created_at", -1).skip(offset).limit(limit)
    txns = await cursor.to_list(length=limit)
    for t in txns:
        t["id"] = str(t.get("_id", t.get("id")))
        t.pop("_id", None)
        t["amount_inr"] = t["amount_paise"] / 100
        t["balance_after_inr"] = t["balance_after_paise"] / 100
    return txns

@router.post("/withdraw")
async def request_wallet_withdrawal(
    payload: WalletWithdrawRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    if payload.amount_inr <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0")

    clean_upi = payload.upi_id.strip()
    if "@" not in clean_upi:
        raise HTTPException(status_code=400, detail="Invalid UPI ID format. Must contain '@'")

    amount_paise = int(round(payload.amount_inr * 100))

    # Move from available to reserved
    res = await reserve_wallet_funds(db, current_user.id, amount_paise)

    payout = Payout(
        payout_type="WALLET_WITHDRAWAL",
        user_id=current_user.id,
        user_name=current_user.display_name or current_user.username,
        amount_paise=amount_paise,
        destination="UPI",
        upi_id=clean_upi,
        status="PENDING",
    )
    p_doc = payout.to_mongo()
    await db.payouts.insert_one(p_doc)

    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="WALLET_WITHDRAWAL_REQUESTED",
        target=f"Withdrawal {payout.id} (₹{payload.amount_inr})",
        details=f"Reserved ₹{payload.amount_inr} to UPI {clean_upi}",
    )

    return {
        "status": "success",
        "message": f"Withdrawal request for ₹{payload.amount_inr:.2f} submitted for admin review.",
        "payout_id": payout.id,
        "available_inr": res["available_paise"] / 100,
        "reserved_inr": res["reserved_paise"] / 100,
    }

@router.get("/withdrawals/my")
async def get_my_withdrawals(
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    cursor = db.payouts.find({
        "user_id": current_user.id,
        "payout_type": "WALLET_WITHDRAWAL",
        "is_dummy": {"$ne": True}
    }).sort("created_at", -1)
    docs = await cursor.to_list(length=100)
    for d in docs:
        d["id"] = str(d.get("_id", d.get("id")))
        d.pop("_id", None)
        d["amount_inr"] = d["amount_paise"] / 100
    return docs
