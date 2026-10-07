from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_db, require_organizer, require_super_admin
from app.models.user import User
from app.models.crest import Crest
from app.schemas import CrestResponse, CrestCreate, CrestUpdate
from app.services.audit import record_audit_log
from app.models.base import get_utc_now

router = APIRouter()
admin_router = APIRouter()

# High-resolution vector data URIs for curated esports crests
DEFAULT_CRESTS = [
    {
        "id": "crest_valkyrie_aegis",
        "name": "Valkyrie Aegis",
        "description": "Celestial golden wings and diamond core awarded to frontline champions.",
        "image_url": "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><defs><linearGradient id='g1' x1='0%' y1='0%' x2='100%' y2='100%'><stop offset='0%' stop-color='%23ffd700'/><stop offset='100%' stop-color='%23ff8800'/></linearGradient></defs><polygon points='50,10 90,30 80,75 50,95 20,75 10,30' fill='%2312141d' stroke='%23ffd700' stroke-width='3'/><polygon points='50,22 80,38 72,70 50,84 28,70 20,38' fill='none' stroke='%23ff8800' stroke-width='1.5' stroke-dasharray='3,3'/><path d='M50,28 L62,50 L50,72 L38,50 Z' fill='url(%23g1)'/><circle cx='50' cy='50' r='5' fill='%23ffffff'/></svg>",
        "is_active": True,
        "category": "OPERATOR",
    },
    {
        "id": "crest_cyber_falcon",
        "name": "Cyber Falcon",
        "description": "Cyan aerodynamic predator insignia for hyper-accurate marksmen.",
        "image_url": "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><defs><linearGradient id='g2' x1='0%' y1='0%' x2='100%' y2='100%'><stop offset='0%' stop-color='%233ef2ff'/><stop offset='100%' stop-color='%230077ff'/></linearGradient></defs><polygon points='50,8 92,28 85,78 50,96 15,78 8,28' fill='%230c0e15' stroke='%233ef2ff' stroke-width='3'/><path d='M25,35 L50,20 L75,35 L68,60 L50,80 L32,60 Z' fill='none' stroke='%233ef2ff' stroke-width='2'/><polygon points='50,30 65,45 50,65 35,45' fill='url(%23g2)'/><line x1='50' y1='15' x2='50' y2='85' stroke='%23ffffff' stroke-width='1.5'/></svg>",
        "is_active": True,
        "category": "OPERATOR",
    },
    {
        "id": "crest_neon_vanguard",
        "name": "Neon Vanguard",
        "description": "Ultraviolet high-tech hexagonal tactical barrier emblem.",
        "image_url": "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><defs><linearGradient id='g3' x1='0%' y1='0%' x2='100%' y2='100%'><stop offset='0%' stop-color='%23a855f7'/><stop offset='100%' stop-color='%23ec4899'/></linearGradient></defs><polygon points='50,12 88,32 88,68 50,88 12,68 12,32' fill='%2312141d' stroke='%23a855f7' stroke-width='3'/><polygon points='50,24 76,38 76,62 50,76 24,62 24,38' fill='none' stroke='%23ec4899' stroke-width='2'/><circle cx='50' cy='50' r='14' fill='url(%23g3)'/><polygon points='50,42 56,50 50,58 44,50' fill='%23ffffff'/></svg>",
        "is_active": True,
        "category": "OPERATOR",
    },
    {
        "id": "crest_shadow_ronin",
        "name": "Shadow Ronin",
        "description": "Dark crimson stealth warrior mask crest for lethal clutch specialists.",
        "image_url": "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><defs><linearGradient id='g4' x1='0%' y1='0%' x2='100%' y2='100%'><stop offset='0%' stop-color='%23ff3b4e'/><stop offset='100%' stop-color='%23880015'/></linearGradient></defs><polygon points='50,10 90,25 78,80 50,95 22,80 10,25' fill='%23150507' stroke='%23ff3b4e' stroke-width='3'/><path d='M30,35 L50,55 L70,35 L80,50 L50,85 L20,50 Z' fill='url(%23g4)'/><polygon points='50,20 60,35 40,35' fill='%23ffffff'/><circle cx='50' cy='62' r='4' fill='%23ffffff'/></svg>",
        "is_active": True,
        "category": "OPERATOR",
    },
    {
        "id": "crest_solaris_phoenix",
        "name": "Solaris Phoenix",
        "description": "Blazing solar flame rebirth emblem for unshakeable comeback squads.",
        "image_url": "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><defs><linearGradient id='g5' x1='0%' y1='0%' x2='100%' y2='100%'><stop offset='0%' stop-color='%23ff7a1a'/><stop offset='100%' stop-color='%23ff0055'/></linearGradient></defs><polygon points='50,10 88,32 78,78 50,92 22,78 12,32' fill='%23180805' stroke='%23ff7a1a' stroke-width='3'/><path d='M50,18 C65,35 75,55 70,72 C65,60 55,52 50,60 C45,52 35,60 30,72 C25,55 35,35 50,18 Z' fill='url(%23g5)'/><circle cx='50' cy='42' r='5' fill='%23ffffff'/></svg>",
        "is_active": True,
        "category": "OPERATOR",
    },
    {
        "id": "crest_hydra_apex",
        "name": "Hydra Apex",
        "description": "Emerald multi-pronged competitive serpent crest signifying tactical depth.",
        "image_url": "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><defs><linearGradient id='g6' x1='0%' y1='0%' x2='100%' y2='100%'><stop offset='0%' stop-color='%233dff9a'/><stop offset='100%' stop-color='%23008855'/></linearGradient></defs><polygon points='50,10 90,30 82,75 50,94 18,75 10,30' fill='%2305150d' stroke='%233dff9a' stroke-width='3'/><path d='M32,45 L50,22 L68,45 L50,75 Z' fill='url(%23g6)' stroke='%233dff9a' stroke-width='1.5'/><circle cx='50' cy='48' r='6' fill='%23ffffff'/></svg>",
        "is_active": True,
        "category": "OPERATOR",
    },
    {
        "id": "crest_abyssal_kraken",
        "name": "Abyssal Kraken",
        "description": "Deep oceanic conqueror badge representing zone dominance.",
        "image_url": "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><defs><linearGradient id='g7' x1='0%' y1='0%' x2='100%' y2='100%'><stop offset='0%' stop-color='%2300e5ff'/><stop offset='100%' stop-color='%234a00e0'/></linearGradient></defs><polygon points='50,12 86,28 78,82 50,94 22,82 14,28' fill='%23070b18' stroke='%2300e5ff' stroke-width='3'/><circle cx='50' cy='46' r='20' fill='url(%23g7)'/><polygon points='50,28 62,46 50,64 38,46' fill='%23ffffff'/></svg>",
        "is_active": True,
        "category": "OPERATOR",
    },
    {
        "id": "crest_titan_guard",
        "name": "Titan Guard",
        "description": "Obsidian reinforced vanguard crest for unbreakable defensive shotcallers.",
        "image_url": "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><defs><linearGradient id='g8' x1='0%' y1='0%' x2='100%' y2='100%'><stop offset='0%' stop-color='%23e0e7ff'/><stop offset='100%' stop-color='%236366f1'/></linearGradient></defs><polygon points='50,10 92,26 84,80 50,96 16,80 8,26' fill='%230e101f' stroke='%23818cf8' stroke-width='3'/><path d='M30,30 L70,30 L62,70 L50,82 L38,70 Z' fill='url(%23g8)'/><polygon points='50,38 58,54 50,70 42,54' fill='%2312141d'/></svg>",
        "is_active": True,
        "category": "OPERATOR",
    },
]

