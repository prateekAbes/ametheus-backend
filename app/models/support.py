import secrets
import string
from datetime import datetime, timezone
from typing import List, Optional
from pydantic import BaseModel, Field


def generate_ticket_id() -> str:
    """Generates a secure, non-sequential unique ticket ID formatted as AMT-XXXXXX."""
    # Exclude ambiguous characters if desired, or use full alphanumeric
    safe_chars = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    suffix = "".join(secrets.choice(safe_chars) for _ in range(6))
    return f"AMT-{suffix}"


class SupportAttachment(BaseModel):
    id: str = Field(default_factory=lambda: f"att_{secrets.token_hex(6)}")
    filename: str
    content_type: str = "application/octet-stream"
    size: int = 0
    url: Optional[str] = None
    storage_path: Optional[str] = None

    def to_mongo(self) -> dict:
        return self.model_dump()


class SupportMessage(BaseModel):
    id: str = Field(default_factory=lambda: f"msg_{secrets.token_hex(6)}")
    ticket_id: str
    thread_id: Optional[str] = None
    sender_id: Optional[str] = None
    sender_email: Optional[str] = None
    sender_name: str = "User"
    recipient_email: Optional[str] = None
    subject: Optional[str] = None
    body_text: str = ""
    body_html: Optional[str] = None
    message: str = ""  # Cleaned / display text (compatible with existing frontend)
    message_id: Optional[str] = None  # Actual Message-ID header (e.g. <AMT-...@ametheus.games>)
    in_reply_to: Optional[str] = None  # In-Reply-To header
    references: Optional[List[str]] = None  # References header list
    direction: str = "inbound"  # "inbound" or "outbound"
    message_type: str = "USER"  # "USER", "ADMIN", or "SYSTEM"
    sender_role: str = "USER"  # "USER", "ADMIN", or "SYSTEM"
    attachments: List[SupportAttachment] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_mongo(self) -> dict:
        data = self.model_dump()
        data["_id"] = self.id
        return data


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
    status: str = "WAITING_FOR_ADMIN"  # OPEN, WAITING_FOR_ADMIN, WAITING_FOR_USER, IN_PROGRESS, RESOLVED, CLOSED
    internal_notes: Optional[str] = None
    email_status: str = "PENDING"  # PENDING, SENT, PARTIAL, FAILED
    email_error: Optional[str] = None
    user_email_status: Optional[str] = "PENDING"  # PENDING, SENT, FAILED
    support_email_status: Optional[str] = "PENDING"  # PENDING, SENT, FAILED
    has_new_reply: bool = False
    last_message_at: Optional[str] = None
    last_message_by: Optional[str] = None
    messages: List[SupportMessage] = Field(default_factory=list)
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
