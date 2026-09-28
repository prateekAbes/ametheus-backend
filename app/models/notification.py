from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

class Notification(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    user_id: str
    type: str = "SYSTEM"  # SYSTEM, TOURNAMENT, MATCH, PAYMENT, DISPUTE
    title: str
    message: str
    is_read: bool = False
    link: Optional[str] = None
    metadata_json: str = "{}"
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

