import secrets
import string
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field
from app.models.base import generate_uuid, get_utc_now

def generate_invite_code(length: int = 6) -> str:
    chars = string.ascii_uppercase + string.digits
    chars = chars.replace("0", "").replace("O", "").replace("I", "").replace("1", "")
    return "".join(secrets.choice(chars) for _ in range(length))

class TeamMember(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    team_id: Optional[str] = None
    user_id: str
    role: str = "MEMBER"  # CAPTAIN, MEMBER, SUBSTITUTE
    game_handle: str
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

class Team(BaseModel):
    id: str = Field(default_factory=generate_uuid)
    game_id: str
    captain_id: str
    name: str
    tag: str
    logo_url: Optional[str] = None
    invite_code: str = Field(default_factory=generate_invite_code)
    max_members: int = 5
    max_substitutes: int = 1
    members: List[TeamMember] = Field(default_factory=list)
    substitutes: List[TeamMember] = Field(default_factory=list)
    payment_status: str = "UNPAID"  # UNPAID, PAYMENT_PENDING, PAID, PAYMENT_REJECTED
    payment_id: Optional[str] = None
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
