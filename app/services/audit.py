import hashlib
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.models.admin import AuditLog

GENESIS_HASH = "0000000000000000000000000000000000000000000000000000000000000000"

def compute_record_hash(
    prev_hash: str,
    operator: str,
    role: str,
    action: str,
    target: str,
    details: Optional[str] = None,
) -> str:
    payload = f"{prev_hash}:{operator}:{role}:{action}:{target}:{details or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

async def record_audit_log(
    db: AsyncIOMotorDatabase,
    operator: str,
    role: str,
    action: str,
    target: str,
    details: Optional[str] = None,
    ip_address: str = "127.0.0.1",
) -> AuditLog:
    """
    Creates an AuditLog entry linked to the previous entry via a SHA-256 hash chain in MongoDB.
    """
    latest_doc = await db.audit_logs.find_one(sort=[("created_at", -1), ("_id", -1)])
    prev_hash = latest_doc.get("record_hash") if (latest_doc and latest_doc.get("record_hash")) else GENESIS_HASH
    rec_hash = compute_record_hash(prev_hash, operator, role, action, target, details)

    log = AuditLog(
        operator=operator,
        role=role,
        action=action,
        target=target,
        details=details,
        ip_address=ip_address,
        prev_hash=prev_hash,
        record_hash=rec_hash,
    )
    await db.audit_logs.insert_one(log.to_mongo())
    return log

