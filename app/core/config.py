import os
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True)

    PROJECT_NAME: str = "AMETHEUS Esports Engine"
    API_V1_STR: str = "/api/v1"
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./ametheus.db")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "ametheus_solar_vanguard_secure_key_2026")
    CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
    ]
    CLERK_ISSUER: str = os.getenv("CLERK_ISSUER", "https://clerk.ametheus.gg")
    RAZORPAY_KEY_ID: str = os.getenv("RAZORPAY_KEY_ID", "rzp_test_ametheus_escrow")
    RAZORPAY_KEY_SECRET: str = os.getenv("RAZORPAY_KEY_SECRET", "mock_secret_key_escrow_123")

settings = Settings()
