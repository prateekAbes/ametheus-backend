from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_db, require_super_admin, require_organizer
from app.models.user import User
from app.models.placement import Placement, get_utc_now
from app.services.audit import record_audit_log

router = APIRouter()

CANONICAL_DEFAULT_PLACEMENTS: List[Dict[str, Any]] = [
    {
        "id": "homepage_hero_slot_1",
        "placement_key": "homepage_hero_slot_1",
        "page": "homepage",
        "section": "hero",
        "slot_name": "Hero Panel 1 (Top-Left)",
        "target_aspect_ratio": "3:4",
        "target_ratio_value": 0.75,
        "recommended_resolution": "720x960",
        "min_width": 720,
        "min_height": 960,
        "focal_x": 50.0,
        "focal_y": 30.0,
        "zoom": 1.0,
        "fit_mode": "cover",
        "target_id": "valorant",
        "asset_url": "https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=1200&auto=format&fit=crop",
        "description": "Floating top-left panel surrounding central hero title. Polygon clip-path with perspective parallax.",
    },
    {
        "id": "homepage_hero_slot_2",
        "placement_key": "homepage_hero_slot_2",
        "page": "homepage",
        "section": "hero",
        "slot_name": "Hero Panel 2 (Top-Right)",
        "target_aspect_ratio": "9:16",
        "target_ratio_value": 0.5625,
        "recommended_resolution": "720x1280",
        "min_width": 720,
        "min_height": 1280,
        "focal_x": 50.0,
        "focal_y": 25.0,
        "zoom": 1.0,
        "fit_mode": "cover",
        "target_id": "bgmi",
        "asset_url": "https://images.unsplash.com/photo-1538481199705-c710c4e965fc?q=80&w=1200&auto=format&fit=crop",
        "description": "Floating top-right panel surrounding central hero title. Tall vertical polygon with perspective parallax.",
    },
    {
        "id": "homepage_hero_slot_3",
        "placement_key": "homepage_hero_slot_3",
        "page": "homepage",
        "section": "hero",
        "slot_name": "Hero Panel 3 (Bottom-Left)",
        "target_aspect_ratio": "4:5",
        "target_ratio_value": 0.8,
        "recommended_resolution": "640x800",
        "min_width": 640,
        "min_height": 800,
        "focal_x": 50.0,
        "focal_y": 40.0,
        "zoom": 1.0,
        "fit_mode": "cover",
        "target_id": "free-fire",
        "asset_url": "https://images.unsplash.com/photo-1518709268805-4e9042af9f23?q=80&w=1200&auto=format&fit=crop",
        "description": "Floating bottom-left panel surrounding central hero title. Compact perspective shard.",
    },
    {
        "id": "homepage_hero_slot_4",
        "placement_key": "homepage_hero_slot_4",
        "page": "homepage",
        "section": "hero",
        "slot_name": "Hero Panel 4 (Bottom-Right)",
        "target_aspect_ratio": "4:3",
        "target_ratio_value": 1.333,
        "recommended_resolution": "960x720",
        "min_width": 960,
        "min_height": 720,
        "focal_x": 50.0,
        "focal_y": 50.0,
        "zoom": 1.0,
        "fit_mode": "cover",
        "target_id": "counter-strike-2",
        "asset_url": "https://images.unsplash.com/photo-1511512578047-dfb367046420?q=80&w=1200&auto=format&fit=crop",
        "description": "Floating bottom-right panel surrounding central hero title. Landscape perspective shard.",
    },
    {
        "id": "homepage_featured_tournament",
        "placement_key": "homepage_featured_tournament",
        "page": "homepage",
        "section": "featured",
        "slot_name": "Lead Featured Tournament Hero Card",
        "target_aspect_ratio": "16:10",
        "target_ratio_value": 1.6,
        "recommended_resolution": "1600x1000",
        "min_width": 1600,
        "min_height": 1000,
        "focal_x": 50.0,
        "focal_y": 50.0,
        "zoom": 1.0,
        "fit_mode": "cover",
        "target_id": None,
        "asset_url": None,
        "description": "Large primary featured tournament hero card on homepage.",
    },
    {
        "id": "homepage_game_showcase",
        "placement_key": "homepage_game_showcase",
        "page": "homepage",
        "section": "showcase",
        "slot_name": "Game Showcase Section (\"8 Games. One Arena.\")",
        "target_aspect_ratio": "3:4",
        "target_ratio_value": 0.75,
        "recommended_resolution": "900x1200",
        "min_width": 900,
        "min_height": 1200,
        "focal_x": 50.0,
        "focal_y": 40.0,
        "zoom": 1.0,
        "fit_mode": "cover",
        "target_id": None,
        "asset_url": None,
        "description": "Staggered vertical cards in the Game Showcase arena section.",
    },
]

class PlacementUpdatePayload(BaseModel):
    slot_name: Optional[str] = None
    asset_url: Optional[str] = None
    target_id: Optional[str] = None
    focal_x: Optional[float] = Field(None, ge=0.0, le=100.0)
    focal_y: Optional[float] = Field(None, ge=0.0, le=100.0)
    zoom: Optional[float] = Field(None, ge=1.0, le=3.0)
    fit_mode: Optional[str] = None
    target_aspect_ratio: Optional[str] = None
    target_ratio_value: Optional[float] = None
    recommended_resolution: Optional[str] = None
    min_width: Optional[int] = None
    min_height: Optional[int] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    metadata: Optional[Dict[str, Any]] = None

class BulkPlacementsPayload(BaseModel):
    placements: List[Dict[str, Any]]

