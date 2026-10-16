import re
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId
from fastapi import HTTPException, status

from app.models.user import User
from app.schemas.archive import ArchiveFilterRequest
from app.services.audit import record_audit_log

SECTION_CONFIG: Dict[str, Dict[str, str]] = {
    "payments": {
        "collection": "game_payments",
        "audit_tag": "PAYMENTS",
        "display_name": "Payments",
        "id_field": "_id",
    },
    "wallet_ledger": {
        "collection": "wallet_ledger",
        "audit_tag": "WALLET_LEDGER",
        "display_name": "Wallet Ledger",
        "id_field": "_id",
    },
    "audit_logs": {
        "collection": "audit_logs",
        "audit_tag": "AUDIT_LOGS",
        "display_name": "Audit Logs",
        "id_field": "_id",
    },
    "notifications": {
        "collection": "notifications",
        "audit_tag": "NOTIFICATIONS",
        "display_name": "Notifications",
        "id_field": "_id",
    },
    "support_tickets": {
        "collection": "support_tickets",
        "audit_tag": "SUPPORT_TICKETS",
        "display_name": "Support Tickets",
        "id_field": "_id",
    },
    "payouts": {
        "collection": "payouts",
        "audit_tag": "PAYOUTS",
        "display_name": "Payouts",
        "id_field": "_id",
    },
}

def get_section_config(section: str) -> Dict[str, str]:
    clean_sec = section.strip().lower()
    if clean_sec not in SECTION_CONFIG:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid archive section '{section}'. Supported sections: {list(SECTION_CONFIG.keys())}",
        )
    return SECTION_CONFIG[clean_sec]

