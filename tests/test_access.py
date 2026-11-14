"""Roles: off until an admin turns them on; then view / prepare / approve / manage are enforced on every page."""

from typing import Any
from unittest import mock
from urllib.parse import unquote

import pytest

from campfire_integration import access
from campfire_integration.models import Membership, Workspace
from gl.models import BillRequest
from records.models import AuditEntry
from tests.conftest import context_token
from tests.test_bills import ENTRY, FakeCampfire


def test_what_each_request_needs() -> None:
    need = access.required
    assert need("bills", "GET", "") == access.VIEW and need("bills", "POST", "enter") == access.PREPARE and need("bills", "POST", "pay") == access.PREPARE
    assert need("bills", "POST", "approve") == access.APPROVE and need("review", "POST", "sign_off") == access.APPROVE and need("close", "POST", "signoff") == access.APPROVE
    assert need("gl", "POST", "map") == access.MANAGE and need("close", "POST", "reopen") == access.MANAGE and need("access", "POST", "set") == access.MANAGE
    assert need("gl", "POST", "tieout") == access.VIEW and need("investments", "POST", "refresh_balances") == access.VIEW
    assert need("anything", "POST", "something_new") == access.PREPARE  # a new write action is closed to viewers by default
    assert access.ROLES[access.CONTROLLER][2] == {access.VIEW, access.PREPARE, access.APPROVE} and not access.ROLES[access.NONE][2]


def _token(client: Any, ws: Workspace, user: int, page: str = "bills-page") -> str:
    html = client.get(f"/fo/frame/{page}?cf_ctx={context_token(customer_id=ws.customer_id, surface='nav', user_id=user)}").content.decode()
    return unquote(html.split('name="fo_t" value="')[1].split('"')[0])


@pytest.mark.django_db
def test_roles_off_then_on_and_enforced(client, workspace: Workspace) -> None:  # type: ignore[no-untyped-def]
    ws = workspace
    with mock.patch("gl.bill_views.CampfireClient", return_value=FakeCampfire()):
        # Off: anyone can do everything (as before roles existed).
        pat = _token(client, ws, 3)
        assert "now waits for approval" in client.post("/fo/frame/bills-page", {"fo_t": pat, "action": "enter", **ENTRY}).content.decode()

        # Pat turns roles on and becomes the admin; everyone else is a viewer by default.
        people = client.post("/fo/frame/people", {"fo_t": _token(client, ws, 3, "people"), "action": "turn_on"}).content.decode()
        assert "Roles are on" in people and Membership.objects.get().role == access.ADMIN
        rae = _token(client, ws, 4)
        page = client.get("/fo/frame/bills-page", {"fo_t": rae})
        assert page.status_code == 200 and "View only" in page.content.decode()
        bill = BillRequest.objects.get()
        refused = client.post("/fo/frame/bills-page", {"fo_t": rae, "action": "approve", "bill": bill.pk})
        assert refused.status_code == 403 and "Your role is Viewer" in refused.content.decode() and "Approver or Controller" in refused.content.decode()
        assert client.post("/fo/frame/bills-page", {"fo_t": rae, "action": "enter", **ENTRY, "bill_number": "X-2"}).status_code == 403
        assert client.post("/fo/frame/people", {"fo_t": rae, "action": "set", "user": "cfu:4", "role": "admin"}).status_code == 403  # can't promote yourself
        bill.refresh_from_db()
        assert bill.status == BillRequest.Status.PENDING and BillRequest.objects.count() == 1

        # The admin makes Rae an approver: she can approve, still can't enter.
        admin = _token(client, ws, 3, "people")
        out = client.post("/fo/frame/people", {"fo_t": admin, "action": "set", "user": "cfu:4", "role": "approver"}).content.decode()
        assert "Campfire user 4: Approver." in out
        assert "approved and in Campfire" in client.post("/fo/frame/bills-page", {"fo_t": rae, "action": "approve", "bill": bill.pk}).content.decode()
        assert client.post("/fo/frame/bills-page", {"fo_t": rae, "action": "enter", **ENTRY, "bill_number": "X-3"}).status_code == 403

        # An accountant enters but doesn't approve; the only admin can't be demoted; unlisted people can be shut out.
        client.post("/fo/frame/people", {"fo_t": admin, "action": "set", "user": "cfu:5", "role": "accountant"})
        sam = _token(client, ws, 5)
        assert "now waits for approval" in client.post("/fo/frame/bills-page", {"fo_t": sam, "action": "enter", **ENTRY, "bill_number": "X-4"}).content.decode()
        assert client.post("/fo/frame/bills-page", {"fo_t": sam, "action": "approve", "bill": BillRequest.objects.get(bill_number="X-4").pk}).status_code == 403
        assert "only admin" in client.post("/fo/frame/people", {"fo_t": admin, "action": "set", "user": "cfu:3", "role": "viewer"}).content.decode()
        assert "No access" in client.post("/fo/frame/people", {"fo_t": admin, "action": "default", "role": "none"}).content.decode()
        stranger = client.get(f"/fo/frame/bills-page?cf_ctx={context_token(customer_id=ws.customer_id, surface='nav', user_id=9)}")
        assert stranger.status_code == 403 and "don&#x27;t have access" in stranger.content.decode()
        assert AuditEntry.objects.filter(kind="roles").count() == 4

        # Turning roles off puts things back.
        assert "Roles are off" in client.post("/fo/frame/people", {"fo_t": admin, "action": "turn_off"}).content.decode()
        assert client.get(f"/fo/frame/bills-page?cf_ctx={context_token(customer_id=ws.customer_id, surface='nav', user_id=9)}").status_code == 200
