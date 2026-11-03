from typing import Literal, Optional

from app.enums import Environment
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "Church Management System API"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"
    DATABASE_URL: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/church_db"
    )

    # JWT Settings
    # REQUIRED (no default): a known default key lets anyone forge tokens if it ever
    # reaches production. Generate one with:  python -c "import secrets; print(secrets.token_urlsafe(48))"
    SECRET_KEY: str = Field(min_length=32)
    ALGORITHM: Literal["HS256", "HS384", "HS512"] = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60  # short-lived; the refresh token renews it
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # DEVELOPMENT | STAGING | PRODUCTION | TEST (case-insensitive).
    # It is a str-enum, so comparisons like `== "DEVELOPMENT"` keep working.
    ENVIRONMENT: Environment = Environment.DEVELOPMENT

    # CORS Settings
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    # Rate limiting: "memory://" is per-process. Use Redis when running several workers,
    # e.g. "redis://localhost:6379/0"
    RATE_LIMIT_STORAGE_URI: str = "memory://"

    # Initial Superadmin Credentials
    SUPERADMIN_EMAIL: str
    SUPERADMIN_PASSWORD: str = Field(min_length=8)

    # Cloudinary Settings
    CLOUDINARY_API_KEY: Optional[str] = None
    CLOUDINARY_API_SECRET: Optional[str] = None
    CLOUDINARY_CLOUD_NAME: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=True, extra="ignore"
    )

    @field_validator("ENVIRONMENT", mode="before")
    @classmethod
    def _upper_env(cls, v):
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("DATABASE_URL")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        # Hosts often hand out postgres:// or postgresql://, which SQLAlchemy's asyncpg engine rejects.
        for old in ("postgres://", "postgresql://"):
            if v.startswith(old):
                return "postgresql+asyncpg://" + v[len(old) :]
        return v


settings = Settings()