def _parse_iso_or_date(d_str: str) -> Optional[datetime]:
    if not d_str:
        return None
    clean = d_str.strip()
    # Try YYYY-MM-DD
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(clean, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

def build_archive_query(section: str, payload: ArchiveFilterRequest, for_active: bool = True) -> Dict[str, Any]:
    """
    Constructs a robust MongoDB query matching records to archive or view.
    If for_active=True, targets only non-archived records.
    If for_active=False, targets archived records.
    """
    query: Dict[str, Any] = {}
    if section == "support_tickets":
        if for_active:
            query["is_archived"] = {"$nin": [True, "true", "True"]}
            query["status"] = {"$ne": "ARCHIVED"}
        else:
            query["$or"] = [
                {"is_archived": True},
                {"is_archived": "true"},
                {"is_archived": "True"},
                {"status": "ARCHIVED"},
            ]
    else:
        if for_active:
            query["is_archived"] = {"$ne": True}
        else:
            query["is_archived"] = True

    # Exclude synthetic dummy records
    query["is_dummy"] = {"$ne": True}

    # Specific Target IDs
    if payload.target_ids and len(payload.target_ids) > 0:
        id_clauses = []
        for tid in payload.target_ids:
            tid_s = str(tid).strip()
            id_clauses.append({"_id": tid_s})
            id_clauses.append({"id": tid_s})
            if section == "support_tickets":
                id_clauses.append({"ticket_id": tid_s})
            if ObjectId.is_valid(tid_s):
                id_clauses.append({"_id": ObjectId(tid_s)})
        query["$or"] = id_clauses
        return query

    # Status filter
    if payload.statuses and len(payload.statuses) > 0:
        query["status"] = {"$in": [s.upper() for s in payload.statuses]}
    elif payload.status and payload.status.upper() != "ALL":
        st = payload.status.upper()
        if st == "RESOLVED_CLOSED":
            query["status"] = {"$in": ["RESOLVED", "CLOSED"]}
        else:
            query["status"] = st

    # Category filter
    if payload.category and payload.category.upper() != "ALL":
        query["category"] = payload.category.upper()

    # Txn type (Wallet)
    if payload.txn_type and payload.txn_type.upper() != "ALL":
        query["txn_type"] = payload.txn_type.upper()

    # Payment type
    if payload.payment_type and payload.payment_type.upper() != "ALL":
        query["payment_type"] = payload.payment_type.upper()

    # Direction (Credit / Debit for Wallet)
    if payload.direction and payload.direction.upper() != "ALL":
        d_upper = payload.direction.upper()
        if d_upper == "CREDIT":
            query["amount_paise"] = {"$gt": 0}
        elif d_upper == "DEBIT":
            query["amount_paise"] = {"$lt": 0}

    # Action / Operator for Audit logs
    if payload.action and payload.action.upper() != "ALL":
        query["action"] = payload.action.upper()
    if payload.operator and payload.operator.strip():
        query["operator"] = {"$regex": re.escape(payload.operator.strip()), "$options": "i"}

    # User filter
    if payload.user_id and payload.user_id.strip():
        u = payload.user_id.strip()
        query["$or"] = [{"user_id": u}, {"user_name": {"$regex": re.escape(u), "$options": "i"}}]

    # Read status for Notifications
    if payload.is_read is not None:
        query["is_read"] = payload.is_read

    # Date filters (supports both Datetime and ISO String in Mongo)
    before_dt: Optional[datetime] = None
    from_dt: Optional[datetime] = None
    to_dt: Optional[datetime] = None

    now = datetime.now(timezone.utc)
    if payload.date_preset == "older_7_days":
        before_dt = now - timedelta(days=7)
    elif payload.date_preset == "older_30_days":
        before_dt = now - timedelta(days=30)
    elif payload.date_preset == "older_90_days":
        before_dt = now - timedelta(days=90)
    elif payload.date_before:
        before_dt = _parse_iso_or_date(payload.date_before)

    if payload.date_from:
        from_dt = _parse_iso_or_date(payload.date_from)
    if payload.date_to:
        to_dt = _parse_iso_or_date(payload.date_to)

    date_conditions = []
    if before_dt:
        date_conditions.append({
            "$or": [
                {"created_at": {"$lte": before_dt}},
                {"created_at": {"$lte": before_dt.isoformat()}},
            ]
        })
    if from_dt:
        date_conditions.append({
            "$or": [
                {"created_at": {"$gte": from_dt}},
                {"created_at": {"$gte": from_dt.isoformat()}},
            ]
        })
    if to_dt:
        date_conditions.append({
            "$or": [
                {"created_at": {"$lte": to_dt}},
                {"created_at": {"$lte": to_dt.isoformat()}},
            ]
        })

    if date_conditions:
        if "$and" in query:
            query["$and"].extend(date_conditions)
        else:
            query["$and"] = date_conditions

    # Text search
    if payload.search and payload.search.strip():
        s = payload.search.strip()
        rgx = {"$regex": re.escape(s), "$options": "i"}
        if section == "payments":
            query["$or"] = [{"utr": rgx}, {"id": rgx}, {"_id": rgx}, {"user_id": rgx}, {"game_name": rgx}]
        elif section == "wallet_ledger":
            query["$or"] = [{"reference_id": rgx}, {"description": rgx}, {"user_id": rgx}, {"id": rgx}]
        elif section == "audit_logs":
            query["$or"] = [{"operator": rgx}, {"action": rgx}, {"target": rgx}, {"details": rgx}]
        elif section == "support_tickets":
            query["$or"] = [{"ticket_id": rgx}, {"name": rgx}, {"email": rgx}, {"description": rgx}]
        elif section == "notifications":
            query["$or"] = [{"title": rgx}, {"message": rgx}, {"user_id": rgx}]
        elif section == "payouts":
            query["$or"] = [{"user_id": rgx}, {"user_name": rgx}, {"upi_id": rgx}, {"outgoing_utr": rgx}]

    return query

async def preview_archive(
    db: AsyncIOMotorDatabase,
    section: str,
    payload: ArchiveFilterRequest,
) -> int:
    cfg = get_section_config(section)
    query = build_archive_query(section, payload, for_active=True)
    count = await db[cfg["collection"]].count_documents(query)
    return count

async def execute_archive(
    db: AsyncIOMotorDatabase,
    section: str,
    payload: ArchiveFilterRequest,
    admin_user: User,
) -> int:
    cfg = get_section_config(section)
    query = build_archive_query(section, payload, for_active=True)

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    admin_username = admin_user.username if getattr(admin_user, "username", None) else getattr(admin_user, "email", "admin")
    admin_role = getattr(admin_user, "role", "SUPER_ADMIN")

    res = await db[cfg["collection"]].update_many(
        query,
        {
            "$set": {
                "is_archived": True,
                "archived_at": now_iso,
                "archived_by": admin_username,
                "archive_reason": payload.reason or "Admin section cleanup",
            }
        },
    )

    archived_count = res.modified_count

    # Always log the archive event to the cryptographic audit trail
    details = f"Archived {archived_count} records. Criteria: {payload.model_dump(exclude_none=True)}"
    await record_audit_log(
        db=db,
        operator=admin_username,
        role=admin_role,
        action=f"ARCHIVE_{cfg['audit_tag']}",
        target=f"{cfg['display_name']} ({archived_count} records)",
        details=details,
    )

    return archived_count

async def archive_single_record(
    db: AsyncIOMotorDatabase,
    section: str,
    record_id: str,
    admin_user: User,
    reason: Optional[str] = None,
) -> bool:
    cfg = get_section_config(section)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    admin_username = admin_user.username if getattr(admin_user, "username", None) else getattr(admin_user, "email", "admin")
    admin_role = getattr(admin_user, "role", "SUPER_ADMIN")

    id_clauses = [{"_id": record_id}, {"id": record_id}]
    if section == "support_tickets":
        id_clauses.append({"ticket_id": record_id})
    if ObjectId.is_valid(record_id):
        id_clauses.append({"_id": ObjectId(record_id)})

    find_query = {"$or": id_clauses}
    existing = await db[cfg["collection"]].find_one(find_query)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{cfg['display_name']} record '{record_id}' not found",
        )

    res = await db[cfg["collection"]].update_one(
        {"_id": existing["_id"]},
        {
            "$set": {
                "is_archived": True,
                "archived_at": now_iso,
                "archived_by": admin_username,
                "archive_reason": reason or "Admin manual archive",
            }
        },
    )

    await record_audit_log(
        db=db,
        operator=admin_username,
        role=admin_role,
        action=f"ARCHIVE_{cfg['audit_tag']}_SINGLE",
        target=f"{cfg['display_name']} Record {record_id}",
        details=f"Archived single record. Reason: {reason or 'Manual'}",
    )
    return res.modified_count > 0

