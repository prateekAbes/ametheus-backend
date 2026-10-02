from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

class TournamentAdminAssignment(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    admin_user_id: str
    tournament_id: str
    assigned_by: str
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)

    def to_mongo(self) -> dict:
        d = self.model_dump()
        d["_id"] = d.pop("id")
        return d

    @classmethod
    def from_mongo(cls, doc: Optional[dict]):
        if not doc:
            return None
        d = dict(doc)
        if "_id" in d and "id" not in d:
            d["id"] = str(d.pop("_id"))
        return cls(**d)

class AuditLog(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    operator: str
    role: str
    action: str
    target: str
    details: Optional[str] = None
    ip_address: str = "127.0.0.1"
    prev_hash: Optional[str] = None
    record_hash: Optional[str] = None
    is_archived: bool = False
    archived_at: Optional[datetime] = None
    archived_by: Optional[str] = None
    archive_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)

    def to_mongo(self) -> dict:
        d = self.model_dump()
        d["_id"] = d.pop("id")
        return d

    @classmethod
    def from_mongo(cls, doc: Optional[dict]):
        if not doc:
            return None
        d = dict(doc)
        if "_id" in d and "id" not in d:
            d["id"] = str(d.pop("_id"))
        return cls(**d)


OFFICIAL_HERO_GIF_URL = "https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExM3Z0ZWFmNnN2cWJxeHoxbjlyM3cydXBnNDB1NnY2d2hkYWFxdG00MSZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/3oKIPnAiaMCws8nOsE/giphy.gif"

class HeroPresentationSettings(BaseModel):
    id: str = "hero_settings"
    animation_speed: float = Field(default=1.0, ge=0.25, le=3.0)
    brightness: float = Field(default=1.0, ge=0.1, le=2.5)
    opacity: float = Field(default=0.40, ge=0.0, le=1.0)
    contrast: float = Field(default=1.0, ge=0.1, le=2.5)
    saturation: float = Field(default=1.0, ge=0.0, le=3.0)
    overlay_opacity: float = Field(default=0.35, ge=0.0, le=1.0)
    asset_url: str = Field(default=OFFICIAL_HERO_GIF_URL)
    is_locked: bool = Field(default=True)
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None

    def to_mongo(self) -> dict:
        d = self.model_dump()
        d["_id"] = d.pop("id")
        return d

    @classmethod
    def from_mongo(cls, doc: Optional[dict]):
        if not doc:
            return cls()
        d = dict(doc)
        if "_id" in d and "id" not in d:
            d["id"] = str(d.pop("_id"))
        # Ensure asset_url is immutable and locked to official approved asset
        d["asset_url"] = OFFICIAL_HERO_GIF_URL
        d["is_locked"] = True
        return cls(**d)

class HeroPresentationSettingsUpdate(BaseModel):
    animation_speed: Optional[float] = Field(None, ge=0.25, le=3.0)
    brightness: Optional[float] = Field(None, ge=0.1, le=2.5)
    opacity: Optional[float] = Field(None, ge=0.0, le=1.0)
    contrast: Optional[float] = Field(None, ge=0.1, le=2.5)
    saturation: Optional[float] = Field(None, ge=0.0, le=3.0)
    overlay_opacity: Optional[float] = Field(None, ge=0.0, le=1.0)

