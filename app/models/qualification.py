from typing import Optional, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

class TournamentQualification(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    tournament_id: str
    game_id: Optional[str] = None
    source_round: str
    source_round_number: int = 1
    source_match_id: str
    participant_id: str
    participant_name: str
    participant_type: str = "TEAM"  # "TEAM" or "SOLO"
    status: str = "QUALIFIED"  # "QUALIFIED", "ASSIGNED", "ELIMINATED"
    destination_round: Optional[str] = None
    destination_match_id: Optional[str] = None
    placement: Optional[int] = None
    points: Optional[int] = None
    result_reference: Dict[str, Any] = Field(default_factory=dict)
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
