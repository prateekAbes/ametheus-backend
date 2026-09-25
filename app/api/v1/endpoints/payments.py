import secrets
import hmac
import hashlib
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.api.deps import get_db, get_current_user
from app.models.user import User
from app.models.payment import GamePayment
from app.models.tournament import TournamentRegistration
from app.schemas import (
    GamePaymentInitiateRequest,
    GamePaymentInitiateResponse,
    SubmitUtrRequest,
    WalletTopupInitiateRequest,
    OrderCreateRequest,
    PaymentVerifyRequest,
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
@router.post("/wallet/{payment_id}/submit-utr")
@router.post("/{payment_id}/submit-utr")
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

    await create_notification(
        db=db,
        user_id=current_user.id,
        title="Payment Submitted",
        message=f"UTR reference {clean_utr} submitted successfully. Wallet will be credited upon admin verification.",
        notification_type="PAYMENT",
        link="/dashboard",
    )

    return {
        "status": "PENDING",
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

@router.post("/create-order")
async def create_escrow_order(
    payload: OrderCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Creates an escrow order for tournament entry fee via Platform Escrow Engine.
    Supports both team registrations and solo player registrations.
    """
    tourn = await db.tournaments.find_one({
        "$or": [{"_id": payload.tournament_id}, {"id": payload.tournament_id}, {"slug": payload.tournament_id}]
    })
    if not tourn:
        raise HTTPException(status_code=404, detail="Tournament not found")

    if tourn.get("status") != "REGISTRATION_OPEN":
        raise HTTPException(status_code=400, detail="Tournament registration is not open")

    tourn_id = str(tourn.get("_id", tourn.get("id")))

    # Check capacity
    reg_count = await db.tournament_registrations.count_documents({
        "$or": [{"tournament_id": tourn_id}, {"tournament_id": tourn.get("slug")}]
    })

    if reg_count >= tourn.get("max_teams", 16):
        raise HTTPException(status_code=400, detail="Tournament is at maximum capacity")

    amount_inr = tourn.get("entry_fee_inr", 0)
    amount_paise = amount_inr * 100

    # Handle Solo Registration Order (when no team_id is provided)
    if not payload.team_id:
        if amount_inr == 0:
            reg = TournamentRegistration(
                tournament_id=tourn_id,
                player_id=current_user.id,
                payment_status="FREE",
                check_in_status="PENDING",
            )
            await db.tournament_registrations.insert_one(reg.to_mongo())
            return {
                "status": "free",
                "message": "Free entry solo tournament. Registration confirmed.",
                "order_id": None,
                "amount_inr": 0,
            }

        escrow_order_id = f"order_amx_{secrets.token_hex(8)}"
        existing_reg = await db.tournament_registrations.find_one({
            "tournament_id": tourn_id,
            "player_id": current_user.id,
        })
        if existing_reg:
            await db.tournament_registrations.update_one(
                {"_id": existing_reg["_id"]},
                {"$set": {"order_id": escrow_order_id, "payment_status": "PENDING"}}
            )
        else:
            reg = TournamentRegistration(
                tournament_id=tourn_id,
                player_id=current_user.id,
                payment_status="PENDING",
                order_id=escrow_order_id,
                check_in_status="PENDING",
            )
            await db.tournament_registrations.insert_one(reg.to_mongo())

        return {
            "status": "created",
            "order_id": escrow_order_id,
            "amount_paise": amount_paise,
            "amount_inr": amount_inr,
            "currency": "INR",
            "tournament_title": tourn.get("title"),
            "team_name": current_user.display_name or current_user.username,
        }

    # Handle Team Registration Order
    team = await db.teams.find_one({"$or": [{"_id": payload.team_id}, {"id": payload.team_id}]})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.get("captain_id") != current_user.id and current_user.role != "SUPER_ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the squad captain can initiate payment for registration",
        )

    team_id = str(team.get("_id", team.get("id")))

    # If free tournament, create immediate PAID registration
    if amount_inr == 0:
        reg = TournamentRegistration(
            tournament_id=tourn_id,
            team_id=team_id,
            payment_status="FREE",
            check_in_status="PENDING",
        )
        await db.tournament_registrations.insert_one(reg.to_mongo())
        return {
            "status": "free",
            "message": "Free entry tournament. Registration confirmed.",
            "order_id": None,
            "amount_inr": 0,
        }

    # Generate Platform Escrow Order
    escrow_order_id = f"order_amx_{secrets.token_hex(8)}"

    # Check existing pending registration
    existing_reg = await db.tournament_registrations.find_one({
        "tournament_id": tourn_id,
        "team_id": team_id,
    })

    if existing_reg:
        await db.tournament_registrations.update_one(
            {"_id": existing_reg["_id"]},
            {"$set": {"order_id": escrow_order_id, "payment_status": "PENDING"}}
        )
    else:
        reg = TournamentRegistration(
            tournament_id=tourn_id,
            team_id=team_id,
            payment_status="PENDING",
            order_id=escrow_order_id,
            check_in_status="PENDING",
        )
        await db.tournament_registrations.insert_one(reg.to_mongo())

    return {
        "status": "created",
        "order_id": escrow_order_id,
        "amount_paise": amount_paise,
        "amount_inr": amount_inr,
        "currency": "INR",
        "tournament_title": tourn.get("title"),
        "team_name": team.get("name"),
    }

@router.post("/verify")
async def verify_payment(
    payload: PaymentVerifyRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Verifies platform escrow signature and confirms escrow deposit.
    """
    order_id = payload.get_order_id()
    payment_id = payload.get_payment_id()
    signature = payload.get_signature()

    message = f"{order_id}|{payment_id}".encode("utf-8")
    expected_sig = hmac.new(
        settings.ESCROW_SECRET_KEY.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()

    is_valid = hmac.compare_digest(expected_sig, signature)

    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payment signature verification failed. Tampered or invalid callback.",
        )

    # Find registration by order_id
    reg = await db.tournament_registrations.find_one({
        "order_id": order_id
    })
    if not reg:
        raise HTTPException(status_code=404, detail="Order registration record not found")

    await db.tournament_registrations.update_one(
        {"_id": reg["_id"]},
        {"$set": {"payment_status": "PAID"}}
    )

    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="ESCROW_DEPOSIT_VERIFIED",
        target=f"Order {order_id} -> Tourn {reg.get('tournament_id')}",
        details=f"Payment ID: {payment_id} | Signature verified via Platform Escrow Engine",
    )

    await create_notification(
        db=db,
        user_id=current_user.id,
        title="Escrow Payment Confirmed",
        message=f"Your entry fee payment (Order: {order_id}) has been verified and placed in secure platform escrow.",
        notification_type="PAYMENT",
        link="/dashboard",
    )

    return {
        "status": "success",
        "message": "Payment verified and held in secure platform escrow. Registration confirmed.",
        "order_id": order_id,
        "payment_id": payment_id,
    }

@router.get("/order/{order_id}")
async def get_order_status(order_id: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    reg = await db.tournament_registrations.find_one({"order_id": order_id})
    if not reg:
        raise HTTPException(status_code=404, detail="Order not found")

    return {
        "order_id": reg.get("order_id"),
        "payment_status": reg.get("payment_status"),
        "tournament_id": reg.get("tournament_id"),
        "team_id": reg.get("team_id"),
    }

