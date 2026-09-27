import base64
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, require_super_admin
from app.models.user import User
from app.models.payment import UpiAccount
from app.schemas import UpiAccountCreate, UpiAccountUpdate, UpiAccountResponse
from app.services.audit import record_audit_log
from app.api.v1.endpoints.media import _save_media, Base64UploadPayload

router = APIRouter()

def serialize_upi_account(d: dict) -> dict:
    item = dict(d)
    doc_id = str(item.get("_id", item.get("id", "")))
    item["id"] = doc_id
    if "_id" in item:
        item["_id"] = str(item["_id"])
    created_at = item.get("created_at")
    if created_at is not None:
        if hasattr(created_at, "isoformat"):
            item["created_at"] = created_at.isoformat()
        else:
            item["created_at"] = str(created_at)
    return item

@router.get("", response_model=List[UpiAccountResponse])
async def list_upi_accounts(
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    cursor = db.upi_accounts.find({"is_dummy": {"$ne": True}}).sort("created_at", -1)
    docs = await cursor.to_list(length=100)
    res = []
    for d in docs:
        cleaned = serialize_upi_account(d)
        res.append(UpiAccountResponse(**cleaned))
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

    cleaned = serialize_upi_account(doc)
    return UpiAccountResponse(**cleaned)

def _decode_b64(data_url: str):
    if "," in data_url:
        header, encoded = data_url.split(",", 1)
        content_type = header.split(";")[0].replace("data:", "") or "image/png"
        content = base64.b64decode(encoded)
    else:
        content = base64.b64decode(data_url)
        content_type = "image/png"
    return content, content_type

@router.post("/upload-qr")
async def upload_standalone_qr(
    file: UploadFile = File(...),
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Uploads a standalone QR code image directly into MongoDB `media` and returns persistent URL."""
    content = await file.read()
    content_type = file.content_type or "image/png"
    filename = file.filename or "upi_qr.png"
    media_result = await _save_media(content, filename, content_type, db)
    return {
        "status": "success",
        "qr_url": media_result["url"],
        "media_id": media_result["id"],
        "filename": filename,
    }

@router.post("/upload-qr-base64")
async def upload_standalone_qr_base64(
    payload: Base64UploadPayload,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Uploads a base64 QR code image directly into MongoDB `media` and returns persistent URL."""
    content, content_type = _decode_b64(payload.data_url)
    filename = payload.filename or "upi_qr.png"
    media_result = await _save_media(content, filename, content_type, db)
    return {
        "status": "success",
        "qr_url": media_result["url"],
        "media_id": media_result["id"],
        "filename": filename,
    }

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

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="UPDATE_UPI_ACCOUNT",
        target=f"UPI Account {updated.get('code')}",
        details=f"Updated fields: {list(update_dict.keys())}",
    )

    cleaned = serialize_upi_account(updated)
    return UpiAccountResponse(**cleaned)

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

@router.post("/{id}/qr")
async def upload_account_qr(
    id: str,
    file: UploadFile = File(...),
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Uploads a QR image and attaches it directly to the specified UPI account in the database."""
    acc = await db.upi_accounts.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not acc:
        raise HTTPException(status_code=404, detail="UPI account not found")

    content = await file.read()
    content_type = file.content_type or "image/png"
    filename = file.filename or f"qr_{acc.get('code', 'upi')}.png"

    media_result = await _save_media(content, filename, content_type, db)
    qr_url = media_result["url"]

    await db.upi_accounts.update_one(
        {"_id": acc["_id"]},
        {"$set": {"qr_url": qr_url, "updated_at": datetime.now(timezone.utc).isoformat()}}
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="UPLOAD_UPI_QR",
        target=f"UPI Account {acc.get('code')}",
        details=f"Uploaded and saved QR image ({media_result['id']}) into database",
    )

    updated = await db.upi_accounts.find_one({"_id": acc["_id"]})
    cleaned = serialize_upi_account(updated)
    return {
        "status": "success",
        "qr_url": qr_url,
        "media_id": media_result["id"],
        "account": UpiAccountResponse(**cleaned),
    }

@router.post("/{id}/qr-base64")
async def upload_account_qr_base64(
    id: str,
    payload: Base64UploadPayload,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Uploads a base64 QR image and attaches it directly to the specified UPI account in the database."""
    acc = await db.upi_accounts.find_one({"$or": [{"_id": id}, {"id": id}]})
    if not acc:
        raise HTTPException(status_code=404, detail="UPI account not found")

    content, content_type = _decode_b64(payload.data_url)
    filename = payload.filename or f"qr_{acc.get('code', 'upi')}.png"

    media_result = await _save_media(content, filename, content_type, db)
    qr_url = media_result["url"]

    await db.upi_accounts.update_one(
        {"_id": acc["_id"]},
        {"$set": {"qr_url": qr_url, "updated_at": datetime.now(timezone.utc).isoformat()}}
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role="SUPER_ADMIN",
        action="UPLOAD_UPI_QR",
        target=f"UPI Account {acc.get('code')}",
        details=f"Uploaded and saved QR image ({media_result['id']}) into database via base64",
    )

    updated = await db.upi_accounts.find_one({"_id": acc["_id"]})
    cleaned = serialize_upi_account(updated)
    return {
        "status": "success",
        "qr_url": qr_url,
        "media_id": media_result["id"],
        "account": UpiAccountResponse(**cleaned),
    }
