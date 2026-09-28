import re
from typing import Optional, Dict
from pydantic import BaseModel, Field, field_validator

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_email_format(v: str, field_name: str) -> str:
    clean = str(v).strip().lower()
    if not clean or not EMAIL_REGEX.match(clean):
        raise ValueError(f"Invalid email address format for {field_name}: '{v}'")
    return clean


class EmailSettingsResponse(BaseModel):
    official_email: str
    support_email: str
    automated_email: str
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class EmailServiceStatusItem(BaseModel):
    service: str
    email: str
    display_name: str
    smtp_host: str
    smtp_port: int
    use_tls: bool
    configured: bool
    status: str  # "Configured" or "Not Configured"


class EmailSystemStatusResponse(BaseModel):
    email_enabled: bool
    services: Dict[str, EmailServiceStatusItem]


class SmtpStatusResponse(BaseModel):
    configured: bool
    connected: bool
    authenticated: bool
    sender: str
    service: Optional[str] = "support"
    host: Optional[str] = None
    port: Optional[int] = 587
    secure: bool = False
    status_code: str  # CONNECTED, NOT_CONFIGURED, CONNECTION_FAILED, AUTHENTICATION_FAILED, TIMEOUT
    message: str


class SendTestEmailRequest(BaseModel):
    service: str = Field(default="support", description="Service identity to test: 'support', 'noreply', or 'official'")
    to_email: str = Field(..., min_length=5, max_length=150, description="Destination email address for the test message")

    @field_validator("to_email")
    @classmethod
    def check_to_email(cls, v: str) -> str:
        return validate_email_format(v, "Destination Email")

    @field_validator("service")
    @classmethod
    def normalize_service(cls, v: str) -> str:
        norm = (v or "support").strip().lower()
        if norm in ("support", "help", "contact"):
            return "support"
        if norm in ("noreply", "no-reply", "automated", "system"):
            return "noreply"
        if norm in ("official", "business", "general"):
            return "official"
        raise ValueError("Service must be 'support', 'noreply', or 'official'")


class SendTestEmailResponse(BaseModel):
    success: bool
    service: str = "support"
    message_id: Optional[str] = None
    error_code: Optional[str] = None
    error_details: Optional[str] = None
    sender_used: Optional[str] = None
    message: str = "Test email sent successfully"


class OfficialEmailSendRequest(BaseModel):
    recipient_type: str = Field(
        ...,
        description="Target audience: 'ALL_USERS', 'SELECTED_USERS', 'SELECTED_TEAMS', or 'CUSTOM_EMAILS'",
    )
    user_ids: Optional[list[str]] = Field(default=None, description="List of user IDs when recipient_type is 'SELECTED_USERS'")
    team_ids: Optional[list[str]] = Field(default=None, description="List of team IDs when recipient_type is 'SELECTED_TEAMS'")
    custom_emails: Optional[list[str]] = Field(default=None, description="List of direct emails when recipient_type is 'CUSTOM_EMAILS'")
    subject: str = Field(..., min_length=3, max_length=200, description="Email subject line")
    message: str = Field(..., min_length=5, max_length=20000, description="Email message body (markdown/plain text)")

    @field_validator("recipient_type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        clean = (v or "").strip().upper()
        if clean not in ("ALL_USERS", "SELECTED_USERS", "SELECTED_TEAMS", "CUSTOM_EMAILS"):
            raise ValueError("recipient_type must be one of: 'ALL_USERS', 'SELECTED_USERS', 'SELECTED_TEAMS', 'CUSTOM_EMAILS'")
        return clean


class OfficialEmailLogItem(BaseModel):
    id: str
    sender: str
    recipient_type: str
    recipient_count: int
    recipients_sample: list[str] = []
    subject: str
    status: str
    created_at: str
    operator_id: Optional[str] = None
    error_summary: Optional[str] = None


class OfficialEmailSendResponse(BaseModel):
    success: bool
    message: str
    recipient_count: int
    log_id: str


class OfficialEmailHistoryResponse(BaseModel):
    total: int
    items: list[OfficialEmailLogItem]

