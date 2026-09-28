import secrets
import string
from datetime import datetime, timezone
from typing import List, Optional
from pydantic import BaseModel, Field


def generate_ticket_id() -> str:
    """Generates a secure, non-sequential unique ticket ID formatted as AMT-XXXXXX."""
    chars = string.ascii_uppercase + string.digits
    # Exclude ambiguous characters if desired, or use full alphanumeric
    safe_chars = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    suffix = "".join(secrets.choice(safe_chars) for _ in range(6))
    return f"AMT-{suffix}"


class SupportLogEntry(BaseModel):
    id: str = Field(default_factory=lambda: f"log_{secrets.token_hex(6)}")
    ticket_id: str
    action: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    actor: str = "SYSTEM"
    actor_role: str = "SYSTEM"
    details: str = ""

    def to_mongo(self) -> dict:
        return self.model_dump()


class SupportTicket(BaseModel):
    id: str = Field(default_factory=generate_ticket_id)
    ticket_id: str = Field(default="")
    name: str
    email: str
    user_id: Optional[str] = None
    user_handle: Optional[str] = None
    category: str
    payment_type: Optional[str] = None
    description: str
    status: str = "OPEN"  # OPEN, IN_PROGRESS, WAITING_FOR_USER, RESOLVED, CLOSED
    internal_notes: Optional[str] = None
    email_status: str = "PENDING"  # PENDING, SENT, PARTIAL, FAILED
    email_error: Optional[str] = None
    user_email_status: Optional[str] = "PENDING"  # PENDING, SENT, FAILED
    support_email_status: Optional[str] = "PENDING"  # PENDING, SENT, FAILED
    logs: List[SupportLogEntry] = Field(default_factory=list)
    is_archived: bool = False
    archived_at: Optional[str] = None
    archived_by: Optional[str] = None
    archive_reason: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    resolved_at: Optional[str] = None

    def model_post_init(self, __context):
        if not self.ticket_id:
            self.ticket_id = self.id

    def to_mongo(self) -> dict:
        data = self.model_dump()
        data["_id"] = self.id
        return data
