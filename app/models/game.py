from typing import Optional, List, Dict, Any
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

class Game(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    slug: str
    name: str
    category: str = "pc"
    genre_id: Optional[str] = None
    genre_name: Optional[str] = None
    platform: str
    tag: str
    is_team_game: bool = True
    min_team_size: int = 1
    max_team_size: int = 5
    max_substitutes: int = 1
    entry_fee_inr: int = 0
    prize_pool_inr: int = 0
    play_url: Optional[str] = None
    poster_url: Optional[str] = None
    banner_url: str
    banner_tablet_url: Optional[str] = None
    banner_mobile_url: Optional[str] = None
    logo_url: str
    description: str = ""
    server_region: str = "AWS Mumbai (ap-south-1)"
    anti_cheat: str = "Hardware TPM 2.0 Vanguard"
    match_protocol: str = "Competitive Protocol"
    accent_color: str = "#E56A10"
    status: str = "ACTIVE"  # ACTIVE, COMING_SOON, ARCHIVED
    expected_release_date: Optional[str] = None
    announcement_text: Optional[str] = None
    hero_media_url: Optional[str] = None
    hero_media_type: str = "gif"
    hero_media_enabled: bool = True
    gallery: List[Dict[str, Any]] = Field(default_factory=list)
    media_config: Dict[str, Any] = Field(default_factory=dict)
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
