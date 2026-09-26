from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

class Match(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    tournament_id: str
    stage: str
    round_number: int = 1
    match_number: int = 1
    format: str = "Best of 3"
    scheduled_at: Optional[datetime] = None  # Authoritative ISO UTC timestamp
    scheduled_time: str = "TBD"
    live_link: Optional[str] = None  # Live stream URL (YouTube, Twitch, Kick, etc.)
    order: int = 0
    status: str = "SCHEDULED"  # DRAFT, SCHEDULED, CHECK_IN_OPEN, LIVE, PENDING_VERIFICATION, DISPUTED, COMPLETED, CANCELLED

    team_a_id: Optional[str] = None
    team_b_id: Optional[str] = None
    score_a: Optional[int] = None
    score_b: Optional[int] = None
    winner_team_id: Optional[str] = None
    check_in_status: dict[str, bool] = Field(default_factory=lambda: {"team_a": False, "team_b": False})

    server_region: str = "AWS Mumbai (ap-south-1)"
    lobby_code: str
    lobby_password: str
    evidence_url: Optional[str] = None
    captain_notes: Optional[str] = None
    dispute_notes: Optional[str] = None
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

