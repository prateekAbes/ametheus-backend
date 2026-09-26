import os
from typing import List, Union
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
    SUPPORT_EMAIL: str = "support@ametheus.gg"
    ESCROW_SECRET_KEY: str = "ametheus_escrow_vault_secret_2026"

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
