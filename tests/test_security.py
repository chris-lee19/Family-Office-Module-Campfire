import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from campfire_integration.models import UsedContextToken
from campfire_integration.security import (
    ContextTokenError,
    IdTokenError,
    sign,
    verify_context_token,
    verify_id_token,
    verify_signature,
)
from tests.conftest import CLIENT_ID, SIGNING_SECRET, context_token


def _headers(ts: str, sig: str, previous: str | None = None) -> dict[str, str]:
    h = {"Campfire-Webhook-Request-Timestamp": ts, "Campfire-Webhook-Signature-v1": sig}
    if previous:
        h["Campfire-Webhook-Signature-v1-Previous"] = previous
    return h


def test_signature_accepts_valid_and_rejects_tampered_or_stale() -> None:
    body, now = b'{"a":1}', time.time()
    ts = str(int(now))
    good = sign(secret=SIGNING_SECRET, timestamp=ts, body=body)
    assert verify_signature(secrets=[SIGNING_SECRET], headers=_headers(ts, good), body=body, now=now)
    assert not verify_signature(secrets=[SIGNING_SECRET], headers=_headers(ts, good), body=b'{"a":2}', now=now)
    assert not verify_signature(secrets=["other"], headers=_headers(ts, good), body=body, now=now)
    assert not verify_signature(secrets=[SIGNING_SECRET], headers=_headers(ts, good), body=body, now=now + 301)


def test_signature_rotation_either_side() -> None:
    body, now = b"{}", time.time()
    ts = str(int(now))
    old_sig = sign(secret="old", timestamp=ts, body=body)
    new_sig = sign(secret="new", timestamp=ts, body=body)
    # Campfire signs with both during rotation; we may hold either secret.
    assert verify_signature(secrets=["old"], headers=_headers(ts, new_sig, previous=old_sig), body=body, now=now)
    assert verify_signature(secrets=["new", "old"], headers=_headers(ts, old_sig), body=body, now=now)


@pytest.mark.django_db
def test_context_token_single_use() -> None:
    token = context_token(customer_id=1, surface="nav")
    claims = verify_context_token(token=token, secrets=[SIGNING_SECRET], client_id=CLIENT_ID, claim_jti=UsedContextToken.claim)
    assert claims["surface"] == "nav"
    with pytest.raises(ContextTokenError, match="already used"):
        verify_context_token(token=token, secrets=[SIGNING_SECRET], client_id=CLIENT_ID, claim_jti=UsedContextToken.claim)


@pytest.mark.django_db
def test_context_token_wrong_secret_audience_or_expired() -> None:
    claim = UsedContextToken.claim
    with pytest.raises(ContextTokenError, match="bad signature"):
        verify_context_token(token=context_token(customer_id=1, surface="nav", secret="a-different-secret-that-is-long-enough-xx"), secrets=[SIGNING_SECRET], client_id=CLIENT_ID, claim_jti=claim)
    with pytest.raises(ContextTokenError):
        verify_context_token(token=context_token(customer_id=1, surface="nav"), secrets=[SIGNING_SECRET], client_id="someone-else", claim_jti=claim)
    with pytest.raises(ContextTokenError):
        verify_context_token(token=context_token(customer_id=1, surface="nav", exp_in=-10), secrets=[SIGNING_SECRET], client_id=CLIENT_ID, claim_jti=claim)


def test_id_token_checks_nonce_and_signature() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    claims = {"iss": "https://api.campfire.test", "aud": CLIENT_ID, "sub": "u1", "iat": now, "exp": now + 60, "nonce": "n1"}
    token = jwt.encode(claims, key, algorithm="RS256")
    ok = verify_id_token(id_token=token, signing_key=key.public_key(), issuer="https://api.campfire.test", client_id=CLIENT_ID, nonce="n1")
    assert ok["sub"] == "u1"
    with pytest.raises(IdTokenError, match="nonce"):
        verify_id_token(id_token=token, signing_key=key.public_key(), issuer="https://api.campfire.test", client_id=CLIENT_ID, nonce="n2")
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(IdTokenError):
        verify_id_token(id_token=token, signing_key=other.public_key(), issuer="https://api.campfire.test", client_id=CLIENT_ID, nonce="n1")
