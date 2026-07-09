from __future__ import annotations

from app.core.config import get_settings
from cryptography.fernet import Fernet, InvalidToken, MultiFernet


def _build_fernet() -> MultiFernet:
    settings = get_settings()
    keys = [Fernet(settings.FERNET_KEY.encode())]
    keys.extend(Fernet(k.encode()) for k in settings.fernet_previous_list)
    return MultiFernet(keys)


_fernet: MultiFernet | None = None


def _f() -> MultiFernet:
    global _fernet
    if _fernet is None:
        _fernet = _build_fernet()
    return _fernet


def encrypt(plain: str) -> str:
    return _f().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _f().decrypt(token.encode()).decode()
    except InvalidToken as e:  # pragma: no cover
        raise ValueError("Failed to decrypt value") from e


def reencrypt(token: str) -> str:
    return _f().rotate(token.encode()).decode()
