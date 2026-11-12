import json
import time
import uuid
from datetime import timedelta
from typing import Any

import jwt
import pytest
from django.utils import timezone

from campfire_integration.models import Workspace
from campfire_integration.security import sign

CLIENT_ID = "client-123"
SIGNING_SECRET = "whsec_test_signing_secret_0123456789abcdef"
BASE_URL = "https://api.campfire.test"
FRONTEND = "https://app.campfire.test"
APP_URL = "https://fo.test"


@pytest.fixture(autouse=True)
def campfire_settings(settings):  # type: ignore[no-untyped-def]
    settings.APP_URL = APP_URL
    settings.CAMPFIRE = {
        **settings.CAMPFIRE,
        "BASE_URL": BASE_URL,
        "FRONTEND_ORIGIN": FRONTEND,
        "CLIENT_ID": CLIENT_ID,
        "CLIENT_SECRET": "client-secret",
        "SIGNING_SECRETS": [SIGNING_SECRET],
        "SCOPES": ["openid", "email", "profile"],
    }
    return settings


@pytest.fixture
def workspace(db) -> Workspace:  # type: ignore[no-untyped-def]
    return Workspace.objects.create(
        customer_id=4242,
        name="Test Workspace",
        access_token="access-1",
        refresh_token="refresh-1",
        token_expires_at=timezone.now() + timedelta(hours=1),
        connected_at=timezone.now(),
    )


def context_token(*, customer_id: int, surface: str, object_type: str | None = None, object_ids: list[Any] | None = None,
                  secret: str = SIGNING_SECRET, exp_in: int = 120, **extra: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "campfire", "aud": CLIENT_ID, "iat": now, "exp": now + exp_in, "jti": uuid.uuid4().hex,
        "customer_id": customer_id, "surface": surface, "object_ids": object_ids or [], **extra,
    }
    if object_type:
        claims["object_type"] = object_type
    return jwt.encode(claims, secret, algorithm="HS256")


def signed_headers(body: bytes, *, secret: str = SIGNING_SECRET, timestamp: int | None = None) -> dict[str, str]:
    ts = str(timestamp if timestamp is not None else int(time.time()))
    return {
        "HTTP_CAMPFIRE_WEBHOOK_REQUEST_TIMESTAMP": ts,
        "HTTP_CAMPFIRE_WEBHOOK_SIGNATURE_V1": sign(secret=secret, timestamp=ts, body=body),
    }


def as_json(payload: Any) -> bytes:
    return json.dumps(payload).encode()
