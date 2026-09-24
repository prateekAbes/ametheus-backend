import secrets
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.payment import GamePayment
from app.schemas import (
    GamePaymentInitiateRequest,
    GamePaymentInitiateResponse,
    SubmitUtrRequest,
    WalletTopupInitiateRequest,
)
from app.services.upi_service import get_next_active_upi_account, build_upi_payment_uri
from app.services.wallet_service import debit_wallet_funds, get_user_wallet_balances
from app.services.audit import record_audit_log
from app.services.notification import create_notification

router = APIRouter()

@router.post("/game/initiate", response_model=GamePaymentInitiateResponse)
async def initiate_game_payment(
    payload: GamePaymentInitiateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Initiates payment directly for a Game.
    - If entry fee is 0 (Free), team status is immediately set to PAID.
    - If Wallet: calculates 5% discount server-side, debits balance, sets team to PAID.
    - If UPI: assigns rotating active UPI account, snapshots details immutably into payment.
    """
    game = await db.games.find_one({"$or": [{"slug": payload.game_slug}, {"_id": payload.game_slug}, {"id": payload.game_slug}]})
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    gid = str(game.get("_id", game.get("id")))
    entry_fee_inr = float(game.get("entry_fee_inr", 0))

    # Resolve team if team game
    team = None
    team_name = None
    if payload.team_id:
        team = await db.teams.find_one({"$or": [{"_id": payload.team_id}, {"id": payload.team_id}]})
        if not team:
            raise HTTPException(status_code=404, detail="Team not found")
        team_name = team.get("name")
        if team.get("captain_id") != current_user.id and current_user.role != "SUPER_ADMIN":
            raise HTTPException(status_code=403, detail="Only team captain can pay entry fee for the team")

    # FREE GAME
    if entry_fee_inr <= 0:
        payment = GamePayment(
            payment_type="GAME_ENTRY",
            user_id=current_user.id,
            game_id=gid,
            game_name=game.get("name"),
            team_id=payload.team_id,
            team_name=team_name,
            amount_paise=0,
            amount_inr=0.0,
            payment_method="FREE",
            status="APPROVED",
        )
        p_doc = payment.to_mongo()
        await db.game_payments.insert_one(p_doc)
        if team:
            await db.teams.update_one(
                {"_id": team["_id"]},
                {"$set": {"payment_status": "PAID", "payment_id": payment.id}}
            )
        return GamePaymentInitiateResponse(
            payment_id=payment.id,
            game_id=gid,
            game_name=game.get("name"),
            team_id=payload.team_id,
            amount_inr=0.0,
            amount_paise=0,
            payment_method="FREE",
            status="APPROVED",
        )

    # WALLET PAYMENT (5% discount)
    if payload.payment_method.upper() == "WALLET":
        discounted_inr = round(entry_fee_inr * 0.95, 2)
        discount_amount = round(entry_fee_inr * 0.05, 2)
        amount_paise = int(round(discounted_inr * 100))

        # Check balance and debit
        try:
            await debit_wallet_funds(
                db=db,
                user_id=current_user.id,
                amount_paise=amount_paise,
                txn_type="GAME_ENTRY_PAYMENT",
                reference_id=payload.team_id or gid,
                description=f"Entry fee for {game.get('name')} (5% Wallet discount applied)",
            )
        except HTTPException as e:
            raise e

        payment = GamePayment(
            payment_type="GAME_ENTRY",
            user_id=current_user.id,
            game_id=gid,
            game_name=game.get("name"),
            team_id=payload.team_id,
            team_name=team_name,
            amount_paise=amount_paise,
            amount_inr=discounted_inr,
            payment_method="WALLET",
            status="APPROVED",
        )
        p_doc = payment.to_mongo()
        await db.game_payments.insert_one(p_doc)

        if team:
            await db.teams.update_one(
                {"_id": team["_id"]},
                {"$set": {"payment_status": "PAID", "payment_id": payment.id}}
            )

        return GamePaymentInitiateResponse(
            payment_id=payment.id,
            game_id=gid,
            game_name=game.get("name"),
            team_id=payload.team_id,
            amount_inr=discounted_inr,
            amount_paise=amount_paise,
            payment_method="WALLET",
            wallet_discount_inr=discount_amount,
            status="APPROVED",
        )

    # UPI PAYMENT (Full price, Rotated UPI Account)
    amount_paise = int(round(entry_fee_inr * 100))
    upi_acc = await get_next_active_upi_account(db)

    upi_id = upi_acc.get("upi_id")
    payee_name = upi_acc.get("payee_name", "AMETHEUS")
    acc_code = upi_acc.get("code", "UPI001")
    acc_id = str(upi_acc.get("_id", upi_acc.get("id")))
    qr_url = upi_acc.get("qr_url")

    upi_uri = build_upi_payment_uri(
        upi_id=upi_id,
        payee_name=payee_name,
        amount_inr=entry_fee_inr,
        note=f"{game.get('name')} Entry",
    )

    payment = GamePayment(
        payment_type="GAME_ENTRY",
        user_id=current_user.id,
        game_id=gid,
        game_name=game.get("name"),
        team_id=payload.team_id,
        team_name=team_name,
        amount_paise=amount_paise,
        amount_inr=entry_fee_inr,
        payment_method="UPI",
        payment_account_id=acc_id,
        payment_account_code=acc_code,
        upi_id=upi_id,
        payee_name=payee_name,
        qr_url=qr_url,
        upi_uri=upi_uri,
        status="PENDING",
    )
    p_doc = payment.to_mongo()
    await db.game_payments.insert_one(p_doc)

    if team:
        await db.teams.update_one(
            {"_id": team["_id"]},
            {"$set": {"payment_status": "PAYMENT_PENDING", "payment_id": payment.id}}
        )

    return GamePaymentInitiateResponse(
        payment_id=payment.id,
        game_id=gid,
        game_name=game.get("name"),
        team_id=payload.team_id,
        amount_inr=entry_fee_inr,
        amount_paise=amount_paise,
        payment_method="UPI",
        status="PENDING",
        payment_account_code=acc_code,
        upi_id=upi_id,
        payee_name=payee_name,
        qr_url=qr_url,
        upi_uri=upi_uri,
    )

@router.post("/game/{payment_id}/submit-utr")
async def submit_payment_utr(
    payment_id: str,
    payload: SubmitUtrRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    payment = await db.game_payments.find_one({"$or": [{"_id": payment_id}, {"id": payment_id}]})
    if not payment:
        raise HTTPException(status_code=404, detail="Payment record not found")

    if payment.get("user_id") != current_user.id and current_user.role != "SUPER_ADMIN":
        raise HTTPException(status_code=403, detail="Not authorized to submit UTR for this payment")

    clean_utr = payload.utr.strip()
    if len(clean_utr) < 6:
        raise HTTPException(status_code=400, detail="Invalid UPI Transaction ID / UTR. Must be at least 6 characters.")

    # Duplicate UTR check across all payments
    existing_utr = await db.game_payments.find_one({
        "utr": clean_utr,
        "$or": [{"_id": {"$ne": payment["_id"]}}, {"id": {"$ne": payment.get("id")}}]
    })
    if existing_utr:
        raise HTTPException(status_code=400, detail="This UPI Transaction ID / UTR has already been submitted for another payment.")

    await db.game_payments.update_one(
        {"_id": payment["_id"]},
        {
            "$set": {
                "utr": clean_utr,
                "screenshot_url": payload.screenshot_url,
                "status": "PENDING",
            }
        }
    )

    if payment.get("team_id"):
        await db.teams.update_one(
            {"$or": [{"_id": payment["team_id"]}, {"id": payment["team_id"]}]},
            {"$set": {"payment_status": "PAYMENT_PENDING"}}
        )

    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="SUBMIT_UTR",
        target=f"Payment {payment_id}",
        details=f"Submitted UTR {clean_utr}",
    )

    return {
        "status": "success",
        "message": "Payment details submitted successfully. Awaiting admin verification.",
        "payment_id": payment_id,
        "utr": clean_utr,
    }

@router.post("/wallet/topup/initiate")
async def initiate_wallet_topup(
    payload: WalletTopupInitiateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    if payload.amount_inr <= 0:
        raise HTTPException(status_code=400, detail="Topup amount must be greater than 0")

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
        status="PENDING",
    )
    p_doc = payment.to_mongo()
    await db.game_payments.insert_one(p_doc)

    return {
        "payment_id": payment.id,
        "amount_inr": payload.amount_inr,
        "amount_paise": amount_paise,
        "payment_account_code": acc_code,
        "upi_id": upi_id,
        "payee_name": payee_name,
        "qr_url": qr_url,
        "upi_uri": upi_uri,
    }

@router.get("/my")
async def get_my_payments(
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    cursor = db.game_payments.find({
        "user_id": current_user.id,
        "is_dummy": {"$ne": True}
    }).sort("created_at", -1)
    docs = await cursor.to_list(length=100)
    for d in docs:
        d["id"] = str(d.get("_id", d.get("id")))
        d.pop("_id", None)
    return docs

@router.get("/{id}")
async def get_payment_status(
    id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    payment = await db.game_payments.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not payment:
        raise HTTPException(status_code=404, detail="Payment record not found")

    if payment.get("user_id") != current_user.id and current_user.role != "SUPER_ADMIN":
        raise HTTPException(status_code=403, detail="Forbidden")

    payment["id"] = str(payment.get("_id", payment.get("id")))
    payment.pop("_id", None)
    return payment
