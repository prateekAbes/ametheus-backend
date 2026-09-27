from typing import List, Optional
from datetime import datetime, timezone
import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from app.api.deps import get_db, require_organizer
from app.models.user import User
from app.models.regulation import GameRegulation, RegulationTable
from app.schemas.regulation import (
    GameRegulationCreate,
    GameRegulationUpdate,
    GameRegulationResponse,
    RegulationReorderRequest,
    RegulationTableCreate,
    RegulationTableUpdate,
    RegulationTableResponse,
)

router = APIRouter()

SEED_REGULATIONS = [
    {
        "_id": "reg_bgmi",
        "id": "reg_bgmi",
        "game_name": "BGMI",
        "chapter_code": "BGMI-01",
        "title": "Battlegrounds Mobile India (BGMI)",
        "format": "Squads 4v4 / Battle Royale | Multi-Match Points System",
        "server": "Official Krafton Custom Esports Room (Mumbai Data Center)",
        "map_pool": ["Erangel", "Miramar", "Sanhok"],
        "rules": [
            "Roster Size: 4 active players per squad. All players must use registered in-game IDs (UIDs).",
            "Scoring System: Official BGIS 10-point matrix (1st: 10pts, 2nd: 6pts, 3rd: 5pts, 4th: 4pts, 5th: 3pts, 6th: 2pts, 7th-8th: 1pt) + 1 point per kill.",
            "Devices: Mobile phones only (iOS / Android). Emulators, iPad, tablets, triggers, and macros are strictly prohibited.",
            "Result Evidence: Captains must submit an uncropped end-game results screenshot showing team rank and kills.",
        ],
        "order": 1,
        "is_active": True,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    },
    {
        "_id": "reg_valorant",
        "id": "reg_valorant",
        "game_name": "VALORANT",
        "chapter_code": "VAL-01",
        "title": "Valorant (5v5 Tactical Shooter)",
        "format": "Standard 5v5 | Single Elimination Bracket | Best-of-1 (Finals BO3)",
        "server": "AWS Mumbai (ap-south-1) — 128 Tick",
        "map_pool": ["Ascent", "Haven", "Bind", "Split", "Sunset", "Lotus", "Abyss"],
        "rules": [
            "Roster Size: Exactly 5 active players + up to 1 optional substitute registered before roster lock.",
            "Overtime: Standard 2-round win margin overtime (Win by 2 rounds).",
            "Anti-Cheat: Riot Vanguard must be running with TPM 2.0 and Secure Boot enabled. Any Vanguard violation results in permanent ban.",
            "Cheats & Glitches: Deliberate use of map exploits (Cypher camera glitches, Sova dart out-of-bounds) forfeits the round.",
            "Pauses: Each team receives one 60-second tactical timeout per half. Max 5 minutes technical pause per match upon arbiter approval.",
        ],
        "order": 2,
        "is_active": True,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    },
    {
        "_id": "reg_freefire",
        "id": "reg_freefire",
        "game_name": "Free Fire MAX",
        "chapter_code": "FF-01",
        "title": "Free Fire MAX",
        "format": "Clash Squad 4v4 / Battle Royale",
        "server": "India Region Dedicated Esports Server",
        "map_pool": ["Bermuda", "Purgatory", "Kalahari"],
        "rules": [
            "Roster Size: 4 players per roster registered under identical team tag.",
            "Emulators: PC emulators (BlueStacks, LDPlayer, Nox) are strictly banned. Violations trigger immediate bracket forfeiture.",
            "Character Skills: Gun attribute skins must be set to 'Off/Default' when specified by the tournament lobby host.",
            "Proof of Victory: Full victory screen screenshot displaying all elimination counts must be submitted within 15 minutes.",
        ],
        "order": 3,
        "is_active": True,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    },
    {
        "_id": "reg_codm",
        "id": "reg_codm",
        "game_name": "Call of Duty: Mobile",
        "chapter_code": "COD-01",
        "title": "Call of Duty: Mobile (CODM)",
        "format": "5v5 Search & Destroy / Hardpoint",
        "server": "India South Low-Latency Dedicated Matchmaking",
        "map_pool": ["Crash", "Firing Range", "Raid", "Summit", "Standoff", "Tunisia"],
        "rules": [
            "Roster Size: 5 players per squad. Substitutes must be listed prior to tournament check-in.",
            "Banned Equipment: NA-45, Akimbo perks, Persistence perk, Heartbeat Sensor, and Operator Skills when specified.",
            "Touch Controls Only: Controllers and emulators are completely barred from certified competitive cups.",
        ],
        "order": 4,
        "is_active": True,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    },
    {
        "_id": "reg_cs2",
        "id": "reg_cs2",
        "game_name": "Counter-Strike 2",
        "chapter_code": "CS2-01",
        "title": "Counter-Strike 2 (5v5 MR12)",
        "format": "5v5 Competitive MR12 | Single Elimination",
        "server": "Valve South Asia (ap-south-1) Dedicated Server",
        "map_pool": ["Mirage", "Inferno", "Nuke", "Ancient", "Anubis", "Dust II"],
        "rules": [
            "Roster Size: 5 players per squad + 1 optional substitute registered before check-in.",
            "Anti-Cheat: VAC Live and server-side integrity telemetry active at all times.",
            "Overtime: MR3 (first to 4 rounds) with 10k start money.",
            "Tactical Pauses: 1 tactical pause of 30 seconds per half allowed per team.",
        ],
        "order": 5,
        "is_active": True,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    },
]

