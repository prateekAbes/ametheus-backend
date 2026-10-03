from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

class Genre(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    slug: str
    name: str
    description: str = ""
    banner_url: Optional[str] = None
    icon_url: Optional[str] = None
    rotation_interval_seconds: int = 4
    display_order: int = 0
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
