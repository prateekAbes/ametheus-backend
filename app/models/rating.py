from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

class PlayerRating(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    user_id: str
    game_id: str
    game_slug: Optional[str] = None
    rating: int = 500  # Initial authoritative starting rating: 500
    highest_rating: int = 500
    is_dummy: bool = False
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

class RatingAuditLog(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    player_id: str
    game_id: str
    game_slug: Optional[str] = None
    game_name: Optional[str] = None
    previous_rating: int
    adjustment: int  # Exact integer change (+/-)
    new_rating: int
    reason: str  # Placement, MVP, Performance, Penalty, Administrative Correction, Other
    explanation: str  # Player-visible narrative
    admin_id: str
    admin_username: str
    tournament_id: Optional[str] = None
    tournament_name: Optional[str] = None
    round_id: Optional[str] = None
    round_name: Optional[str] = None
    match_id: Optional[str] = None
    is_reversal: bool = False
    original_log_id: Optional[str] = None
    created_at: datetime = Field(default_factory=get_utc_now)

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