SEED_TABLE = {
    "_id": "tbl_discipline_matrix",
    "id": "tbl_discipline_matrix",
    "title": "Game Discipline Regulations Master Matrix",
    "description": "Standardized competitive framework, dynamic chapter codes, server regions, and mandatory rules.",
    "columns": ["Game", "Chapter Code", "Format", "Server / Region", "Key Regulation"],
    "rows": [
        ["BGMI", "BGMI-01", "Squads 4v4 / Battle Royale", "Krafton Dedicated Mumbai", "BGIS 10-point scoring; Mobile phones only (emulators banned)"],
        ["VALORANT", "VAL-01", "Standard 5v5 Tactical", "AWS Mumbai (ap-south-1) 128T", "Riot Vanguard TPM 2.0 required; standard overtime rules"],
        ["Free Fire MAX", "FF-01", "Clash Squad 4v4 / BR", "India Dedicated Esports", "Default gun attributes only; PC emulators strictly barred"],
        ["COD Mobile", "COD-01", "5v5 Search & Destroy", "India South Low-Latency", "Touch controls only; heartbeats and persistence banned"],
        ["CS2", "CS2-01", "Competitive 5v5 MR12", "Valve South Asia ap-south-1", "VAC Live & server anti-cheat; standard timeout protocols"],
    ],
    "order": 1,
    "is_active": True,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
}


async def _ensure_seed_regulations(db: AsyncIOMotorDatabase):
    count = await db.game_regulations.count_documents({})
    if count == 0:
        for reg in SEED_REGULATIONS:
            await db.game_regulations.update_one(
                {"_id": reg["_id"]},
                {"$setOnInsert": reg},
                upsert=True,
            )

    tbl_count = await db.regulation_tables.count_documents({})
    if tbl_count == 0:
        await db.regulation_tables.update_one(
            {"_id": SEED_TABLE["_id"]},
            {"$setOnInsert": SEED_TABLE},
            upsert=True,
        )


def _clean_doc(doc: dict) -> dict:
    item = dict(doc)
    item["id"] = str(item.get("_id", item.get("id", "")))
    return item


