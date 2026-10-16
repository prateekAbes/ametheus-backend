from typing import Optional, Dict, Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field

def get_utc_now():
    return datetime.now(timezone.utc)

class Placement(BaseModel):
    id: str
    placement_key: str
    page: str           # "homepage", "games", "tournaments", "global"
    section: str        # "hero", "featured", "catalog", "upcoming", "showcase", "listing", "detail"
    slot_name: str      # e.g. "Hero Panel 1 (Top-Left)"
    asset_url: Optional[str] = None
    target_id: Optional[str] = None   # optional linked game/tournament slug or id
    focal_x: float = 50.0  # 0 to 100
    focal_y: float = 50.0  # 0 to 100
    zoom: float = 1.0     # 1.0 to 3.0
    fit_mode: str = "cover"  # "cover", "contain", "ambient"
    target_aspect_ratio: str = "16:9"
    target_ratio_value: float = 1.777
    recommended_resolution: str = "1920x1080"
    min_width: int = 1280
    min_height: int = 720
    description: str = ""
    is_active: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)
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
