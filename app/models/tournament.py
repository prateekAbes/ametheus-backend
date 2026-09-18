import json
from typing import Optional, List, Any
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

class Tournament(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    slug: str
    game_id: str
    organizer_id: Optional[str] = None
    title: str
    prize_pool_inr: int = 0
    entry_fee_inr: int = 0
    status: str = "REGISTRATION_OPEN"  # DRAFT, REGISTRATION_OPEN, REGISTRATION_CLOSED, UPCOMING, LIVE, COMPLETED, CANCELLED
    starts_at: str
    roster_lock_time: str
    server_region: str = "AWS Mumbai (ap-south-1)"
    max_teams: int = 16
    min_team_size: int = 1
    max_team_size: int = 5
    rank_requirement: str = "Open to All Ranks"
    format: str = "Single Elimination Bracket"
    banner_url: str
    badge_color: str = "#E56A10"
    description: str = ""
    rules: List[Any] = Field(default_factory=list)
    schedule: List[Any] = Field(default_factory=list)
    prizes: List[Any] = Field(default_factory=list)
    bracket_rounds: List[Any] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)

    @property
    def rules_json(self) -> str:
        return json.dumps(self.rules)

    @property
    def schedule_json(self) -> str:
        return json.dumps(self.schedule)

    @property
    def prizes_json(self) -> str:
        return json.dumps(self.prizes)

    @property
    def bracket_json(self) -> str:
        return json.dumps(self.bracket_rounds)

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
        # Parse legacy JSON strings if migrated from SQL
        for field in ["rules", "schedule", "prizes", "bracket_rounds"]:
            legacy_field = field.replace("bracket_rounds", "bracket") + "_json"
            if legacy_field in d and field not in d:
                try:
                    d[field] = json.loads(d.pop(legacy_field))
                except Exception:
                    d[field] = []
        return cls(**d)

class TournamentRegistration(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    tournament_id: str
    team_id: Optional[str] = None
    player_id: Optional[str] = None
    payment_status: str = "FREE"  # FREE, PENDING, PAID, REFUNDED
    order_id: Optional[str] = None
    check_in_status: str = "PENDING"  # PENDING, CHECKED_IN
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

