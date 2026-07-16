from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # App
    APP_ENV: Literal["development", "staging", "production"] = "development"
    APP_NAME: str = "repnex-backend"
    LOG_LEVEL: str = "INFO"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    CORS_ORIGINS: str = "*"
    GRACEFUL_SHUTDOWN_SECONDS: int = 30
    API_PREFIX: str = "/v1"
    SEMANTIC_ERP_DEFAULT: str = "syspro"

    # Metadata DB
    DATABASE_URL: str
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800  # recycle connections every 30min (prevents Neon idle drops)
    RUN_MIGRATIONS: bool = True

    # Redis
    REDIS_URL: str = ""

    # JWT
    JWT_SECRET: str = Field(min_length=32)
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TTL_MIN: int = 480    # 8 hours — full work day without re-login
    JWT_REFRESH_TTL_DAYS: int = 30   # 30 days — monthly rolling refresh
    INVITE_TTL_HOURS: int = 24
    PASSWORD_RESET_TTL_MIN: int = 30

    # Encryption
    FERNET_KEY: str
    FERNET_PREVIOUS_KEYS: str = ""

    # LLM — OpenAI
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    LLM_TIMEOUT_S: int = 20
    LLM_MAX_RETRIES: int = 2
    INTENT_MIN_CONFIDENCE: float = 0.55

    # Pinecone Vector Store
    PINECONE_API_KEY: str = ""
    PINECONE_HOST: str = ""
    PINECONE_INDEX_NAME: str = "repnex"
    PINECONE_NAMESPACE: str = "repnex"

    # LLM — DeepSeek (OpenAI-compatible, used as primary when key is set)
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_MODEL: str = "deepseek-chat"
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"



    # Rate limits (per minute, per user)
    RATE_LIMIT_AUTH_PER_MIN: int = 10
    RATE_LIMIT_QUERY_PER_MIN: int = 30
    RATE_LIMIT_API_PER_MIN: int = 120
    WS_MSG_PER_MIN: int = 30

    # Query executor
    EXECUTOR_TIMEOUT_S: int = 120
    EXECUTOR_MAX_ROWS: int = 100_000
    EXECUTOR_BATCH_SIZE: int = 500
    TARGET_POOL_MAX: int = 64
    TARGET_POOL_MIN_SIZE: int = 2
    TARGET_POOL_MAX_SIZE: int = 10
    MSSQL_POOL_WORKERS: int = 32

    # Email
    EMAIL_PROVIDER: Literal["console", "smtp"] = "smtp"
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    APP_BASE_URL: str = "http://localhost:5173"

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _strip_cors(cls, v: str) -> str:
        return v.strip()

    @property
    def cors_origins_list(self) -> list[str]:
        if self.CORS_ORIGINS in ("", "*"):
            return ["*"]
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def fernet_previous_list(self) -> list[str]:
        return [k.strip() for k in self.FERNET_PREVIOUS_KEYS.split(",") if k.strip()]

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
