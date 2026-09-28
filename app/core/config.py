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
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "https://ametheus-frontend.vercel.app",
    ]
    CLERK_ISSUER: str = "https://clerk.ametheus.gg"
    SUPER_ADMIN_EMAIL: str = "admin@ametheus.gg"
    SUPPORT_PHONE: str = "+91 98765 43210"
    SUPPORT_ADMIN_EMAIL: str = "support.ametheus@gmail.com"
    ESCROW_SECRET_KEY: str = "ametheus_escrow_vault_secret_2026"

    # =========================================================
    # AMETHEUS 3 GMAIL SMTP IDENTITIES & EMAIL SYSTEM
    # =========================================================
    EMAIL_ENABLED: bool = True
    EMAIL_REPLY_TO: str = "support.ametheus@gmail.com"

    # ---------- 1. Support Email ----------
    SUPPORT_EMAIL: str = "support.ametheus@gmail.com"
    SUPPORT_EMAIL_NAME: str = "Ametheus Support"
    SUPPORT_SMTP_HOST: str = "smtp.gmail.com"
    SUPPORT_SMTP_PORT: int = 587
    SUPPORT_SMTP_USERNAME: str = "support.ametheus@gmail.com"
    SUPPORT_SMTP_PASSWORD: Optional[str] = None
    SUPPORT_SMTP_USE_TLS: bool = True

    # ---------- 2. No-Reply / Automated Email ----------
    NOREPLY_EMAIL: str = "noreply.ametheus@gmail.com"
    NOREPLY_EMAIL_NAME: str = "Ametheus"
    NOREPLY_SMTP_HOST: str = "smtp.gmail.com"
    NOREPLY_SMTP_PORT: int = 587
    NOREPLY_SMTP_USERNAME: str = "noreply.ametheus@gmail.com"
    NOREPLY_SMTP_PASSWORD: Optional[str] = None
    NOREPLY_SMTP_USE_TLS: bool = True

    # ---------- 3. Official Email ----------
    OFFICIAL_EMAIL: str = "official.ametheus@gmail.com"
    OFFICIAL_EMAIL_NAME: str = "Ametheus"
    OFFICIAL_SMTP_HOST: str = "smtp.gmail.com"
    OFFICIAL_SMTP_PORT: int = 587
    OFFICIAL_SMTP_USERNAME: str = "official.ametheus@gmail.com"
    OFFICIAL_SMTP_PASSWORD: Optional[str] = None
    OFFICIAL_SMTP_USE_TLS: bool = True

    # Compatibility fallbacks
    AUTOMATED_EMAIL: str = "noreply.ametheus@gmail.com"
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_SECURE: bool = False
    SMTP_FROM_EMAIL: str = "support.ametheus@gmail.com"
    SMTP_FROM_NAME: str = "Ametheus"

    @field_validator(
        "EMAIL_ENABLED",
        "SUPPORT_SMTP_USE_TLS",
        "NOREPLY_SMTP_USE_TLS",
        "OFFICIAL_SMTP_USE_TLS",
        "SMTP_SECURE",
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

    @field_validator(
        "SUPPORT_SMTP_PORT",
        "NOREPLY_SMTP_PORT",
        "OFFICIAL_SMTP_PORT",
        "SMTP_PORT",
        mode="before"
    )
    @classmethod
    def resolve_smtp_ports(cls, v: Union[int, str, None]) -> int:
        if v is None or v == "":
            return 587
        try:
            return int(v)
        except (ValueError, TypeError):
            return 587

    @field_validator("SMTP_HOST", mode="before")
    @classmethod
    def resolve_smtp_host(cls, v: Optional[str]) -> Optional[str]:
        if v:
            return v.strip().strip("'\"")
        for key in ["EMAIL_HOST", "MAIL_SERVER", "SMTP_SERVER"]:
            val = os.getenv(key)
            if val:
                return val.strip().strip("'\"")
        return None

    @field_validator("SMTP_USER", mode="before")
    @classmethod
    def resolve_smtp_user(cls, v: Optional[str]) -> Optional[str]:
        if v:
            return v.strip().strip("'\"")
        for key in ["EMAIL_HOST_USER", "MAIL_USERNAME", "SMTP_USERNAME"]:
            val = os.getenv(key)
            if val:
                return val.strip().strip("'\"")
        return None

    @field_validator("SMTP_PASSWORD", mode="before")
    @classmethod
    def resolve_smtp_password(cls, v: Optional[str]) -> Optional[str]:
        if v:
            return v.strip().strip("'\"")
        for key in ["EMAIL_HOST_PASSWORD", "MAIL_PASSWORD", "SMTP_PASS"]:
            val = os.getenv(key)
            if val:
                return val.strip().strip("'\"")
        return None

    @field_validator("SMTP_FROM_EMAIL", mode="before")
    @classmethod
    def resolve_smtp_from_email(cls, v: Optional[str]) -> str:
        if v and v.strip():
            return v.strip().strip("'\"")
        for key in ["DEFAULT_FROM_EMAIL", "MAIL_FROM"]:
            val = os.getenv(key)
            if val:
                return val.strip().strip("'\"")
        return "support.ametheus@gmail.com"

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
