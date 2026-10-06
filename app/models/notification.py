from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

class Notification(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    user_id: str
    type: str = "SYSTEM"  # Legacy/granular type
    category: str = "SYSTEM"  # Canonical category: SYSTEM, TEAM, WALLET, REGISTRATION, BATCHES
    title: str
    message: str
    is_read: bool = False
    link: Optional[str] = None
    metadata_json: str = "{}"
    sender_role: Optional[str] = None  # SUPER_ADMIN, ADMIN, ORGANIZER, USER, SYSTEM
    sender_name: Optional[str] = None
    label: Optional[str] = None  # e.g. "Admin Response", "System Notification"
    is_archived: bool = False
    archived_at: Optional[datetime] = None
    archived_by: Optional[str] = None
    archive_reason: Optional[str] = None
    is_cleared: bool = False
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
        if not d.get("category"):
            from app.services.notification_category import resolve_canonical_category
            d["category"] = resolve_canonical_category(d.get("type"), d.get("title"))
        if not d.get("label"):
            from app.services.notification_category import resolve_notification_label
            d["label"] = resolve_notification_label(
                sender_role=d.get("sender_role"),
                notification_type=d.get("type"),
                category=d.get("category"),
                title=d.get("title"),
                message=d.get("message"),
            )
        return cls(**d)

