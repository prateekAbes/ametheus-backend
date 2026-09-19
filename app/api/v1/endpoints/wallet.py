import uuid
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_db, get_current_user, require_super_admin
from app.models.user import User
from app.services.audit import record_audit_log
from app.services.notification import create_notification

router = APIRouter()

class WithdrawalRequestPayload(BaseModel):
    amount: float = Field(..., gt=0, description="Amount in INR to withdraw")
    upi_id: str = Field(..., min_length=4, description="Destination UPI ID / VPA")
    account_holder_name: Optional[str] = None
    notes: Optional[str] = None

class WithdrawalApprovalPayload(BaseModel):
    payout_reference: Optional[str] = Field(None, description="IMPS / UTR payout transaction reference")
    admin_notes: Optional[str] = None

class WithdrawalRejectionPayload(BaseModel):
    admin_notes: str = Field(..., min_length=3, description="Reason for withdrawal rejection")

class DepositPayload(BaseModel):
    amount: float = Field(..., gt=0, description="Amount in INR to deposit")

@router.post("/withdraw")
async def request_wallet_withdrawal(
    payload: WithdrawalRequestPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Submits a withdrawal request.
    Requires admin approval before disbursement.
    Amount is deducted from user's active inr_balance into pending escrow.
    """
    user_doc = await db.users.find_one({"$or": [{"_id": current_user.id}, {"id": current_user.id}]})
    if not user_doc:
        raise HTTPException(status_code=404, detail="User account not found")

    current_balance = float(user_doc.get("inr_balance", "0") or "0")
    if current_balance < payload.amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Insufficient wallet balance. Available: ₹{current_balance:.2f}, Requested: ₹{payload.amount:.2f}",
        )

    # Clean and validate UPI ID format
    upi = payload.upi_id.strip()
    if "@" not in upi:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid UPI ID format. Must contain '@' (e.g. user@oksbi, gamer@paytm)",
        )

    new_balance = current_balance - payload.amount
    await db.users.update_one(
        {"_id": user_doc["_id"]},
        {"$set": {"inr_balance": str(int(new_balance) if new_balance.is_integer() else f"{new_balance:.2f}")}}
    )

    request_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    doc = {
        "id": request_id,
        "_id": request_id,
        "user_id": current_user.id,
        "user_email": current_user.email,
        "user_name": current_user.display_name or current_user.username,
        "amount": payload.amount,
        "upi_id": upi,
        "account_holder_name": payload.account_holder_name or current_user.display_name or current_user.username,
        "notes": payload.notes,
        "status": "PENDING_APPROVAL",  # PENDING_APPROVAL | COMPLETED | REJECTED
        "created_at": now_iso,
        "reviewed_by": None,
        "reviewed_at": None,
        "payout_reference": None,
        "admin_notes": None,
    }

    await db.withdrawal_requests.insert_one(doc)

    # Record Audit Log
    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="WALLET_WITHDRAWAL_REQUESTED",
        target=f"Request {request_id} (₹{payload.amount} -> {upi})",
        details="Submitted for Admin Approval",
    )

    # Create In-App Notification
    await create_notification(
        db=db,
        user_id=current_user.id,
        title="Withdrawal Request Submitted",
        message=f"Your withdrawal request of ₹{payload.amount:,.2f} to {upi} is pending Admin review.",
        notification_type="PAYMENT",
        link="/dashboard",
    )

    return {
        "status": "success",
        "message": "Withdrawal request submitted for Admin approval. Funds will be disbursed once reviewed.",
        "request": {
            "id": request_id,
            "amount": payload.amount,
            "upi_id": upi,
            "status": "PENDING_APPROVAL",
            "created_at": now_iso,
        },
        "new_balance": new_balance,
    }

@router.get("/withdrawals/my")
async def get_my_withdrawals(
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns withdrawal requests created by the authenticated user.
    """
    cursor = db.withdrawal_requests.find({"user_id": current_user.id}).sort("created_at", -1)
    docs = await cursor.to_list(length=100)
    for d in docs:
        d["id"] = str(d.get("_id", d.get("id")))
        d.pop("_id", None)
    return docs

@router.get("/withdrawals/admin")
async def get_admin_withdrawals(
    status_filter: Optional[str] = None,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    (Super Admin only) Returns all withdrawal requests across the platform.
    """
    query = {}
    if status_filter and status_filter.upper() != "ALL":
        query["status"] = status_filter.upper()

    cursor = db.withdrawal_requests.find(query).sort("created_at", -1)
    docs = await cursor.to_list(length=200)
    for d in docs:
        d["id"] = str(d.get("_id", d.get("id")))
        d.pop("_id", None)
    return docs

@router.post("/withdrawals/{request_id}/approve")
async def approve_withdrawal(
    request_id: str,
    payload: WithdrawalApprovalPayload,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    (Super Admin only) Approves a pending withdrawal request and certifies payout.
    """
    req = await db.withdrawal_requests.find_one({"$or": [{"_id": request_id}, {"id": request_id}]})
    if not req:
        raise HTTPException(status_code=404, detail="Withdrawal request not found")

    if req.get("status") != "PENDING_APPROVAL":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot approve request with current status: {req.get('status')}",
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    update_fields = {
        "status": "COMPLETED",
        "reviewed_by": admin_user.username,
        "reviewed_at": now_iso,
        "payout_reference": payload.payout_reference or f"IMPS-{uuid.uuid4().hex[:10].upper()}",
        "admin_notes": payload.admin_notes or "Approved and disbursed via IMPS/UPI",
    }

    await db.withdrawal_requests.update_one({"_id": req["_id"]}, {"$set": update_fields})

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="WALLET_WITHDRAWAL_APPROVED",
        target=f"Request {request_id} (₹{req.get('amount')})",
        details=f"Ref: {update_fields['payout_reference']} | Notes: {update_fields['admin_notes']}",
    )

    await create_notification(
        db=db,
        user_id=req["user_id"],
        title="Withdrawal Approved & Paid",
        message=f"Your withdrawal of ₹{req.get('amount', 0):,.2f} has been approved and paid to {req.get('upi_id')}. Ref: {update_fields['payout_reference']}.",
        notification_type="PAYMENT",
        link="/dashboard",
    )

    return {
        "status": "success",
        "message": "Withdrawal approved and payout marked as completed",
        "request_id": request_id,
        "payout_reference": update_fields["payout_reference"],
    }

@router.post("/withdrawals/{request_id}/reject")
async def reject_withdrawal(
    request_id: str,
    payload: WithdrawalRejectionPayload,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    (Super Admin only) Rejects a withdrawal request and refunds the amount back to user's wallet.
    """
    req = await db.withdrawal_requests.find_one({"$or": [{"_id": request_id}, {"id": request_id}]})
    if not req:
        raise HTTPException(status_code=404, detail="Withdrawal request not found")

    if req.get("status") != "PENDING_APPROVAL":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot reject request with status: {req.get('status')}",
        )

    amount = float(req.get("amount", 0))
    user_id = req["user_id"]

    # Refund amount back to user
    user_doc = await db.users.find_one({"$or": [{"_id": user_id}, {"id": user_id}]})
    if user_doc:
        current_bal = float(user_doc.get("inr_balance", "0") or "0")
        refunded_bal = current_bal + amount
        await db.users.update_one(
            {"_id": user_doc["_id"]},
            {"$set": {"inr_balance": str(int(refunded_bal) if refunded_bal.is_integer() else f"{refunded_bal:.2f}")}}
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    await db.withdrawal_requests.update_one(
        {"_id": req["_id"]},
        {"$set": {
            "status": "REJECTED",
            "reviewed_by": admin_user.username,
            "reviewed_at": now_iso,
            "admin_notes": payload.admin_notes,
        }}
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="WALLET_WITHDRAWAL_REJECTED",
        target=f"Request {request_id} (₹{amount} refunded to user {user_id})",
        details=f"Reason: {payload.admin_notes}",
    )

    await create_notification(
        db=db,
        user_id=user_id,
        title="Withdrawal Request Rejected",
        message=f"Your withdrawal of ₹{amount:,.2f} was rejected: {payload.admin_notes}. The full amount has been refunded to your wallet.",
        notification_type="PAYMENT",
        link="/dashboard",
    )

    return {
        "status": "success",
        "message": f"Withdrawal request rejected. ₹{amount:,.2f} refunded to user wallet.",
        "request_id": request_id,
    }

@router.post("/deposit")
async def deposit_funds(
    payload: DepositPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Adds funds to the user's wallet via Platform Escrow Deposit.
    """
    user_doc = await db.users.find_one({"$or": [{"_id": current_user.id}, {"id": current_user.id}]})
    if not user_doc:
        raise HTTPException(status_code=404, detail="User account not found")

    current_bal = float(user_doc.get("inr_balance", "0") or "0")
    new_bal = current_bal + payload.amount

    await db.users.update_one(
        {"_id": user_doc["_id"]},
        {"$set": {"inr_balance": str(int(new_bal) if new_bal.is_integer() else f"{new_bal:.2f}")}}
    )

    await record_audit_log(
        db=db,
        operator=current_user.username,
        role=current_user.role,
        action="WALLET_DEPOSIT_COMPLETED",
        target=f"User {current_user.id} (+₹{payload.amount})",
        details="Credited via Platform Escrow Deposit",
    )

    await create_notification(
        db=db,
        user_id=current_user.id,
        title="Wallet Deposit Successful",
        message=f"₹{payload.amount:,.2f} has been added to your AMETHEUS Escrow Wallet.",
        notification_type="PAYMENT",
        link="/dashboard",
    )

    return {
        "status": "success",
        "message": f"₹{payload.amount:,.2f} added to wallet.",
        "new_balance": new_bal,
    }
