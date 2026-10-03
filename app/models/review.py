from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

class Review(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    user_id: str
    user_name: str
    user_email: str
    avatar_url: Optional[str] = None
    rating: int = Field(default=5, ge=1, le=5)
    review_text: str
    category: str = "PLATFORM"  # TOURNAMENTS, SUPPORT, PLATFORM, MATCHMAKING
    status: str = "PENDING"  # PENDING, APPROVED, FEATURED, HIDDEN
    is_verified: bool = True
    is_featured: bool = False
    admin_notes: Optional[str] = None
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
