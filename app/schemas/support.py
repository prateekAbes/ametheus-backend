from typing import List, Optional
from pydantic import BaseModel, Field, EmailStr, ConfigDict


class SupportTicketCreateRequest(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    category: Optional[str] = "OTHER"
    payment_type: Optional[str] = None
    description: Optional[str] = None
    subject: Optional[str] = None
    message: Optional[str] = None
    priority: Optional[str] = None


class SupportTicketUpdateRequest(BaseModel):
    status: Optional[str] = None  # NEW, OPEN, WAITING_FOR_USER, WAITING_FOR_ADMIN, UNREAD, IN_PROGRESS, ON_HOLD, RESOLVED, CLOSED, ARCHIVED
    internal_notes: Optional[str] = None


class SupportTicketReplyRequest(BaseModel):
    message: str
    status: Optional[str] = None


class SupportEmailStatus(BaseModel):
    status: str  # "sent" or "failed" or "pending"
    error: Optional[str] = None


class SupportSettingsResponse(BaseModel):
    support_email: str


class SupportSettingsUpdateRequest(BaseModel):
    support_email: str = Field(..., min_length=5, max_length=150)


class SupportEmailRetryRequest(BaseModel):
    target: str = Field("ALL", description="'USER', 'SUPPORT', or 'ALL'")


class SupportLogResponse(BaseModel):
    id: str
    ticket_id: str
    action: str
    timestamp: str
    actor: str
    actor_role: str
    details: str
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class SupportAttachmentResponse(BaseModel):
    id: str
    filename: str
    content_type: str = "application/octet-stream"
    size: int = 0
    url: Optional[str] = None
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class SupportMessageResponse(BaseModel):
    id: str
    ticket_id: str
    thread_id: Optional[str] = None
    sender_id: Optional[str] = None
    sender_email: Optional[str] = None
    sender_name: str = "User"
    recipient_email: Optional[str] = None
    subject: Optional[str] = None
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    message: str = ""
    message_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    references: Optional[List[str]] = None
    direction: str = "inbound"
    message_type: str = "USER"
    sender_role: str = "USER"
    attachments: List[SupportAttachmentResponse] = Field(default_factory=list)
    created_at: str
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class SupportTicketResponse(BaseModel):
    id: str
    ticket_id: str
    name: str
    email: str
    user_id: Optional[str] = None
    user_handle: Optional[str] = None
    category: str
    payment_type: Optional[str] = None
    description: str
    status: str
    internal_notes: Optional[str] = None
    email_status: str = "PENDING"
    email_error: Optional[str] = None
    user_email_status: Optional[str] = None
    support_email_status: Optional[str] = None
    has_new_reply: bool = False
    last_message_at: Optional[str] = None
    last_message_by: Optional[str] = None
    messages: List[SupportMessageResponse] = Field(default_factory=list)
    logs: List[SupportLogResponse] = Field(default_factory=list)
    is_archived: bool = False
    archived_at: Optional[str] = None
    archived_by: Optional[str] = None
    archive_reason: Optional[str] = None
    created_at: str
    updated_at: str
    resolved_at: Optional[str] = None
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class SupportTicketPublicResponse(BaseModel):
    id: str
    ticket_id: str
    name: str
    category: str
    payment_type: Optional[str] = None
    status: str
    created_at: str
    email_status: str = "PENDING"
    email_error: Optional[str] = None
    user_email: Optional[SupportEmailStatus] = None
    support_email: Optional[SupportEmailStatus] = None
    ticket_created: bool = True
    message: str = "Support ticket created successfully"
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class SupportTicketListResponse(BaseModel):
    tickets: List[SupportTicketResponse]
    total: int

