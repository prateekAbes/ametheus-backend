from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

class User(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    clerk_user_id: Optional[str] = None
    email: str
    username: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    role: str = "PLAYER"
    riot_id: Optional[str] = None
    steam_id: Optional[str] = None
    inr_balance_paise: int = 0
    inr_reserved_paise: int = 0
    inr_balance: str = "0"  # keep for backwards compatibility
    is_dummy: bool = False
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)

    def to_mongo(self) -> dict:
        d = self.model_dump()
        d["_id"] = d.pop("id")
        if d.get("clerk_user_id") is None:
            d.pop("clerk_user_id", None)
        return d

    @classmethod
    def from_mongo(cls, doc: Optional[dict]):
        if not doc:
            return None
        d = dict(doc)
        if "_id" in d and "id" not in d:
            d["id"] = str(d.pop("_id"))
        # Sync inr_balance string from inr_balance_paise if present
        if "inr_balance_paise" in d and ("inr_balance" not in d or d["inr_balance"] == "0"):
            d["inr_balance"] = str(d["inr_balance_paise"] / 100)
        elif "inr_balance" in d and "inr_balance_paise" not in d:
            try:
                d["inr_balance_paise"] = int(float(d.get("inr_balance", "0")) * 100)
            except Exception:
                d["inr_balance_paise"] = 0
        return cls(**d)
