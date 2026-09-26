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
    status: str = "REGISTRATION_OPEN"  # COMING_SOON, REGISTRATION_OPEN, REGISTRATION_CLOSED, COMPLETED, CANCELLED
    expected_start_date: Optional[str] = None
    registration_open_date: Optional[str] = None
    announcement_text: Optional[str] = None
    starts_at: str
    roster_lock_time: str
    server_region: str = "AWS Mumbai (ap-south-1)"
    max_teams: int = 16
    min_team_size: int = 1
    max_team_size: int = 5
    rank_requirement: str = "Open to All Ranks"
    format: str = "Single Elimination"
    tournament_type: str = "Single Elimination"
    match_format: str = "Single Elimination"  # SINGLE_ELIMINATION, DOUBLE_ELIMINATION, CONSOLATION, SINGLE_ROUND_ROBIN, DOUBLE_ROUND_ROBIN, SWISS, LADDER, GROUP_STAGE_KNOCKOUT
    series_format: str = "BO1"  # BO1, BO3, BO5
    stage_formats: dict[str, str] = Field(default_factory=dict)
    registration_deadline: Optional[datetime] = None
    check_in_window_minutes: int = 15
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

        # Rule 5 & 11: Tournament NEVER has LIVE or ONGOING status. Live belongs strictly to individual matches.
        raw_status = str(d.get("status", "REGISTRATION_OPEN")).upper()
        if raw_status in ["LIVE", "ONGOING", "IN_PROGRESS"]:
            d["status"] = "REGISTRATION_CLOSED"
        elif raw_status in ["UPCOMING", "DRAFT"]:
            d["status"] = "COMING_SOON"

        # Migrate legacy match_format / series_format
        raw_fmt = d.get("match_format") or d.get("format") or "Single Elimination"
        if raw_fmt in ["Best of 1", "BO1"]:
            d["series_format"] = "BO1"
            d["match_format"] = d.get("tournament_type") or "Single Elimination"
        elif raw_fmt in ["Best of 3", "BO3"]:
            d["series_format"] = "BO3"
            d["match_format"] = d.get("tournament_type") or "Single Elimination"
        elif raw_fmt in ["Best of 5", "BO5"]:
            d["series_format"] = "BO5"
            d["match_format"] = d.get("tournament_type") or "Single Elimination"

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
    payment_method: str = "WALLET"  # WALLET, UPI, FREE
    payment_status: str = "FREE"  # FREE, PENDING, PENDING_VERIFICATION, PAID, REJECTED, REFUNDED
    payment_id: Optional[str] = None
    utr: Optional[str] = None
    order_id: Optional[str] = None
    wallet_txn_id: Optional[str] = None
    check_in_status: str = "PENDING"  # PENDING, CHECKED_IN
    registered_at: datetime = Field(default_factory=get_utc_now)
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