async def seed_crests_if_needed(db: AsyncIOMotorDatabase):
    count = await db.crests.count_documents({})
    if count == 0:
        now = get_utc_now()
        for sc in DEFAULT_CRESTS:
            doc = {
                "_id": sc["id"],
                "id": sc["id"],
                "name": sc["name"],
                "description": sc["description"],
                "image_url": sc["image_url"],
                "is_active": sc["is_active"],
                "category": sc.get("category", "OPERATOR"),
                "created_at": now,
                "updated_at": now,
            }
            await db.crests.insert_one(doc)

def serialize_crest_doc(doc: dict) -> CrestResponse:
    cid = str(doc.get("_id", doc.get("id")))
    ca_str = None
    ua_str = None
    ca = doc.get("created_at")
    ua = doc.get("updated_at")
    if isinstance(ca, datetime):
        ca_str = ca.isoformat()
    elif ca:
        ca_str = str(ca)
    if isinstance(ua, datetime):
        ua_str = ua.isoformat()
    elif ua:
        ua_str = str(ua)

    return CrestResponse(
        id=cid,
        name=doc.get("name", "Unnamed Crest"),
        description=doc.get("description"),
        image_url=doc.get("image_url", ""),
        is_active=bool(doc.get("is_active", True)),
        category=doc.get("category", "OPERATOR"),
        created_at=ca_str,
        updated_at=ua_str,
    )

