from __future__ import annotations

import uuid

import pytest

from app.core.exceptions import Unauthorized
from app.core.security.auth import (
    create_access_token,
    create_refresh_token,
    current_user_from_payload,
    decode_token,
)


def test_access_token_roundtrip(settings):
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    token = create_access_token(
        user_id=user_id, org_id=org_id, email="a@b.co", role="admin"
    )
    payload = decode_token(token, expected_type="access")
    cu = current_user_from_payload(payload)
    assert cu.user_id == user_id
    assert cu.org_id == org_id
    assert cu.role == "admin"


def test_refresh_rejected_as_access(settings):
    user_id = uuid.uuid4()
    org_id = uuid.uuid4()
    refresh = create_refresh_token(user_id=user_id, org_id=org_id)
    with pytest.raises(Unauthorized):
        decode_token(refresh, expected_type="access")
