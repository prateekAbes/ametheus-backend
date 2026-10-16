import base64
import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Response, status
from pydantic import BaseModel
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_db, require_organizer
from app.models.user import User

router = APIRouter()

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB limit
ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}

def sanitize_svg(content: bytes) -> bytes:
    """Strip malicious executable tags and attributes from SVG vector content."""
    try:
        import re
        text = content.decode("utf-8", errors="ignore")
        cleaned = re.sub(r'(?i)<script[\s\S]*?</script>', '', text)
        cleaned = re.sub(r'(?i)\son\w+\s*=\s*["\'][^"\']*["\']', '', cleaned)
        cleaned = re.sub(r'(?i)href\s*=\s*["\']javascript:[^"\']*["\']', '', cleaned)
        cleaned = re.sub(r'(?i)<foreignObject[\s\S]*?</foreignObject>', '', cleaned)
        return cleaned.encode("utf-8")
    except Exception:
        return content

class Base64UploadPayload(BaseModel):
    data_url: str
    filename: Optional[str] = "upload.png"

def _extract_dimensions(content: bytes, content_type: str):
    """Extract width and height from binary headers without external dependencies."""
    try:
        import struct
        # PNG
        if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 24:
            w, h = struct.unpack(">II", content[16:24])
            return w, h
        # GIF
        if content[:6] in (b"GIF87a", b"GIF89a") and len(content) >= 10:
            w, h = struct.unpack("<HH", content[6:10])
            return w, h
        # JPEG basic SOF0 / SOF2 markers
        if content.startswith(b"\xff\xd8"):
            idx = 2
            length = len(content)
            while idx < length - 9:
                if content[idx] == 0xFF and content[idx + 1] in (0xC0, 0xC1, 0xC2, 0xC3):
                    h, w = struct.unpack(">HH", content[idx + 5 : idx + 9])
                    return w, h
                # Skip marker segment
                idx += 1
    except Exception:
        pass
    return None, None

async def _find_media_references(media_id: str, db: AsyncIOMotorDatabase):
    """
    Finds all active references to a media file across Games and Tournaments.
    """
    refs = []
    target_pattern = media_id

    # Search Games
    games_cursor = db.games.find(
        {},
        {"name": 1, "slug": 1, "poster_url": 1, "banner_url": 1, "logo_url": 1, "media_config": 1},
    )
    async for g in games_cursor:
        gid = str(g.get("_id", g.get("id", "")))
        gname = g.get("name") or g.get("slug") or "Game"

        for field, role in [
            ("poster_url", "game.active_catalog_card"),
            ("banner_url", "game.detail_banner"),
            ("logo_url", "game.logo"),
        ]:
            val = g.get(field) or ""
            if target_pattern in val:
                refs.append({
                    "entity_type": "game",
                    "entity_id": gid,
                    "entity_name": gname,
                    "role": role,
                    "field": field,
                })

        cfg = g.get("media_config") or {}
        if isinstance(cfg, dict):
            for slot, slot_data in cfg.items():
                if isinstance(slot_data, dict):
                    url_val = slot_data.get("asset_url") or ""
                    if target_pattern in url_val:
                        refs.append({
                            "entity_type": "game",
                            "entity_id": gid,
                            "entity_name": gname,
                            "role": f"game.{slot}",
                            "field": f"media_config.{slot}.asset_url",
                        })

    # Search Tournaments
    tournaments_cursor = db.tournaments.find(
        {},
        {"title": 1, "slug": 1, "banner_url": 1, "hero_banner_url": 1, "logo_url": 1, "media_config": 1},
    )
    async for t in tournaments_cursor:
        tid = str(t.get("_id", t.get("id", "")))
        tname = t.get("title") or t.get("slug") or "Tournament"

        for field, role in [
            ("banner_url", "tournament.listing_card"),
            ("hero_banner_url", "tournament.detail_hero"),
            ("logo_url", "tournament.logo"),
        ]:
            val = t.get(field) or ""
            if target_pattern in val:
                refs.append({
                    "entity_type": "tournament",
                    "entity_id": tid,
                    "entity_name": tname,
                    "role": role,
                    "field": field,
                })

        cfg = t.get("media_config") or {}
        if isinstance(cfg, dict):
            for slot, slot_data in cfg.items():
                if isinstance(slot_data, dict):
                    url_val = slot_data.get("asset_url") or ""
                    if target_pattern in url_val:
                        refs.append({
                            "entity_type": "tournament",
                            "entity_id": tid,
                            "entity_name": tname,
                            "role": f"tournament.{slot}",
                            "field": f"media_config.{slot}.asset_url",
                        })

    return refs

