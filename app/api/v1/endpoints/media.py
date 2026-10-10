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

async def _save_media(content: bytes, filename: str, content_type: str, db: AsyncIOMotorDatabase):
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
