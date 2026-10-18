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
    stage: str = "Round 1"
    match_type: str = "MATCH"  # MATCH, QUALIFIER, ROUND_1, ROUND_2, etc.
    round_number: int = 1
    match_number: int = 1
    format: str = "BO1"
    series_format: str = "BO1"  # Strictly "BO1" or "BO3" (BO5 removed)
    scheduled_at: Optional[datetime] = None  # Authoritative ISO UTC timestamp
    scheduled_time: str = "TBD"
    scheduled_date: Optional[str] = None
    roster_status: str = "PUBLISHED"  # DRAFT, PUBLISHED, UPDATED, LIVE, COMPLETED, CANCELLED
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
    next_match_id: Optional[str] = None  # ID of downstream match to advance winner to
    next_match_slot: Optional[str] = None  # "A" or "B"
    slot_a_source_match_id: Optional[str] = None  # Source match whose winner occupies slot A
    slot_b_source_match_id: Optional[str] = None  # Source match whose winner occupies slot B
    advancement_label: Optional[str] = None  # Human-readable destination label (e.g. "Round 2 Match 1 Slot A")
    previous_match_ids: list[str] = Field(default_factory=list)
    notified_captains: list[str] = Field(default_factory=list)
    check_in_status: dict[str, bool] = Field(default_factory=lambda: {"team_a": False, "team_b": False})
    teams: list[dict] = Field(default_factory=list)  # For Battle Royale / multi-team matches
    team_ids: list[str] = Field(default_factory=list)  # IDs of participating teams in this match lobby
    participants: list[dict] = Field(default_factory=list)  # For solo competitors or individual rosters
    is_solo: bool = False
    room_capacity: Optional[int] = None
    players_per_team: Optional[int] = None
    max_teams: Optional[int] = None

    server_region: str = "AWS Mumbai (ap-south-1)"
    lobby_code: Optional[str] = None
    lobby_password: Optional[str] = None
    room_id: Optional[str] = None
    room_password: Optional[str] = None
    credentials_released: bool = False
    credentials_released_at: Optional[datetime] = None
    credentials_released_by: Optional[str] = None
    custom_instructions: Optional[str] = None
    check_ins: list[dict] = Field(default_factory=list)
    alive_status: dict[str, bool] = Field(default_factory=dict)
    placements: list[dict] = Field(default_factory=list)
    qualifiers: list[dict] = Field(default_factory=list)
    qualifier_team_ids: list[str] = Field(default_factory=list)
    qualifiers_count: Optional[int] = None
    is_final_round: bool = False
    scoring_matrix: Optional[dict] = None
    participant_ratings: list[dict] = Field(default_factory=list)
    join_info_enabled: bool = False
    join_info: Optional[dict[str, str]] = None  # Game-specific: party_id, room_id, password, join_link, etc.
    match_message: Optional[str] = None  # Custom match instructions from admin
    sent_reminders: list[str] = Field(default_factory=list)  # Deduplication tracker for reminders
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

        if "teams" not in d or d["teams"] is None:
            d["teams"] = []
        if "team_ids" not in d or d["team_ids"] is None:
            d["team_ids"] = []
        if "check_ins" not in d or d["check_ins"] is None:
            d["check_ins"] = []
        if "alive_status" not in d or d["alive_status"] is None:
            d["alive_status"] = {}
        if "placements" not in d or d["placements"] is None:
            d["placements"] = []
        if "participant_ratings" not in d or d["participant_ratings"] is None:
            d["participant_ratings"] = []
        if "credentials_released" not in d:
            d["credentials_released"] = False
        if "roster_status" not in d or not d["roster_status"]:
            d["roster_status"] = "PUBLISHED"
        if "notified_captains" not in d or d["notified_captains"] is None:
            d["notified_captains"] = []
        if "previous_match_ids" not in d or d["previous_match_ids"] is None:
            d["previous_match_ids"] = []

        return cls(**d)