async def _clear_media_references(media_id: str, refs: list, db: AsyncIOMotorDatabase):
    """Clears all references to this media file in games and tournaments safely."""
    from bson import ObjectId

    for ref in refs:
        ent_type = ref.get("entity_type")
        ent_id = ref.get("entity_id")
        field = ref.get("field")
        if not ent_id or not field:
            continue

        id_query = {"$or": [{"_id": ent_id}, {"id": ent_id}]}
        if ObjectId.is_valid(ent_id):
            id_query["$or"].append({"_id": ObjectId(ent_id)})

        if ent_type == "game":
            await db.games.update_many(id_query, {"$set": {field: ""}})
        elif ent_type == "tournament":
            await db.tournaments.update_many(id_query, {"$set": {field: ""}})

async def _save_media(
    content: bytes,
    filename: str,
    content_type: str,
    db: AsyncIOMotorDatabase,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    artwork_role: Optional[str] = None,
):
    content_type = (content_type or "image/png").lower().split(";")[0].strip()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File size exceeds maximum limit of 20MB",
        )

    if content_type not in ALLOWED_TYPES and not content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type: {content_type}. Allowed: PNG, JPEG, WEBP, GIF, SVG",
        )

    if content_type == "image/svg+xml" or filename.lower().endswith(".svg"):
        content = sanitize_svg(content)
        content_type = "image/svg+xml"

    w, h = _extract_dimensions(content, content_type)
    media_id = f"img_{uuid.uuid4().hex[:14]}"
    b64_str = base64.b64encode(content).decode("utf-8")
    data_url_str = f"data:{content_type};base64,{b64_str}"

    doc = {
        "id": media_id,
        "_id": media_id,
        "filename": filename,
        "content_type": content_type,
        "size": len(content),
        "width": w,
        "height": h,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "artwork_role": artwork_role,
        "data": b64_str,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    await db.media.insert_one(doc)

    return {
        "status": "success",
        "id": media_id,
        "url": f"/api/v1/media/{media_id}",
        "data_url": data_url_str,
        "filename": filename,
        "size": len(content),
        "width": w,
        "height": h,
        "content_type": content_type,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "artwork_role": artwork_role,
    }

@router.get("")
async def list_media_files(
    search: Optional[str] = None,
    entity_type: Optional[str] = None,
    content_type: Optional[str] = None,
    limit: int = 50,
    skip: int = 0,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    List uploaded database-managed media files with metadata and usage references.
    """
    import re

    query = {}
    if search and search.strip():
        query["filename"] = {"$regex": re.escape(search.strip()), "$options": "i"}
    if content_type and content_type.strip():
        query["content_type"] = {"$regex": re.escape(content_type.strip()), "$options": "i"}

    total = await db.media.count_documents(query)
    cursor = db.media.find(query, {"data": 0}).sort("created_at", -1).skip(skip).limit(min(limit, 100))
    docs = await cursor.to_list(length=limit)

    items = []
    for d in docs:
        mid = str(d.get("_id", d.get("id")))
        refs = await _find_media_references(mid, db)

        # Filter by entity_type if requested
        if entity_type:
            has_ent = any(r.get("entity_type") == entity_type for r in refs) or d.get("entity_type") == entity_type
            if not has_ent:
                continue

        items.append({
            "id": mid,
            "url": f"/api/v1/media/{mid}",
            "filename": d.get("filename", "image.png"),
            "content_type": d.get("content_type", "image/png"),
            "size": d.get("size", 0),
            "width": d.get("width"),
            "height": d.get("height"),
            "created_at": d.get("created_at"),
            "references": refs,
            "in_use": len(refs) > 0,
        })

    return {"items": items, "total": total, "skip": skip, "limit": limit}

@router.post("/upload")
async def upload_media_file(
    file: UploadFile = File(...),
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    artwork_role: Optional[str] = None,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Accepts multipart file upload, saves directly into MongoDB `media` collection,
    and returns accessible URL and data_url.
    """
    content = await file.read()
    content_type = file.content_type or "image/png"
    filename = file.filename or "image.png"
    return await _save_media(
        content,
        filename,
        content_type,
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        artwork_role=artwork_role,
    )

@router.post("/upload-base64")
async def upload_media_base64(
    payload: Base64UploadPayload,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Accepts JSON payload with base64 data URL, saves directly into MongoDB `media` collection.
    """
    data_url = payload.data_url
    filename = payload.filename or "upload.png"
    content_type = "image/png"

    if "," in data_url:
        header, encoded = data_url.split(",", 1)
        content_type = header.split(";")[0].replace("data:", "") or "image/png"
        content = base64.b64decode(encoded)
    else:
        content = base64.b64decode(data_url)

    return await _save_media(content, filename, content_type, db)

@router.get("/{media_id}/references")
async def get_media_references(
    media_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Inspect active references to a media file across Games and Tournaments.
    """
    doc = await db.media.find_one({"$or": [{"_id": media_id}, {"id": media_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Media file not found")

    clean_id = str(doc.get("_id", doc.get("id")))
    refs = await _find_media_references(clean_id, db)
    return {
        "media_id": clean_id,
        "filename": doc.get("filename"),
        "in_use": len(refs) > 0,
        "references_count": len(refs),
        "references": refs,
    }

@router.delete("/{media_id}")
async def delete_media_file(
    media_id: str,
    force: bool = False,
    current_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Permanently deletes a stored media file from the database.
    Performs reference-aware checking against all Games and Tournaments.
    If force=False and references exist, returns 409 Conflict with reference details.
    If force=True, clears all referencing fields before permanent physical deletion.
    """
    doc = await db.media.find_one({"$or": [{"_id": media_id}, {"id": media_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Media file not found")

    clean_id = str(doc.get("_id", doc.get("id")))
    refs = await _find_media_references(clean_id, db)

    if refs and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": f"Asset is currently in use across {len(refs)} placement(s).",
                "in_use": True,
                "references": refs,
            },
        )

    if refs and force:
        await _clear_media_references(clean_id, refs, db)

    # Permanent deletion of the stored physical document in MongoDB
    del_res = await db.media.delete_one({"$or": [{"_id": clean_id}, {"id": clean_id}]})
    if del_res.deleted_count == 0:
        raise HTTPException(status_code=500, detail="Failed to delete media record")

    return {
        "status": "success",
        "message": "Asset permanently deleted from storage.",
        "id": clean_id,
        "cleared_references_count": len(refs),
    }

@router.api_route("/{media_id}", methods=["GET", "HEAD"])
async def get_media_file(
    media_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Streams the stored binary image with appropriate Content-Type and Cache-Control headers.
    """
    doc = await db.media.find_one({"$or": [{"_id": media_id}, {"id": media_id}]})
    if not doc:
        raise HTTPException(status_code=404, detail="Media file not found")

    content_type = doc.get("content_type", "image/png")
    b64_data = doc.get("data", "")
    try:
        raw_bytes = base64.b64decode(b64_data)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to decode stored media")

    return Response(
        content=raw_bytes,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Disposition": f'inline; filename="{doc.get("filename", "image.png")}"',
        },
    )
