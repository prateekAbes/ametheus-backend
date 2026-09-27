from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

class Match(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    tournament_id: str
    stage_id: Optional[str] = None
    round_id: Optional[str] = None
    stage_name: Optional[str] = None
    round_name: Optional[str] = None
    stage: str = "Quarterfinals"
    match_type: str = "QUARTER_FINAL"  # QUALIFIER, PRELIMINARY, ROUND_OF_32, ROUND_OF_16, QUARTER_FINAL, SEMI_FINAL, THIRD_PLACE, FINAL, GRAND_FINAL, GROUP_STAGE, SWISS_ROUND, WINNERS_BRACKET, LOSERS_BRACKET
    round_number: int = 1
    match_number: int = 1
    format: str = "BO1"
    series_format: str = "BO1"  # Strictly "BO1" or "BO3" (BO5 removed)
    scheduled_at: Optional[datetime] = None  # Authoritative ISO UTC timestamp
    scheduled_time: str = "TBD"
    live_link: Optional[str] = None  # Live stream URL (YouTube, Twitch, Kick, etc.)
    youtube_live_url: Optional[str] = None  # Dedicated YouTube Live URL
    order: int = 0
    status: str = "SCHEDULED"  # SCHEDULED, CHECK_IN_OPEN, LIVE, PENDING_VERIFICATION, DISPUTED, COMPLETED, CANCELLED

    team_a_id: Optional[str] = None
    team_b_id: Optional[str] = None
    team_a_name: Optional[str] = None
    team_b_name: Optional[str] = None
    team_a_placeholder: Optional[str] = None  # e.g., "Winner of Round 1 Match 1"
    team_b_placeholder: Optional[str] = None  # e.g., "Winner of Round 1 Match 2"
    score_a: Optional[int] = None
    score_b: Optional[int] = None
    winner_team_id: Optional[str] = None
    games: Optional[list[dict]] = None  # Individual game results for BO3 series
    advancement: Optional[dict] = None  # Tracks suggested and confirmed advancement destinations
    check_in_status: dict[str, bool] = Field(default_factory=lambda: {"team_a": False, "team_b": False})

    server_region: str = "AWS Mumbai (ap-south-1)"
    lobby_code: str = "TBD"
    lobby_password: str = "TBD"
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

        # Synchronize live_link and youtube_live_url
        if "youtube_live_url" not in d or not d["youtube_live_url"]:
            d["youtube_live_url"] = d.get("live_link")
        if "live_link" not in d or not d["live_link"]:
            d["live_link"] = d.get("youtube_live_url")

        # Synchronize format and series_format (BO5 mapped to BO3)
        raw_fmt = d.get("format") or d.get("series_format") or "BO1"
        if raw_fmt in ["Best of 1", "BO1"]:
            d["series_format"] = "BO1"
            d["format"] = "BO1"
        elif raw_fmt in ["Best of 3", "BO3"]:
            d["series_format"] = "BO3"
            d["format"] = "BO3"
        elif raw_fmt in ["Best of 5", "BO5"]:
            d["series_format"] = "BO3"
            d["format"] = "BO3"

        # Synchronize stage and match_type
        if "match_type" not in d or not d["match_type"]:
            stg = (d.get("stage") or "QUARTER_FINAL").upper().replace(" ", "_").replace("—", "").replace("-", "_")
            d["match_type"] = stg

        # Fill default stage_name and round_name if missing
        if not d.get("stage_name"):
            d["stage_name"] = d.get("stage") or f"Stage {d.get('stage_id', 1)}"
        if not d.get("round_name"):
            d["round_name"] = f"Round {d.get('round_number', 1)}"

        return cls(**d)

