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

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB limit
ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}

class Base64UploadPayload(BaseModel):
    data_url: str
    filename: Optional[str] = "upload.png"

async def _save_media(content: bytes, filename: str, content_type: str, db: AsyncIOMotorDatabase):
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File size exceeds maximum limit of 10MB",
        )

    if content_type not in ALLOWED_TYPES and not content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type: {content_type}. Allowed: PNG, JPEG, WEBP, GIF, SVG",
        )

    media_id = f"img_{uuid.uuid4().hex[:14]}"
    b64_str = base64.b64encode(content).decode("utf-8")
    data_url_str = f"data:{content_type};base64,{b64_str}"

    doc = {
        "id": media_id,
        "_id": media_id,
        "filename": filename,
        "content_type": content_type,
        "size": len(content),
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
        "content_type": content_type,
    }

@router.post("/upload")
async def upload_media_file(
    file: UploadFile = File(...),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Accepts multipart file upload, saves directly into MongoDB `media` collection,
    and returns accessible URL and data_url.
    """
    content = await file.read()
    content_type = file.content_type or "image/png"
    filename = file.filename or "image.png"
    return await _save_media(content, filename, content_type, db)

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
 
class PageMediaConfigPayload(BaseModel):
    page_id: str
    title: Optional[str] = None
    media_type: str = "gif"  # "gif" or "image"
    media_url: str
    fallback_url: Optional[str] = None
    is_active: bool = True
    opacity: Optional[float] = 0.35

DEFAULT_PAGE_MEDIA = {
    "home": {
        "page_id": "home",
        "title": "Homepage Hero",
        "media_type": "gif",
        "media_url": "https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExM3Z0ZWFmNnN2cWJxeHoxbjlyM3cydXBnNDB1NnY2d2hkYWFxdG00MSZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/3oKIPnAiaMCws8nOsE/giphy.gif",
        "fallback_url": "https://images.unsplash.com/photo-1542751371-adc38448a05e?auto=format&fit=crop&w=1600&q=80",
        "is_active": True,
        "opacity": 0.40,
    },
    "tournaments": {
        "page_id": "tournaments",
        "title": "Tournaments Hub",
        "media_type": "image",
        "media_url": "https://images.unsplash.com/photo-1511512578047-dfb367046420?auto=format&fit=crop&w=1600&q=80",
        "fallback_url": "https://images.unsplash.com/photo-1542751371-adc38448a05e?auto=format&fit=crop&w=1600&q=80",
        "is_active": True,
        "opacity": 0.25,
    },
    "tournament_detail": {
        "page_id": "tournament_detail",
        "title": "Tournament Arena",
        "media_type": "image",
        "media_url": "https://images.unsplash.com/photo-1538481199705-c710c4e965fc?auto=format&fit=crop&w=1600&q=80",
        "fallback_url": "https://images.unsplash.com/photo-1542751371-adc38448a05e?auto=format&fit=crop&w=1600&q=80",
        "is_active": True,
        "opacity": 0.30,
    },
    "matches": {
        "page_id": "matches",
        "title": "Live Matches & Schedules",
        "media_type": "gif",
        "media_url": "https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExcGhrMTljbnN3Z280ZXJ4OXl0YWtzc2t5NDc2cmE3dHdpcW00Mms4cyZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/l0HlOvJ7YGIm9PQDm/giphy.gif",
        "fallback_url": "https://images.unsplash.com/photo-1550745165-9bc0b252726f?auto=format&fit=crop&w=1600&q=80",
        "is_active": True,
        "opacity": 0.25,
    },
    "leaderboard": {
        "page_id": "leaderboard",
        "title": "Global Leaderboard",
        "media_type": "gif",
        "media_url": "https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExOHY5b3ZkZnVla2FvYmpuc2w2anYxejZzMXVsaGR6cWhreHhkcjl2ayZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/26AHONQyp1uz480T6/giphy.gif",
        "fallback_url": "https://images.unsplash.com/photo-1563089145-599997674d42?auto=format&fit=crop&w=1600&q=80",
        "is_active": True,
        "opacity": 0.25,
    },
    "teams": {
        "page_id": "teams",
        "title": "Teams & Squads Hub",
        "media_type": "gif",
        "media_url": "https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExM3Z0ZWFmNnN2cWJxeHoxbjlyM3cydXBnNDB1NnY2d2hkYWFxdG00MSZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/3oKIPnAiaMCws8nOsE/giphy.gif",
        "fallback_url": "https://images.unsplash.com/photo-1542751371-adc38448a05e?auto=format&fit=crop&w=1600&q=80",
        "is_active": True,
        "opacity": 0.25,
    },
    "games": {
        "page_id": "games",
        "title": "Games Directory",
        "media_type": "image",
        "media_url": "https://images.unsplash.com/photo-1511512578047-dfb367046420?auto=format&fit=crop&w=1600&q=80",
        "fallback_url": "https://images.unsplash.com/photo-1542751371-adc38448a05e?auto=format&fit=crop&w=1600&q=80",
        "is_active": True,
        "opacity": 0.25,
    },
}

@router.get("/pages")
async def list_all_page_media(db: AsyncIOMotorDatabase = Depends(get_db)):
    """Return configured visual media for all supported pages (merged with defaults)."""
    configs = await db.page_media.find().to_list(length=100)
    config_map = {c["page_id"]: c for c in configs if "page_id" in c}

    results = []
    for pid, default_cfg in DEFAULT_PAGE_MEDIA.items():
        if pid in config_map:
            db_doc = config_map[pid]
            merged = {**default_cfg, **db_doc}
            merged["_id"] = str(db_doc.get("_id", pid))
            results.append(merged)
        else:
            results.append(default_cfg)

    for pid, c in config_map.items():
        if pid not in DEFAULT_PAGE_MEDIA:
            item = dict(c)
            item["_id"] = str(c.get("_id", pid))
            results.append(item)

    return results

@router.get("/pages/{page_id}")
async def get_page_media(page_id: str, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Return the active visual media configuration for a specific page."""
    doc = await db.page_media.find_one({"page_id": page_id})
    if doc:
        doc["_id"] = str(doc.get("_id", page_id))
        return doc

    default_cfg = DEFAULT_PAGE_MEDIA.get(page_id)
    if default_cfg:
        return default_cfg

    return {
        "page_id": page_id,
        "title": page_id.replace("_", " ").title(),
        "media_type": "image",
        "media_url": "",
        "fallback_url": "",
        "is_active": False,
        "opacity": 0.35,
    }

@router.put("/pages/{page_id}")
async def update_page_media(
    page_id: str,
    payload: PageMediaConfigPayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to create or update active visual media for a page."""
    data = payload.model_dump()
    data["page_id"] = page_id
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["updated_by"] = getattr(admin_user, "email", "admin") or getattr(admin_user, "id", "admin")

    await db.page_media.update_one(
        {"page_id": page_id},
        {"$set": data},
        upsert=True
    )

    saved = await db.page_media.find_one({"page_id": page_id})
    if saved:
        saved["_id"] = str(saved.get("_id", page_id))
        return saved
    return data

@router.post("/pages")
async def save_page_media(
    payload: PageMediaConfigPayload,
    admin_user: User = Depends(require_organizer),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Admin endpoint to save page media configuration."""
    return await update_page_media(payload.page_id, payload, admin_user, db)

@router.get("/{media_id}")
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