# ----------------- PUBLIC ENDPOINTS -----------------
@router.get("", response_model=List[CrestResponse])
async def list_available_crests(
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Public listing of all currently active player/operator crests.
    Used dynamically by user profile and settings avatar selectors.
    """
    await seed_crests_if_needed(db)
    cursor = db.crests.find({"is_active": True}).sort("created_at", 1)
    docs = await cursor.to_list(length=200)
    return [serialize_crest_doc(d) for d in docs]

# ----------------- ADMIN ENDPOINTS -----------------
@admin_router.get("", response_model=List[CrestResponse])
async def list_all_crests_admin(
    search: Optional[str] = Query(None),
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Admin listing of all player crests (both active and disabled).
    Restricted to Organizer and Super Admin roles.
    """
    await seed_crests_if_needed(db)
    query = {}
    if search:
        query["$or"] = [
            {"name": {"$regex": search, "$options": "i"}},
            {"description": {"$regex": search, "$options": "i"}},
        ]
    cursor = db.crests.find(query).sort("created_at", -1)
    docs = await cursor.to_list(length=200)
    return [serialize_crest_doc(d) for d in docs]

@admin_router.post("", response_model=CrestResponse, status_code=status.HTTP_201_CREATED)
async def create_crest_admin(
    payload: CrestCreate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Add a new player crest to the platform catalog.
    Restricted to Organizer and Super Admin roles.
    """
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="Crest name cannot be empty.")
    if not payload.image_url.strip():
        raise HTTPException(status_code=400, detail="Crest image URL is required.")

    now = get_utc_now()
    crest = Crest(
        name=payload.name.strip(),
        description=payload.description.strip() if payload.description else None,
        image_url=payload.image_url.strip(),
        is_active=payload.is_active,
        category=payload.category or "OPERATOR",
        created_at=now,
        updated_at=now,
    )
    doc = crest.to_mongo()
    await db.crests.insert_one(doc)

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="CREATE_CREST",
        target=f"Crest {crest.id} ({crest.name})",
        details=f"Created crest {crest.name}",
    )

    return serialize_crest_doc(doc)

@admin_router.put("/{crest_id}", response_model=CrestResponse)
async def update_crest_admin(
    crest_id: str,
    payload: CrestUpdate,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Update crest metadata, title, description, image, or toggle active status.
    Restricted to Organizer and Super Admin roles.
    """
    doc = await db.crests.find_one({"$or": [{"_id": crest_id}, {"id": crest_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Crest not found")

    update_fields = {}
    if payload.name is not None:
        if not payload.name.strip():
            raise HTTPException(status_code=400, detail="Crest name cannot be empty.")
        update_fields["name"] = payload.name.strip()
    if payload.description is not None:
        update_fields["description"] = payload.description.strip() if payload.description else None
    if payload.image_url is not None:
        if not payload.image_url.strip():
            raise HTTPException(status_code=400, detail="Crest image URL cannot be empty.")
        update_fields["image_url"] = payload.image_url.strip()
    if payload.is_active is not None:
        update_fields["is_active"] = bool(payload.is_active)
    if payload.category is not None:
        update_fields["category"] = payload.category

    if update_fields:
        update_fields["updated_at"] = get_utc_now()
        await db.crests.update_one({"_id": doc["_id"]}, {"$set": update_fields})

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="UPDATE_CREST",
        target=f"Crest {crest_id}",
        details=f"Updated crest {crest_id}",
    )

    refreshed = await db.crests.find_one({"_id": doc["_id"]})
    return serialize_crest_doc(refreshed)

@admin_router.delete("/{crest_id}")
async def delete_crest_admin(
    crest_id: str,
    admin_user: User = Depends(require_super_admin),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Delete a crest from the platform catalog.
    Restricted to Super Admin role.
    """
    doc = await db.crests.find_one({"$or": [{"_id": crest_id}, {"id": crest_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Crest not found")

    await db.crests.delete_one({"_id": doc["_id"]})

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="DELETE_CREST",
        target=f"Crest {crest_id} ({doc.get('name')})",
        details=f"Deleted crest {crest_id}",
    )

    return {"status": "success", "message": f"Crest '{doc.get('name')}' successfully deleted."}