@router.get("", response_model=List[GameRegulationResponse])
async def list_regulations(
    all: bool = False,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Retrieve all tournament discipline regulations, sorted by configured order."""
    await _ensure_seed_regulations(db)
    query = {} if all else {"is_active": True}
    docs = await db.game_regulations.find(query).sort("order", 1).to_list(length=200)
    return [_clean_doc(d) for d in docs]


@router.post("", response_model=GameRegulationResponse, status_code=status.HTTP_201_CREATED)
async def create_regulation(
    payload: GameRegulationCreate,
    admin: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to create a new game regulation."""
    now_iso = datetime.now(timezone.utc).isoformat()
    reg_id = f"reg_{uuid.uuid4().hex[:10]}"

    # Auto compute next order if not specified or 0
    order = payload.order
    if not order:
        max_order_doc = await db.game_regulations.find().sort("order", -1).limit(1).to_list(1)
        order = (max_order_doc[0].get("order", 0) + 1) if max_order_doc else 1

    doc = {
        "_id": reg_id,
        "id": reg_id,
        "game_name": payload.game_name.strip(),
        "chapter_code": payload.chapter_code.strip().upper(),
        "title": payload.title.strip(),
        "format": payload.format.strip(),
        "server": payload.server.strip(),
        "map_pool": payload.map_pool,
        "rules": payload.rules,
        "order": order,
        "is_active": payload.is_active if payload.is_active is not None else True,
        "created_at": now_iso,
        "updated_at": now_iso,
    }

    await db.game_regulations.insert_one(doc)
    return _clean_doc(doc)


@router.put("/{reg_id}", response_model=GameRegulationResponse)
async def update_regulation(
    reg_id: str,
    payload: GameRegulationUpdate,
    admin: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to edit an existing discipline regulation."""
    existing = await db.game_regulations.find_one({"$or": [{"_id": reg_id}, {"id": reg_id}]})
    if not existing:
        raise HTTPException(status_code=404, detail=f"Regulation '{reg_id}' not found.")

    update_data = {}
    if payload.game_name is not None:
        update_data["game_name"] = payload.game_name.strip()
    if payload.chapter_code is not None:
        update_data["chapter_code"] = payload.chapter_code.strip().upper()
    if payload.title is not None:
        update_data["title"] = payload.title.strip()
    if payload.format is not None:
        update_data["format"] = payload.format.strip()
    if payload.server is not None:
        update_data["server"] = payload.server.strip()
    if payload.map_pool is not None:
        update_data["map_pool"] = payload.map_pool
    if payload.rules is not None:
        update_data["rules"] = payload.rules
    if payload.order is not None:
        update_data["order"] = payload.order
    if payload.is_active is not None:
        update_data["is_active"] = payload.is_active

    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()

    await db.game_regulations.update_one({"_id": existing["_id"]}, {"$set": update_data})
    updated = await db.game_regulations.find_one({"_id": existing["_id"]})
    return _clean_doc(updated)


@router.delete("/{reg_id}")
async def delete_regulation(
    reg_id: str,
    admin: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to delete a discipline regulation."""
    res = await db.game_regulations.delete_one({"$or": [{"_id": reg_id}, {"id": reg_id}]})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail=f"Regulation '{reg_id}' not found.")
    return {"status": "success", "message": f"Regulation '{reg_id}' deleted successfully."}


@router.post("/reorder")
async def reorder_regulations(
    payload: RegulationReorderRequest,
    admin: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to reorder regulations given a list of IDs in sequence."""
    for idx, r_id in enumerate(payload.ordered_ids):
        await db.game_regulations.update_one(
            {"$or": [{"_id": r_id}, {"id": r_id}]},
            {"$set": {"order": idx + 1, "updated_at": datetime.now(timezone.utc).isoformat()}},
        )
    return {"status": "success", "message": "Regulations reordered successfully."}


# -------------------------------------------------------------
# DYNAMIC TABLE BUILDER ENDPOINTS
# -------------------------------------------------------------

@router.get("/tables", response_model=List[RegulationTableResponse])
async def list_regulation_tables(
    all: bool = False,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Retrieve all configurable regulation tables."""
    await _ensure_seed_regulations(db)
    query = {} if all else {"is_active": True}
    docs = await db.regulation_tables.find(query).sort("order", 1).to_list(length=100)
    return [_clean_doc(d) for d in docs]


@router.post("/tables", response_model=RegulationTableResponse, status_code=status.HTTP_201_CREATED)
async def create_regulation_table(
    payload: RegulationTableCreate,
    admin: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to create a configurable regulation table."""
    now_iso = datetime.now(timezone.utc).isoformat()
    tbl_id = f"tbl_{uuid.uuid4().hex[:10]}"

    order = payload.order
    if not order:
        max_order_doc = await db.regulation_tables.find().sort("order", -1).limit(1).to_list(1)
        order = (max_order_doc[0].get("order", 0) + 1) if max_order_doc else 1

    doc = {
        "_id": tbl_id,
        "id": tbl_id,
        "title": payload.title.strip(),
        "description": (payload.description or "").strip(),
        "columns": [c.strip() for c in payload.columns if c.strip()],
        "rows": payload.rows,
        "order": order,
        "is_active": payload.is_active if payload.is_active is not None else True,
        "created_at": now_iso,
        "updated_at": now_iso,
    }

    await db.regulation_tables.insert_one(doc)
    return _clean_doc(doc)


@router.put("/tables/{tbl_id}", response_model=RegulationTableResponse)
async def update_regulation_table(
    tbl_id: str,
    payload: RegulationTableUpdate,
    admin: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to update an existing regulation table."""
    existing = await db.regulation_tables.find_one({"$or": [{"_id": tbl_id}, {"id": tbl_id}]})
    if not existing:
        raise HTTPException(status_code=404, detail=f"Table '{tbl_id}' not found.")

    update_data = {}
    if payload.title is not None:
        update_data["title"] = payload.title.strip()
    if payload.description is not None:
        update_data["description"] = payload.description.strip()
    if payload.columns is not None:
        update_data["columns"] = [c.strip() for c in payload.columns if c.strip()]
    if payload.rows is not None:
        update_data["rows"] = payload.rows
    if payload.order is not None:
        update_data["order"] = payload.order
    if payload.is_active is not None:
        update_data["is_active"] = payload.is_active

    update_data["updated_at"] = datetime.now(timezone.utc).isoformat()

    await db.regulation_tables.update_one({"_id": existing["_id"]}, {"$set": update_data})
    updated = await db.regulation_tables.find_one({"_id": existing["_id"]})
    return _clean_doc(updated)


@router.delete("/tables/{tbl_id}")
async def delete_regulation_table(
    tbl_id: str,
    admin: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to delete a regulation table."""
    res = await db.regulation_tables.delete_one({"$or": [{"_id": tbl_id}, {"id": tbl_id}]})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail=f"Table '{tbl_id}' not found.")
    return {"status": "success", "message": f"Table '{tbl_id}' deleted successfully."}
