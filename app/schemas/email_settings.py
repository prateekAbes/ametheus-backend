import re
from typing import Optional
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


class EmailSettingsUpdateRequest(BaseModel):
    official_email: str = Field(..., min_length=5, max_length=150, description="General official or business communication email")
    support_email: str = Field(..., min_length=5, max_length=150, description="Public support destination email")
    automated_email: str = Field(..., min_length=5, max_length=150, description="From sender identity for automated notifications")

    @field_validator("official_email")
    @classmethod
    def check_official_email(cls, v: str) -> str:
        return validate_email_format(v, "Official Email")

    @field_validator("support_email")
    @classmethod
    def check_support_email(cls, v: str) -> str:
        return validate_email_format(v, "Support Email")

    @field_validator("automated_email")
    @classmethod
    def check_automated_email(cls, v: str) -> str:
        return validate_email_format(v, "Automated Email")


class SmtpStatusResponse(BaseModel):
    configured: bool
    connected: bool
    authenticated: bool
    sender: str
    host: Optional[str] = None
    port: Optional[int] = 587
    secure: bool = False
    status_code: str  # CONNECTED, NOT_CONFIGURED, CONNECTION_FAILED, AUTHENTICATION_FAILED, TIMEOUT
    message: str


class SendTestEmailRequest(BaseModel):
    to_email: str = Field(..., min_length=5, max_length=150, description="Destination email address for the test message")

    @field_validator("to_email")
    @classmethod
    def check_to_email(cls, v: str) -> str:
        return validate_email_format(v, "Destination Email")


class SendTestEmailResponse(BaseModel):
    success: bool
    message_id: Optional[str] = None
    error_code: Optional[str] = None
    error_details: Optional[str] = None
    sender_used: Optional[str] = None
