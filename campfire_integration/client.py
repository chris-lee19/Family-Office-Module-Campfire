"""Talking to Campfire: the OAuth handshake, and API calls with a workspace's token (refreshed when needed)."""

import base64
import hashlib
import logging
import secrets
from datetime import timedelta
from functools import lru_cache
from typing import Any
from urllib.parse import urlencode

import jwt
import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from campfire_integration.models import Workspace

log = logging.getLogger(__name__)

INSTALL_CONFIG_PATH = "/ca/api/v1/oauth-app-install/config"
REFRESH_MARGIN = timedelta(seconds=30)


class NotConnected(Exception):
    """No usable grant for the workspace: never connected, revoked, or refresh refused."""


class CampfireError(Exception):
    def __init__(self, response: requests.Response) -> None:
        self.status_code = response.status_code
        self.body = response.text[:2000]
        super().__init__(f"Campfire {response.request.method} {response.url} -> {response.status_code}: {self.body[:300]}")


def _cfg(key: str) -> Any:
    return settings.CAMPFIRE[key]


def redirect_uri() -> str:
    return settings.APP_URL + _cfg("REDIRECT_PATH")


def token_url() -> str:
    return f"{_cfg('BASE_URL')}/ca/oauth/token"


def issuer() -> str:
    return _cfg("BASE_URL")


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


# Resources where the app creates records that people should approve in Campfire.
DRAFTED_RESOURCES = ("journal_entries", "bills")


def connect_scopes() -> list[str]:
    """Scopes to request when a workspace connects: the manifest ceiling, with journal entries and bills at the
    draft tier unless the app is set to post directly. Draft-tier writes land in Campfire's approval queue."""
    scopes = list(_cfg("SCOPES"))
    if _cfg("POSTING_MODE") == "direct":
        return scopes
    return [f"{s.split(':')[0]}:draft" if s.split(":")[0] in DRAFTED_RESOURCES and s.endswith(":write") else s for s in scopes]


def authorize_url(*, state: str, nonce: str, code_challenge: str) -> str:
    query = {
        "client_id": _cfg("CLIENT_ID"),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(connect_scopes()),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{_cfg('BASE_URL')}/ca/oauth/authorize?{urlencode(query)}"


def exchange_code(*, code: str, verifier: str) -> dict[str, Any]:
    r = requests.post(
        token_url(),
        auth=(_cfg("CLIENT_ID"), _cfg("CLIENT_SECRET")),
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(), "code_verifier": verifier},
        timeout=10,
    )
    if r.status_code != 200:
        raise CampfireError(r)
    return r.json()


@lru_cache(maxsize=1)
def _jwks_client() -> jwt.PyJWKClient:
    # Caches Campfire's public keys; refetches when a token names a key it has not seen.
    return jwt.PyJWKClient(f"{_cfg('BASE_URL')}/.well-known/jwks.json")


def id_token_signing_key(id_token: str) -> Any:
    return _jwks_client().get_signing_key_from_jwt(id_token).key


def send(*, access_token: str, method: str, path: str, json: Any = None, params: Any = None, timeout: float = 15) -> requests.Response:
    return requests.request(
        method,
        f"{_cfg('BASE_URL')}{path}",
        headers={"Authorization": f"Bearer {access_token}"},
        json=json,
        params=params,
        timeout=timeout,
    )


def fetch_install_config(*, access_token: str) -> dict[str, Any]:
    """Which workspace a fresh token belongs to (`customer_id`, `customer_name`) plus its install config."""
    r = send(access_token=access_token, method="GET", path=INSTALL_CONFIG_PATH)
    if r.status_code != 200:
        raise CampfireError(r)
    return r.json()


class CampfireClient:
    """API calls on behalf of one connected workspace."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def _current_token(self) -> str:
        ws = self.workspace
        if not ws.is_connected:
            raise NotConnected(f"workspace {ws.customer_id} is not connected")
        if ws.token_expires_at is None or ws.token_expires_at - REFRESH_MARGIN < timezone.now():
            self.refresh(stale_token=ws.access_token)
        return self.workspace.access_token

    def refresh(self, *, stale_token: str) -> None:
        """Rotate the refresh token under a row lock so two workers never spend the same refresh token."""
        with transaction.atomic():
            ws = Workspace.objects.select_for_update().get(pk=self.workspace.pk)
            fresh = ws.token_expires_at is not None and ws.token_expires_at - REFRESH_MARGIN > timezone.now()
            if ws.access_token and ws.access_token != stale_token and fresh:
                self.workspace = ws  # another worker already refreshed
                return
            if not ws.refresh_token:
                raise NotConnected(f"workspace {ws.customer_id} has no refresh token")
            r = requests.post(
                token_url(),
                auth=(_cfg("CLIENT_ID"), _cfg("CLIENT_SECRET")),
                data={"grant_type": "refresh_token", "refresh_token": ws.refresh_token},
                timeout=10,
            )
            refused = r.status_code != 200
            if not refused:
                ws.apply_tokens(r.json())
                ws.save()
            self.workspace = ws
        if refused:
            # Outside the atomic block: raising inside it would roll the disconnect back.
            log.warning("refresh refused for workspace %s: %s", ws.customer_id, r.status_code)
            ws.drop_grant()
            raise NotConnected(f"refresh failed: {r.status_code}")

    def request(self, method: str, path: str, *, json: Any = None, params: Any = None, timeout: float = 15) -> requests.Response:
        token = self._current_token()
        r = send(access_token=token, method=method, path=path, json=json, params=params, timeout=timeout)
        if r.status_code != 401:
            return r
        self.refresh(stale_token=token)
        return send(access_token=self.workspace.access_token, method=method, path=path, json=json, params=params, timeout=timeout)

    def get_json(self, path: str, *, params: Any = None) -> Any:
        r = self.request("GET", path, params=params)
        if r.status_code >= 400:
            raise CampfireError(r)
        return r.json()

    def put_install_config(self, config: dict[str, str]) -> dict[str, str]:
        r = self.request("PUT", INSTALL_CONFIG_PATH, json={"install_config": config})
        if r.status_code >= 400:
            raise CampfireError(r)
        saved: dict[str, str] = r.json().get("install_config", config)
        self.workspace.install_config = saved
        self.workspace.save(update_fields=["install_config", "updated_at"])
        return saved
