from datetime import timedelta
from unittest import mock

import pytest
import requests
from django.utils import timezone

from campfire_integration.client import CampfireClient, NotConnected
from campfire_integration.models import Workspace


def _response(status: int, payload: object = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = b"{}" if payload is None else __import__("json").dumps(payload).encode()
    r.request = requests.Request("GET", "https://api.campfire.test/x").prepare()
    r.url = "https://api.campfire.test/x"
    return r


@pytest.mark.django_db(transaction=True)
def test_401_triggers_refresh_and_retry(workspace: Workspace) -> None:
    calls: list[str] = []

    def fake_request(method, url, headers, **kw):  # type: ignore[no-untyped-def]
        calls.append(headers["Authorization"])
        return _response(401) if headers["Authorization"] == "Bearer access-1" else _response(200, {"ok": True})

    refreshed = _response(200, {"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600})
    with mock.patch("requests.request", side_effect=fake_request), mock.patch("requests.post", return_value=refreshed) as post:
        assert CampfireClient(workspace).get_json("/coa/api/thing") == {"ok": True}
    assert calls == ["Bearer access-1", "Bearer access-2"]
    assert post.call_args.kwargs["data"] == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}
    workspace.refresh_from_db()
    assert workspace.access_token == "access-2" and workspace.refresh_token == "refresh-2"


@pytest.mark.django_db(transaction=True)
def test_expired_token_refreshes_before_call_and_refused_refresh_disconnects(workspace: Workspace) -> None:
    workspace.token_expires_at = timezone.now() - timedelta(minutes=1)
    workspace.save()
    with mock.patch("requests.post", return_value=_response(400, {"error": "invalid_grant"})), mock.patch("requests.request") as req:
        with pytest.raises(NotConnected):
            CampfireClient(workspace).get_json("/coa/api/thing")
        req.assert_not_called()
    workspace.refresh_from_db()
    assert not workspace.is_connected and workspace.disconnected_at is not None
