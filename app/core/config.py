import os
from typing import List, Union, Optional
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    PROJECT_NAME: str = "AMETHEUS Esports Engine"
    API_V1_STR: str = "/api/v1"
    MONGODB_URI: str = "mongodb://localhost:27017"
    MONGODB_DB_NAME: str = "ametheus"
    DATABASE_URL: str = "sqlite+aiosqlite:///./ametheus.db"
    SECRET_KEY: str = "ametheus_solar_vanguard_secure_key_2026"
    CORS_ORIGINS: Union[List[str], str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "https://ametheus.games",
        "https://www.ametheus.games",
        "https://ametheus-frontend.vercel.app",
    ]
    CLERK_ISSUER: str = "https://clerk.ametheus.gg"
    CLERK_PUBLISHABLE_KEY: Optional[str] = None
    CLERK_SECRET_KEY: Optional[str] = None
    CLERK_JWT_KEY: Optional[str] = None
    SUPER_ADMIN_EMAIL: str = "admin@ametheus.gg"
    SUPPORT_PHONE: str = "+91 98765 43210"
    SUPPORT_ADMIN_EMAIL: str = "support@ametheus.games"
    ESCROW_SECRET_KEY: str = "ametheus_escrow_vault_secret_2026"

    # =========================================================
    # EMAIL CONFIGURATION (BREVO SMTP & SENDER IDENTITIES)
    # =========================================================
    EMAIL_ENABLED: bool = True

    # Brevo Shared Outgoing SMTP
    SMTP_HOST: str = "smtp-relay.brevo.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_USE_TLS: bool = True

    # Sender Identities
    OFFICIAL_EMAIL: str = "official@ametheus.games"
    OFFICIAL_EMAIL_NAME: str = "Ametheus Official"

    SUPPORT_EMAIL: str = "support@ametheus.games"
    SUPPORT_EMAIL_NAME: str = "Ametheus Support"

    NOREPLY_EMAIL: str = "noreply@ametheus.games"
    NOREPLY_EMAIL_NAME: str = "Ametheus"

    # Default reply-to address
    EMAIL_REPLY_TO: str = "support@ametheus.games"

    # Compatibility alias
    AUTOMATED_EMAIL: str = "noreply@ametheus.games"

    @field_validator(
        "EMAIL_ENABLED",
        "SMTP_USE_TLS",
        mode="before"
    )
    @classmethod
    def resolve_bool_flags(cls, v: Union[bool, str, None]) -> bool:
        if v is None:
            return True
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes", "on", "t")
        return bool(v)

    @field_validator("SMTP_PORT", mode="before")
    @classmethod
    def resolve_smtp_port(cls, v: Union[int, str, None]) -> int:
        if v is None or v == "":
            return 587
        try:
            return int(v)
        except (ValueError, TypeError):
            return 587

    @field_validator("SMTP_HOST", mode="before")
    @classmethod
    def resolve_smtp_host(cls, v: Optional[str]) -> str:
        if v and v.strip():
            return v.strip().strip("'\"")
        for key in ["EMAIL_HOST", "MAIL_SERVER", "SMTP_SERVER"]:
            val = os.getenv(key)
            if val and val.strip():
                return val.strip().strip("'\"")
        return "smtp-relay.brevo.com"

    @field_validator("SMTP_USERNAME", mode="before")
    @classmethod
    def resolve_smtp_username(cls, v: Optional[str]) -> Optional[str]:
        if v and v.strip():
            return v.strip().strip("'\"")
        for key in ["SMTP_USER", "EMAIL_HOST_USER", "MAIL_USERNAME"]:
            val = os.getenv(key)
            if val and val.strip():
                return val.strip().strip("'\"")
        return None

    @field_validator("SMTP_PASSWORD", mode="before")
    @classmethod
    def resolve_smtp_password(cls, v: Optional[str]) -> Optional[str]:
        if v and v.strip():
            return v.strip().strip("'\"")
        for key in ["EMAIL_HOST_PASSWORD", "MAIL_PASSWORD", "SMTP_PASS"]:
            val = os.getenv(key)
            if val and val.strip():
                return val.strip().strip("'\"")
        return None

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def assemble_db_url(cls, v: str) -> str:
        if not v:
            return "sqlite+aiosqlite:///./ametheus.db"
        # Render PostgreSQL URL compatibility
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+asyncpg://", 1)
        elif v.startswith("postgresql://") and not v.startswith("postgresql+asyncpg://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)

        # Strip sslmode=require from query params as asyncpg handles SSL via connect_args or ssl context
        if "sslmode=" in v:
            v = v.replace("?sslmode=require", "").replace("&sslmode=require", "")
        return v

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str):
            return [i.strip() for i in v.split(",") if i.strip()]
        return v


settings = Settings()
