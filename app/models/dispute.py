from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

class Dispute(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    tournament_id: str
    match_id: str
    submitted_by: str
    reason: str
    description: str
    evidence_url: Optional[str] = None
    status: str = "OPEN"  # OPEN, UNDER_REVIEW, RESOLVED, ESCALATED, CLOSED
    resolution_notes: Optional[str] = None
    resolved_by: Optional[str] = None
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