async def unarchive_record(
    db: AsyncIOMotorDatabase,
    section: str,
    record_id: str,
    admin_user: User,
) -> bool:
    cfg = get_section_config(section)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    admin_username = admin_user.username if getattr(admin_user, "username", None) else getattr(admin_user, "email", "admin")
    admin_role = getattr(admin_user, "role", "SUPER_ADMIN")

    id_clauses = [{"_id": record_id}, {"id": record_id}]
    if section == "support_tickets":
        id_clauses.append({"ticket_id": record_id})
    if ObjectId.is_valid(record_id):
        id_clauses.append({"_id": ObjectId(record_id)})

    find_query = {"$or": id_clauses}
    existing = await db[cfg["collection"]].find_one(find_query)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{cfg['display_name']} record '{record_id}' not found",
        )

    update_set = {
        "is_archived": False,
        "unarchived_at": now_iso,
        "unarchived_by": admin_username,
    }
    if section == "support_tickets" and str(existing.get("status", "")).upper() == "ARCHIVED":
        update_set["status"] = "RESOLVED" if existing.get("resolved_at") else "OPEN"

    res = await db[cfg["collection"]].update_one(
        {"_id": existing["_id"]},
        {
            "$set": update_set,
            "$unset": {
                "archived_at": "",
                "archived_by": "",
                "archive_reason": "",
            },
        },
    )

    await record_audit_log(
        db=db,
        operator=admin_username,
        role=admin_role,
        action=f"RESTORE_{cfg['audit_tag']}_SINGLE",
        target=f"{cfg['display_name']} Record {record_id}",
        details="Restored record to active view",
    )
    return res.modified_count > 0

async def unarchive_bulk_records(
    db: AsyncIOMotorDatabase,
    section: str,
    record_ids: List[str],
    admin_user: User,
) -> int:
    cfg = get_section_config(section)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    admin_username = admin_user.username if getattr(admin_user, "username", None) else getattr(admin_user, "email", "admin")
    admin_role = getattr(admin_user, "role", "SUPER_ADMIN")

    id_clauses = []
    for tid in record_ids:
        tid_s = str(tid).strip()
        id_clauses.append({"_id": tid_s})
        id_clauses.append({"id": tid_s})
        if section == "support_tickets":
            id_clauses.append({"ticket_id": tid_s})
        if ObjectId.is_valid(tid_s):
            id_clauses.append({"_id": ObjectId(tid_s)})

    if not id_clauses:
        return 0

    target_match: Dict[str, Any] = {}
    if section == "support_tickets":
        target_match = {
            "$and": [
                {"$or": id_clauses},
                {
                    "$or": [
                        {"is_archived": True},
                        {"is_archived": "true"},
                        {"is_archived": "True"},
                        {"status": "ARCHIVED"},
                    ]
                },
            ]
        }
    else:
        target_match = {"$or": id_clauses, "is_archived": True}

    res = await db[cfg["collection"]].update_many(
        target_match,
        {
            "$set": {
                "is_archived": False,
                "unarchived_at": now_iso,
                "unarchived_by": admin_username,
            }
        },
    )

    restored_count = res.modified_count
    await record_audit_log(
        db=db,
        operator=admin_username,
        role=admin_role,
        action=f"RESTORE_{cfg['audit_tag']}_BULK",
        target=f"{cfg['display_name']} ({restored_count} records restored)",
        details=f"Restored {restored_count} records by ID list",
    )
    return restored_count