async def ensure_default_placements(db: AsyncIOMotorDatabase):
    """Seed any canonical placements missing from db.site_placements without overwriting existing assignments."""
    for default_item in CANONICAL_DEFAULT_PLACEMENTS:
        key = default_item["placement_key"]
        existing = await db.site_placements.find_one({"placement_key": key})
        if not existing:
            item = dict(default_item)
            item["_id"] = item["id"]
            item["created_at"] = get_utc_now().isoformat()
            item["updated_at"] = get_utc_now().isoformat()
            await db.site_placements.insert_one(item)

@router.get("", response_model=List[Dict[str, Any]])
async def list_placements(
    page: Optional[str] = None,
    section: Optional[str] = None,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Returns all site and page image placements with their container ratios,
    recommended dimensions, current asset assignments, and focal coordinates.
    """
    await ensure_default_placements(db)

    query: Dict[str, Any] = {}
    if page:
        query["page"] = page
    if section:
        query["section"] = section

    cursor = db.site_placements.find(query).sort("slot_name", 1)
    docs = await cursor.to_list(length=200)

    results = []
    for d in docs:
        d["id"] = str(d.get("_id", d.get("id", "")))
        d.pop("_id", None)
        # Ensure raw datetime objects are serialized as ISO strings
        for date_key in ("created_at", "updated_at"):
            if isinstance(d.get(date_key), datetime):
                d[date_key] = d[date_key].isoformat()
        results.append(d)

    return results

@router.get("/{placement_key}")
async def get_placement(
    placement_key: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Get single placement configuration by key."""
    doc = await db.site_placements.find_one({"placement_key": placement_key})
    if not doc:
        # Check canonical defaults
        def_match = next((x for x in CANONICAL_DEFAULT_PLACEMENTS if x["placement_key"] == placement_key), None)
        if def_match:
            item = dict(def_match)
            item["_id"] = item["id"]
            item["created_at"] = get_utc_now().isoformat()
            item["updated_at"] = get_utc_now().isoformat()
            await db.site_placements.insert_one(item)
            item["id"] = str(item.pop("_id"))
            return item
        raise HTTPException(status_code=404, detail=f"Placement '{placement_key}' not found")

    doc["id"] = str(doc.get("_id", doc.get("id", "")))
    doc.pop("_id", None)
    for date_key in ("created_at", "updated_at"):
        if isinstance(doc.get(date_key), datetime):
            doc[date_key] = doc[date_key].isoformat()
    return doc

@router.put("/{placement_key}")
async def update_placement(
    placement_key: str,
    payload: PlacementUpdatePayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Update or create a placement configuration (requires admin or organizer).
    Persists focal point X/Y%, zoom, fit mode, and assigned asset URL.
    """
    now_iso = get_utc_now().isoformat()
    update_data = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    update_data["updated_at"] = now_iso

    # Find canonical metadata if creating newly
    def_match = next((x for x in CANONICAL_DEFAULT_PLACEMENTS if x["placement_key"] == placement_key), None)
    base_info = dict(def_match) if def_match else {
        "id": placement_key,
        "placement_key": placement_key,
        "page": "custom",
        "section": "custom",
        "slot_name": placement_key.replace("_", " ").title(),
        "created_at": now_iso,
    }

    res = await db.site_placements.find_one_and_update(
        {"placement_key": placement_key},
        {
            "$set": update_data,
            "$setOnInsert": {
                "_id": base_info.get("id", placement_key),
                "created_at": base_info.get("created_at", now_iso),
                "page": base_info.get("page", "custom"),
                "section": base_info.get("section", "custom"),
                "slot_name": base_info.get("slot_name", placement_key),
            },
        },
        upsert=True,
        return_document=True,
    )

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="UPDATE_PLACEMENT",
        target=f"Placement {placement_key}",
        details=f"Updated placement fields: {list(update_data.keys())}",
    )

    res["id"] = str(res.get("_id", res.get("id", "")))
    res.pop("_id", None)
    for date_key in ("created_at", "updated_at"):
        if isinstance(res.get(date_key), datetime):
            res[date_key] = res[date_key].isoformat()

    return res

@router.delete("/{placement_key}/asset")
async def clear_placement_asset(
    placement_key: str,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Clears the assigned asset URL from a placement without deleting the underlying media record.
    """
    now_iso = get_utc_now().isoformat()
    res = await db.site_placements.find_one_and_update(
        {"placement_key": placement_key},
        {
            "$set": {
                "asset_url": None,
                "updated_at": now_iso,
            }
        },
        return_document=True,
    )
    if not res:
        raise HTTPException(status_code=404, detail="Placement not found")

    await record_audit_log(
        db=db,
        operator=admin_user.username,
        role=admin_user.role,
        action="CLEAR_PLACEMENT_ASSET",
        target=f"Placement {placement_key}",
        details="Removed asset assignment from placement",
    )

    res["id"] = str(res.get("_id", res.get("id", "")))
    res.pop("_id", None)
    return res

@router.post("/bulk")
async def bulk_update_placements(
    payload: BulkPlacementsPayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Bulk update multiple placements."""
    now_iso = get_utc_now().isoformat()
    updated_count = 0
    for p in payload.placements:
        key = p.get("placement_key")
        if not key:
            continue
        update_fields = {k: v for k, v in p.items() if k not in ["id", "_id", "placement_key"]}
        update_fields["updated_at"] = now_iso
        await db.site_placements.update_one(
            {"placement_key": key},
            {"$set": update_fields},
            upsert=True,
        )
        updated_count += 1

    return {"status": "success", "updated_count": updated_count}
