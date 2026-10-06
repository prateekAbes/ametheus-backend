import uuid
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from pydantic import BaseModel
from app.api.deps import get_db, get_current_user, require_super_admin
from app.models.user import User
from app.models.payment import Payout, GamePayment
from app.schemas import WalletBalanceResponse, WalletWithdrawRequest
from app.services.wallet_service import (
    get_user_wallet_balances,
    reserve_wallet_funds,
    release_reserved_funds,
    finalize_reserved_deduction,
)
from app.services.upi_service import get_next_active_upi_account, build_upi_payment_uri
from app.services.audit import record_audit_log
from app.services.notification import create_notification

router = APIRouter()

@router.get("", response_model=WalletBalanceResponse)
@router.get("/balance", response_model=WalletBalanceResponse)
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
        amt_paise = t.get("amount_paise", 0)
        is_credit = amt_paise > 0
        t["entry_type"] = "CREDIT" if is_credit else "DEBIT"
        t["source_type"] = t.get("txn_type", "WALLET_TOPUP" if is_credit else "PAYMENT")
        t["amount_inr"] = abs(amt_paise) / 100
        t["balance_after_inr"] = t.get("balance_after_paise", 0) / 100
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

    await create_notification(
        db=db,
        user_id=current_user.id,
        title="Withdrawal Requested",
        message=f"Withdrawal request for ₹{payload.amount_inr:.2f} to {clean_upi} submitted. Funds are reserved pending admin review.",
        notification_type="WALLET",
        category="WALLET",
        link="/wallet",
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

class WalletDepositPayload(BaseModel):
    amount_inr: float
    utr: Optional[str] = None
    screenshot_url: Optional[str] = None

@router.post("/deposit")
@router.post("/topup")
async def deposit_wallet_funds(
    payload: WalletDepositPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    if payload.amount_inr <= 0:
        raise HTTPException(status_code=400, detail="Deposit amount must be greater than zero")

    amount_paise = int(round(payload.amount_inr * 100))
    upi_acc = await get_next_active_upi_account(db)
    upi_id = upi_acc.get("upi_id")
    payee_name = upi_acc.get("payee_name", "AMETHEUS")
    acc_code = upi_acc.get("code", "UPI001")
    acc_id = str(upi_acc.get("_id", upi_acc.get("id")))
    qr_url = upi_acc.get("qr_url")

    upi_uri = build_upi_payment_uri(
        upi_id=upi_id,
        payee_name=payee_name,
        amount_inr=payload.amount_inr,
        note="Wallet Topup",
    )

    clean_utr = payload.utr.strip() if payload.utr else None
    payment_status = "PENDING" if clean_utr else "INITIATED"

    payment = GamePayment(
        payment_type="WALLET_TOPUP",
        user_id=current_user.id,
        amount_paise=amount_paise,
        amount_inr=payload.amount_inr,
        payment_method="UPI",
        payment_account_id=acc_id,
        payment_account_code=acc_code,
        upi_id=upi_id,
        payee_name=payee_name,
        qr_url=qr_url,
        upi_uri=upi_uri,
        utr=clean_utr,
        screenshot_url=payload.screenshot_url,
        status=payment_status,
    )
    p_doc = payment.to_mongo()
    await db.game_payments.insert_one(p_doc)

    if clean_utr:
        await record_audit_log(
            db=db,
            operator=current_user.username,
            role=current_user.role,
            action="SUBMIT_WALLET_DEPOSIT",
            target=f"Payment {payment.id}",
            details=f"Deposit of ₹{payload.amount_inr:.2f} submitted with UTR {clean_utr}",
        )
        await create_notification(
            db=db,
            user_id=current_user.id,
            title="Deposit Submitted",
            message=f"Deposit reference {clean_utr} received. Wallet will be credited upon admin verification.",
            notification_type="WALLET",
            category="WALLET",
            link="/wallet",
        )

    return {
        "status": payment_status,
        "payment_id": payment.id,
        "amount_inr": payload.amount_inr,
        "amount_paise": amount_paise,
        "payment_account_code": acc_code,
        "upi_id": upi_id,
        "payee_name": payee_name,
        "qr_url": qr_url,
        "upi_uri": upi_uri,
        "utr": clean_utr,
        "message": "Deposit submitted for admin approval" if clean_utr else "Deposit initiated",
    }

