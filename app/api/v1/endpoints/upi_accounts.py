from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, require_super_admin
from app.models.user import User
from app.models.payment import UpiAccount
from app.schemas import UpiAccountCreate, UpiAccountUpdate, UpiAccountResponse
from app.services.audit import record_audit_log

router = APIRouter()

@router.get("", response_model=List[UpiAccountResponse])
async def list_upi_accounts(
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    cursor = db.upi_accounts.find({"is_dummy": {"$ne": True}}).sort("created_at", -1)
    docs = await cursor.to_list(length=100)
    res = []
    for d in docs:
        d["id"] = str(d.get("_id", d.get("id")))
        res.append(UpiAccountResponse(**d))
    return res

@router.post("", response_model=UpiAccountResponse, status_code=status.HTTP_201_CREATED)
async def create_upi_account(
    payload: UpiAccountCreate,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    code_upper = payload.code.strip().upper()
    existing = await db.upi_accounts.find_one({"code": code_upper})
    if existing:
        raise HTTPException(status_code=409, detail=f"UPI account code '{code_upper}' already exists.")

    acc = UpiAccount(
        code=code_upper,
        upi_id=payload.upi_id.strip(),
        payee_name=payload.payee_name.strip(),
        qr_url=payload.qr_url,
        display_name=payload.display_name,
        status=payload.status or "ACTIVE",
    )
    doc = acc.to_mongo()
    await db.upi_accounts.insert_one(doc)

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="CREATE_UPI_ACCOUNT",
        target=f"UPI Account {acc.code} ({acc.upi_id})",
        details="Added new rotating UPI account",
    )

    doc["id"] = str(doc.get("_id", doc.get("id")))
    return UpiAccountResponse(**doc)

@router.patch("/{id}", response_model=UpiAccountResponse)
async def update_upi_account(
    id: str,
    payload: UpiAccountUpdate,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    acc = await db.upi_accounts.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not acc:
        raise HTTPException(status_code=404, detail="UPI account not found")

    update_dict = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if update_dict:
        await db.upi_accounts.update_one({"_id": acc["_id"]}, {"$set": update_dict})

    updated = await db.upi_accounts.find_one({"_id": acc["_id"]})
    updated["id"] = str(updated.get("_id", updated.get("id")))

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="UPDATE_UPI_ACCOUNT",
        target=f"UPI Account {updated.get('code')}",
        details=f"Updated fields: {list(update_dict.keys())}",
    )

    return UpiAccountResponse(**updated)

@router.post("/{id}/toggle-status")
async def toggle_upi_account_status(
    id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    acc = await db.upi_accounts.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not acc:
        raise HTTPException(status_code=404, detail="UPI account not found")

    new_status = "INACTIVE" if acc.get("status") == "ACTIVE" else "ACTIVE"
    await db.upi_accounts.update_one({"_id": acc["_id"]}, {"$set": {"status": new_status}})

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="TOGGLE_UPI_STATUS",
        target=f"UPI Account {acc.get('code')}",
        details=f"Status changed to {new_status}",
    )

    return {"status": "success", "new_status": new_status, "code": acc.get("code")}
