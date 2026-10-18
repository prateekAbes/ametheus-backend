import json
from typing import Optional, List, Any, Dict
from datetime import datetime, timezone
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

class StageRound(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    round_id: Optional[str] = None
    stage_id: Optional[str] = None
    round_number: int = 1
    round_order: int = 1
    name: str = "Round 1"
    round_name: Optional[str] = None
    status: str = "UPCOMING"  # UPCOMING, IN_PROGRESS, COMPLETED
    format: str = "SINGLE_ELIMINATION"  # SINGLE_ELIMINATION, KNOCKOUT, DOUBLE_ELIMINATION, ROUND_ROBIN, BATTLE_ROYALE, DROP_IN
    series_format: str = "BO1"  # Strictly "BO1" or "BO3"
    match_count: int = 1
    duration_minutes: Optional[int] = None
    advancement_rule: Optional[str] = None
    rules_notes: Optional[str] = None
    scheduled_date: Optional[str] = None
    scheduled_time: Optional[str] = None
    stream_url: Optional[str] = None
    live_stream_url: Optional[str] = None
    match_ids: List[str] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        if not self.round_id:
            self.round_id = self.id
        if not self.round_name:
            self.round_name = self.name
        if not self.name and self.round_name:
            self.name = self.round_name
        if not self.stream_url and self.live_stream_url:
            self.stream_url = self.live_stream_url
        elif not self.live_stream_url and self.stream_url:
            self.live_stream_url = self.stream_url
        if self.round_order == 1 and self.round_number != 1:
            self.round_order = self.round_number
        elif self.round_number == 1 and self.round_order != 1:
            self.round_number = self.round_order

    def to_mongo(self) -> dict:
        d = self.model_dump()
        if not d.get("stream_url") and d.get("live_stream_url"):
            d["stream_url"] = d["live_stream_url"]
        if not d.get("live_stream_url") and d.get("stream_url"):
            d["live_stream_url"] = d["stream_url"]
        return d

class TournamentStage(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    stage_id: Optional[str] = None
    stage_number: int = 1
    stage_order: int = 1
    name: str = "Stage 1"
    stage_name: Optional[str] = None
    format: str = "KNOCKOUT"
    stage_format: Optional[str] = None
    status: str = "UPCOMING"  # UPCOMING, IN_PROGRESS, COMPLETED, CANCELLED
    series_format: str = "BO1"  # Strictly "BO1" or "BO3"
    advancing_teams_count: Optional[int] = None
    qualified_team_ids: List[str] = Field(default_factory=list)
    eliminated_team_ids: List[str] = Field(default_factory=list)
    rounds: List[StageRound] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)

    def model_post_init(self, __context: Any) -> None:
        if not self.stage_id:
            self.stage_id = self.id
        if not self.stage_name:
            self.stage_name = self.name
        if not self.name and self.stage_name:
            self.name = self.stage_name
        if not self.stage_format:
            self.stage_format = self.format
        if not self.format and self.stage_format:
            self.format = self.stage_format
        if self.stage_order == 1 and self.stage_number != 1:
            self.stage_order = self.stage_number
        elif self.stage_number == 1 and self.stage_order != 1:
            self.stage_number = self.stage_order

    def to_mongo(self) -> dict:
        d = self.model_dump()
        d["rounds"] = [r if isinstance(r, dict) else r.to_mongo() for r in (self.rounds or [])]
        return d

class Tournament(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    slug: str
    game_id: str
    organizer_id: Optional[str] = None
    title: str
    name: Optional[str] = None
    prize_pool_inr: int = 0
    entry_fee_inr: int = 0
    status: str = "REGISTRATION_OPEN"  # COMING_SOON, REGISTRATION_OPEN, REGISTRATION_CLOSED, LIVE, COMPLETED, CANCELLED
    expected_start_date: Optional[str] = None
    registration_open_date: Optional[str] = None
    announcement_text: Optional[str] = None
    starts_at: str
    roster_lock_time: Optional[str] = None
    participation_mode: str = "SQUAD"  # "SOLO", "DUO", "SQUAD"
    server_region: str = "AWS Mumbai (ap-south-1)"
    max_teams: int = 16
    min_team_size: int = 1
    max_team_size: int = 5
    room_capacity: Optional[int] = None  # Battle Royale room capacity (e.g. 100 players)
    players_per_team: Optional[int] = None  # Active players per team/squad (e.g. 4)
    rank_requirement: str = "Open to All Ranks"
    format: str = "Knockout"
    tournament_type: str = "Knockout"
    match_format: str = "Knockout"
    series_format: str = "BO1"  # Strictly "BO1" or "BO3"
    registration_type: str = "TEAM"  # "SOLO" or "TEAM"
    is_solo: bool = False
    match_structure: str = "KNOCKOUT"  # "KNOCKOUT", "BATTLE_ROYALE", "ROUND_ROBIN"
    game_format: Optional[str] = None
    live_stream_url: Optional[str] = None  # Permanent 1-stream per tournament
    stages: List[TournamentStage] = Field(default_factory=list)
    current_stage_id: Optional[str] = None
    stage_formats: dict[str, str] = Field(default_factory=dict)
    registration_deadline: Optional[datetime] = None
    check_in_window_minutes: int = 15
    banner_url: str = ""
    hero_banner_url: Optional[str] = None
    banner_tablet_url: Optional[str] = None
    banner_mobile_url: Optional[str] = None
    logo_url: Optional[str] = None
    badge_color: str = "#E56A10"
    description: str = ""
    media_config: Dict[str, Any] = Field(default_factory=dict)
    rules: List[Any] = Field(default_factory=list)
    schedule: List[Any] = Field(default_factory=list)
    prizes: List[Any] = Field(default_factory=list)
    bracket_rounds: List[Any] = Field(default_factory=list)
    custom_rounds: List[str] = Field(default_factory=list)
    round_streams: Dict[str, str] = Field(default_factory=dict)
    community_platform: Optional[str] = None  # "DISCORD" | "WHATSAPP" | None
    community_link: Optional[str] = None      # invite link URL
    whatsapp_group_url: Optional[str] = None  # dedicated WhatsApp group invite URL
    discord_url: Optional[str] = None         # dedicated Discord server invite URL
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
        tid = d.get("id") or generate_uuid()
        d["_id"] = tid
        d["id"] = tid
        return d

    @classmethod
    def from_mongo(cls, doc: Optional[dict]):
        if not doc:
            return None
        d = dict(doc)
        if "_id" in d and "id" not in d:
            d["id"] = str(d.pop("_id"))

        if not d.get("name") and d.get("title"):
            d["name"] = d["title"]
        if not d.get("title") and d.get("name"):
            d["title"] = d["name"]

        # Authentic Tournament Lifecycle: COMING_SOON, REGISTRATION_OPEN, REGISTRATION_CLOSED, LIVE, COMPLETED, CANCELLED
        raw_status = str(d.get("status", "REGISTRATION_OPEN")).upper()
        if raw_status in ["LIVE", "ONGOING", "IN_PROGRESS"]:
            d["status"] = "LIVE"
        elif raw_status in ["UPCOMING", "DRAFT"]:
            d["status"] = "COMING_SOON"
        elif raw_status in ["CLOSED"]:
            d["status"] = "REGISTRATION_CLOSED"
        elif raw_status in ["OPEN"]:
            d["status"] = "REGISTRATION_OPEN"
        else:
            d["status"] = raw_status

        # Competition format / match structure: Strictly Knockout, Round Robin, Battle Royale
        raw_comp_fmt = str(d.get("format") or d.get("tournament_type") or d.get("match_structure") or d.get("match_format") or "Knockout").strip()
        comp_fmt_lower = raw_comp_fmt.lower()
        if "battle" in comp_fmt_lower or "royale" in comp_fmt_lower:
            norm_comp_fmt = "Battle Royale"
            d["match_structure"] = "BATTLE_ROYALE"
        elif "robin" in comp_fmt_lower:
            norm_comp_fmt = "Round Robin"
            d["match_structure"] = "ROUND_ROBIN"
        else:
            norm_comp_fmt = "Knockout"
            d["match_structure"] = "KNOCKOUT"
        d["format"] = norm_comp_fmt
        d["tournament_type"] = norm_comp_fmt
        if not d.get("match_format"):
            d["match_format"] = norm_comp_fmt

        # Participation Mode: Strictly SOLO, DUO, or SQUAD
        raw_mode = d.get("participation_mode")
        if not raw_mode:
            min_s = d.get("min_team_size", 1)
            max_s = d.get("max_team_size", 5)
            raw_fmt_str = (str(d.get("format", "")) + " " + str(d.get("match_format", "")) + " " + str(d.get("title", ""))).lower()
            if d.get("is_solo") or d.get("registration_type") == "SOLO" or (min_s == 1 and max_s == 1) or "solo" in raw_fmt_str or "1v1" in raw_fmt_str:
                d["participation_mode"] = "SOLO"
            elif d.get("players_per_team") == 2 or (min_s == 2 and max_s == 2) or "duo" in raw_fmt_str:
                d["participation_mode"] = "DUO"
            else:
                d["participation_mode"] = "SQUAD"
        else:
            norm_mode = str(raw_mode).upper().strip()
            d["participation_mode"] = norm_mode if norm_mode in {"SOLO", "DUO", "SQUAD"} else "SQUAD"

        d["is_solo"] = (d["participation_mode"] == "SOLO")
        d["registration_type"] = "SOLO" if d["is_solo"] else "TEAM"


        # Series format: Strictly BO1 or BO3 (BO5, BO2, etc. mapped to BO3)
        raw_s_fmt = str(d.get("series_format") or "").upper().strip()
        raw_m_fmt = str(d.get("match_format") or "").upper().strip()
        if "3" in raw_s_fmt or "3" in raw_m_fmt or "BO3" in raw_s_fmt:
            d["series_format"] = "BO3"
        elif "5" in raw_s_fmt or "5" in raw_m_fmt or "BO5" in raw_s_fmt:
            d["series_format"] = "BO3"
        else:
            d["series_format"] = "BO1"

        # If live_stream_url not set, check youtube_live_url or stream_url
        if not d.get("live_stream_url"):
            d["live_stream_url"] = d.get("youtube_live_url") or d.get("stream_url") or None

        # Synchronize discord_url, whatsapp_group_url, and community_link
        if not d.get("discord_url") and d.get("community_platform") == "DISCORD":
            d["discord_url"] = d.get("community_link")
        elif d.get("discord_url") and not d.get("community_link"):
            d["community_link"] = d.get("discord_url")
            if not d.get("community_platform"):
                d["community_platform"] = "DISCORD"

        if not d.get("whatsapp_group_url") and d.get("community_platform") == "WHATSAPP":
            d["whatsapp_group_url"] = d.get("community_link")
        elif d.get("whatsapp_group_url") and not d.get("community_link"):
            d["community_link"] = d.get("whatsapp_group_url")
            if not d.get("community_platform"):
                d["community_platform"] = "WHATSAPP"

        # Fallback required fields if missing in legacy or mock documents
        if not d.get("game_id"):
            d["game_id"] = "general"
        if not d.get("starts_at"):
            d["starts_at"] = datetime.now(timezone.utc).isoformat()

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
    game_id: Optional[str] = None
    in_game_id: Optional[str] = None
    screenshot_url: Optional[str] = None
    rejection_reason: Optional[str] = None
    position: Optional[int] = None
    check_in_status: str = "PENDING"  # PENDING, CHECKED_IN
    registered_at: datetime = Field(default_factory=get_utc_now)
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)

    def to_mongo(self) -> dict:
        d = self.model_dump()
        tid = d.get("id") or generate_uuid()
        d["_id"] = tid
        d["id"] = tid
        return d

    @classmethod
    def from_mongo(cls, doc: Optional[dict]):
        if not doc:
            return None
        d = dict(doc)
        if "_id" in d and "id" not in d:
            d["id"] = str(d.pop("_id"))
        return cls(**d)

