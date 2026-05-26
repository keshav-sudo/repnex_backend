from __future__ import annotations


def test_encrypt_roundtrip(settings):
    from app.core.security import encryption

    encryption._fernet = None
    cipher = encryption.encrypt("hello")
    assert cipher != "hello"
    assert encryption.decrypt(cipher) == "hello"


def test_passwords_roundtrip():
    from app.core.security.passwords import hash_password, verify_password

    h = hash_password("supersecret123")
    assert verify_password("supersecret123", h)
    assert not verify_password("wrong", h)
