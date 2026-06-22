from __future__ import annotations

import os

import pytest

# Sane defaults for unit tests so Settings() doesn't blow up.
os.environ.setdefault("DATABASE_URL", "mongodb://localhost:27017/repnex_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "x" * 48)
# A valid Fernet key for tests.
os.environ.setdefault("FERNET_KEY", "Q5p_T8u-3JkM4G2HzVx6yYbN7cJgKLpQRsTuVwXyZaA=")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("OPENAI_API_KEY", "test")


@pytest.fixture
def settings():
    from app.core.config import get_settings

    get_settings.cache_clear()
    return get_settings()
