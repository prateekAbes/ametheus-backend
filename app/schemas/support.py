from typing import List, Optional
from pydantic import BaseModel, Field, EmailStr, ConfigDict


class SupportTicketCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    email: str = Field(..., min_length=5, max_length=150)
    category: str = Field(..., min_length=2, max_length=50)
    payment_type: Optional[str] = None
    description: str = Field(..., min_length=10, max_length=5000)


class SupportTicketUpdateRequest(BaseModel):
    status: Optional[str] = None  # OPEN, IN_PROGRESS, WAITING_FOR_USER, RESOLVED, CLOSED
    internal_notes: Optional[str] = None


class SupportLogResponse(BaseModel):
    id: str
    ticket_id: str
    action: str
    timestamp: str
    actor: str
    actor_role: str
    details: str
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
    logs: List[SupportLogResponse] = Field(default_factory=list)
    created_at: str
    updated_at: str
    resolved_at: Optional[str] = None
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class SupportTicketPublicResponse(BaseModel):
    id: str
    ticket_id: str
    name: str
    category: str
    status: str
    created_at: str
    message: str = "Support ticket created successfully"
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class SupportTicketListResponse(BaseModel):
    tickets: List[SupportTicketResponse]
    total: int
