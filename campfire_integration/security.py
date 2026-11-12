"""Checking that a request really came from Campfire: body signatures, `cf_ctx` context tokens, ID tokens.

Mirrors the verification in Campfire's sample partner app (Campfire-eng/campfire_faketax_app).
"""

import hashlib
import hmac
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import jwt

TIMESTAMP_HEADER = "Campfire-Webhook-Request-Timestamp"
SIGNATURE_HEADER = "Campfire-Webhook-Signature-v1"
PREVIOUS_SIGNATURE_HEADER = "Campfire-Webhook-Signature-v1-Previous"
MAX_AGE_SECONDS = 300
CONTEXT_TOKEN_ISSUER = "campfire"


class ContextTokenError(Exception):
    pass


class IdTokenError(Exception):
    pass


def sign(*, secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()


def verify_signature(
    *, secrets: Sequence[str], headers: Mapping[str, str], body: bytes, now: float, max_age: int = MAX_AGE_SECONDS
) -> bool:
    """HMAC-SHA256 over "{timestamp}:{body}". Any of our secrets may match either signature header (rotation)."""
    lowered = {key.lower(): value for key, value in headers.items()}
    timestamp = lowered.get(TIMESTAMP_HEADER.lower(), "")
    if not timestamp.isdigit() or abs(now - int(timestamp)) > max_age:
        return False
    candidates = [
        value
        for value in (lowered.get(SIGNATURE_HEADER.lower()), lowered.get(PREVIOUS_SIGNATURE_HEADER.lower()))
        if value
    ]
    for secret in secrets:
        if not secret:
            continue
        expected = sign(secret=secret, timestamp=timestamp, body=body)
        if any(hmac.compare_digest(expected, candidate) for candidate in candidates):
            return True
    return False


def decode_context_token(*, token: str, secrets: Sequence[str], client_id: str) -> dict[str, Any]:
    """Signature, expiry, audience and issuer only; replay protection is the caller's job."""
    last_error: Exception | None = None
    for secret in secrets:
        if not secret:
            continue
        try:
            return jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience=client_id,
                issuer=CONTEXT_TOKEN_ISSUER,
                options={"require": ["exp", "iat", "jti", "aud", "iss"]},
            )
        except jwt.InvalidSignatureError as e:
            last_error = e
        except jwt.PyJWTError as e:
            raise ContextTokenError(str(e)) from e
    raise ContextTokenError(f"bad signature: {last_error}")


def verify_context_token(
    *, token: str, secrets: Sequence[str], client_id: str, claim_jti: Callable[[str, float], bool]
) -> dict[str, Any]:
    """A verified, single-use context token. `claim_jti(jti, exp)` returns False when already seen."""
    claims = decode_context_token(token=token, secrets=secrets, client_id=client_id)
    if not claim_jti(str(claims["jti"]), float(claims["exp"])):
        raise ContextTokenError("context token already used")
    return claims


def token_covers(*, claims: Mapping[str, Any], object_type: str, object_id: str) -> bool:
    return claims.get("object_type") == object_type and object_id in [str(i) for i in claims.get("object_ids", [])]


def verify_id_token(*, id_token: str, signing_key: Any, issuer: str, client_id: str, nonce: str) -> dict[str, Any]:
    """An OpenID Connect ID token from Campfire: RS256 signature, issuer, audience, expiry and our nonce."""
    try:
        claims = jwt.decode(
            id_token,
            signing_key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=issuer,
            options={"require": ["sub", "exp", "iat", "aud", "iss", "nonce"]},
        )
    except jwt.PyJWTError as e:
        raise IdTokenError(str(e)) from e
    if not hmac.compare_digest(str(claims["nonce"]), nonce):
        raise IdTokenError("nonce mismatch")
    return claims
