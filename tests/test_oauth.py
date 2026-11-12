import time
from unittest import mock
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.db import connection

from campfire_integration.models import Workspace
from tests.conftest import APP_URL, BASE_URL, CLIENT_ID


@pytest.mark.django_db
def test_connect_redirects_to_campfire_with_pkce(client) -> None:  # type: ignore[no-untyped-def]
    c = client
    r = c.get("/campfire/connect?return_to=https://evil.example/x")
    assert r.status_code == 302
    url = urlparse(r["Location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{BASE_URL}/ca/oauth/authorize"
    q = parse_qs(url.query)
    assert q["client_id"] == [CLIENT_ID]
    assert q["redirect_uri"] == [f"{APP_URL}/campfire/callback"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["scope"] == ["openid email profile"]
    pending = c.session["campfire_oauth"]
    assert pending["state"] == q["state"][0] and pending["nonce"] == q["nonce"][0]
    assert pending["return_to"] is None  # foreign return targets are dropped


@pytest.mark.django_db
def test_callback_rejects_state_mismatch(client) -> None:  # type: ignore[no-untyped-def]
    client.get("/campfire/connect")
    r = client.get("/campfire/callback?code=abc&state=wrong")
    assert r.status_code == 400
    assert Workspace.objects.count() == 0


@pytest.mark.django_db
def test_callback_creates_workspace_signs_in_and_encrypts_tokens(client) -> None:  # type: ignore[no-untyped-def]
    client.get("/campfire/connect?return_to=https://app.campfire.test/invoices")
    pending = client.session["campfire_oauth"]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    id_token = jwt.encode(
        {"iss": BASE_URL, "aud": CLIENT_ID, "sub": "user-9", "iat": now, "exp": now + 60, "nonce": pending["nonce"],
         "name": "Pat Example", "email": "pat@example.com"},
        key, algorithm="RS256",
    )
    tokens = {"access_token": "at-xyz", "refresh_token": "rt-xyz", "expires_in": 3600, "id_token": id_token, "scope": "openid email profile"}
    whoami = {"customer_id": 777, "customer_name": "Alder Family Office", "install_config": {}}

    with mock.patch("campfire_integration.client.exchange_code", return_value=tokens) as exchange, \
         mock.patch("campfire_integration.client.id_token_signing_key", return_value=key.public_key()), \
         mock.patch("campfire_integration.client.fetch_install_config", return_value=whoami), \
         mock.patch("campfire_integration.client.CampfireClient.put_install_config", return_value={}) as put_config:
        r = client.get(f"/campfire/callback?code=the-code&state={pending['state']}")

    assert r.status_code == 302 and r["Location"] == "https://app.campfire.test/invoices"
    exchange.assert_called_once_with(code="the-code", verifier=pending["verifier"])
    put_config.assert_called_once_with({"base_url": APP_URL})
    ws = Workspace.objects.get(customer_id=777)
    assert ws.name == "Alder Family Office" and ws.access_token == "at-xyz" and ws.is_connected
    assert client.session["workspace_id"] == ws.pk
    assert client.session["campfire_user"]["email"] == "pat@example.com"
    with connection.cursor() as cur:
        cur.execute("SELECT access_token, refresh_token FROM campfire_integration_workspace WHERE id = %s", [ws.pk])
        raw_access, raw_refresh = cur.fetchone()
    assert "at-xyz" not in raw_access and "rt-xyz" not in raw_refresh  # encrypted at rest
