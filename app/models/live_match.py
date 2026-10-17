from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

class LiveMatch(BaseModel):
    """
    Canonical Persistent Live Match per Tournament.
    Enforces the single Live Match per tournament architecture:
    - Exactly ONE live match document per tournament (enforced by unique index on tournament_id)
    - Dynamically points to the tournament's currently active encounter (match or lobby)
    - Individual historical encounters remain separately stored and preserved in db.matches
    """
    id: str = Field(default_factory=generate_uuid)
    tournament_id: str
    tournament_title: str = "Ametheus Tournament"
    tournament_slug: Optional[str] = None
    game_id: Optional[str] = None
    game_slug: str = "valorant"
    game_name: str = "Valorant"
    status: str = "SCHEDULED"  # SCHEDULED, CHECK_IN_OPEN, LIVE, COMPLETED, CANCELLED
    format: str = "Knockout"
    series_format: str = "BO1"
    is_battle_royale: bool = False
    is_solo: bool = False

    # Current Encounter Reference
    current_encounter_id: Optional[str] = None
    current_encounter_number: int = 1
    current_encounter_title: str = "Match 1"
    current_round_name: str = "Round 1"
    current_round_number: int = 1
    current_stage: str = "Round 1"

    # Current Matchup / Participants (Knockout)
    team_a_id: Optional[str] = None
    team_b_id: Optional[str] = None
    team_a_name: Optional[str] = None
    team_b_name: Optional[str] = None
    team_a: Optional[Dict[str, Any]] = None
    team_b: Optional[Dict[str, Any]] = None
    score_a: Optional[int] = None
    score_b: Optional[int] = None

    # Current Multi-Team Lobby (Battle Royale)
    teams: List[Dict[str, Any]] = Field(default_factory=list)
    team_ids: List[str] = Field(default_factory=list)
    squads_count: int = 0
    placements: List[Dict[str, Any]] = Field(default_factory=list)
    qualifier_team_ids: List[str] = Field(default_factory=list)
    qualifiers_count: Optional[int] = None
    winner_team_id: Optional[str] = None

    # Room Credentials & Stream Broadcast
    server_region: str = "AWS Mumbai (ap-south-1)"
    lobby_code: Optional[str] = None
    lobby_password: Optional[str] = None
    room_id: Optional[str] = None
    room_password: Optional[str] = None
    credentials_released: bool = False
    credentials_released_at: Optional[datetime] = None
    youtube_live_url: Optional[str] = None
    live_link: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    scheduled_date: Optional[str] = None
    scheduled_time: Optional[str] = "TBD"
    custom_instructions: Optional[str] = None
    check_in_status: Dict[str, bool] = Field(default_factory=dict)

    # Encounter Summary History
    encounter_history: List[Dict[str, Any]] = Field(default_factory=list)

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
