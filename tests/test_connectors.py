"""Connectors: an honest catalog (what works today, what can be asked for) and recording a request."""

import pytest

from campfire_integration.models import Workspace
from investments import connectors as cat
from investments.models import ConnectorRequest


def test_catalog_is_consistent() -> None:
    assert len(cat.BY_KEY) == len(cat.CATALOG) and all(c.group in cat.GROUPS for c in cat.CATALOG)
    working = [c for c in cat.CATALOG if c.status != cat.ON_REQUEST]
    assert working and all(c.target for c in working)  # everything that works today opens a page
    assert not any(c.target for c in cat.CATALOG if c.status == cat.ON_REQUEST)  # nothing unbuilt pretends to open
    assert {"addepar", "masttro", "jpmorgan", "arch"} <= set(cat.BY_KEY)


@pytest.mark.django_db
def test_connectors_page_and_requests(client, workspace: Workspace) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import unquote

    from tests.conftest import context_token

    page = client.get(f"/fo/frame/connectors?cf_ctx={context_token(customer_id=workspace.customer_id, surface='nav', user_id=3)}").content.decode()
    assert "Addepar" in page and "J.P. Morgan Private Bank" in page and "Connected" in page and "On request" in page
    assert 'href="/fo/frame/imports?fo_t=' in page  # the file importer opens from here
    token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
    post = lambda data: client.post("/fo/frame/connectors", {"fo_t": token, **data}).content.decode()  # noqa: E731
    assert "Choose a connector that can be requested" in post({"connector": "campfire", "action": "request"})
    out = post({"connector": "addepar", "action": "request"})
    assert "Addepar requested" in out and "Requested" in out and "Withdraw" in out
    assert post({"connector": "addepar", "action": "request"}).count("Withdraw") == 1 and ConnectorRequest.objects.count() == 1  # asking twice is one request
    assert "request withdrawn" in post({"connector": "addepar", "action": "withdraw"}) and not ConnectorRequest.objects.exists()
