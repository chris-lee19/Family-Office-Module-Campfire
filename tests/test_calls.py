import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
import requests

from campfire_integration import events
from campfire_integration.models import CampfireEvent, Workspace
from entities.demo import seed_alder
from entities.models import Entity, Family
from gl.models import AccountMapping, Role
from gl.services import calls
from gl.services.posting import build_preview
from gl.services.setup import SUGGESTED_ACCOUNTS
from investments.demo import seed_alder_investments
from investments.models import CapitalCall, Transaction
from investments.services.privates import summarize

TODAY = date(2026, 6, 30)
S = CapitalCall.Status
LP = 9004


def _response(status: int, payload: Any = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(payload if payload is not None else {}).encode()
    r.request = requests.Request("GET", "https://api.campfire.test/x").prepare()
    r.url = "https://api.campfire.test/x"
    return r


class FakeCampfire:
    """Answers bill requests like Campfire: POST /bill -> 202 draft; GET /bill (search) and /bill/<id> from `bills`."""

    def __init__(self, post_status: int = 202) -> None:
        self.post_status = post_status
        self.bills: dict[int, dict[str, Any]] = {}
        self.posted: list[dict[str, Any]] = []
        self.vendors = [{"id": 501, "name": "Summit Growth Partners"}, {"id": 502, "name": "Crescent Ventures"}]
        self.bank_lines: list[dict[str, Any]] = []  # Campfire bank-feed lines (/ca/api/transaction)

    def approve(self, call: CapitalCall, bill_id: int = 7001) -> dict[str, Any]:
        body = self.posted[-1]
        self.bills[bill_id] = {**body, "id": bill_id, "payment_status": "open", "paid_date": None, "is_deleted": False}
        return self.bills[bill_id]

    def request(self, method: str, path: str, *, json: Any = None, params: Any = None, timeout: float = 15) -> requests.Response:
        if method == "POST" and path == calls.BILL_PATH:
            self.posted.append(json)
            return _response(self.post_status, {"id": 9999} if self.post_status == 201 else {"draft_id": 1, "detail": "Submitted for approval"})
        if method == "GET" and path.startswith("/ca/api/transaction/"):
            line = next((b for b in self.bank_lines if str(b["id"]) == path.rsplit("/", 1)[1]), None)
            return _response(200, line) if line else _response(404, {"detail": "Not found."})
        if method == "GET" and path.startswith(calls.BILL_PATH + "/"):
            bill = self.bills.get(int(path.rsplit("/", 1)[1]))
            return _response(200, bill) if bill else _response(404, {"detail": "Not found."})
        raise AssertionError((method, path))

    def get_json(self, path: str, *, params: Any = None) -> Any:
        if path == calls.BILL_PATH:
            return {"results": [b for b in self.bills.values() if str(b["vendor"]) == str(params["vendor"])], "next": None}
        if path.startswith(calls.BILL_PATH + "/"):
            return self.bills[int(path.rsplit("/", 1)[1])]
        if path == calls.VENDOR_PATH:
            return {"results": self.vendors, "next": None}
        if path == "/ca/api/transaction":
            return {"results": self.bank_lines, "next": None}
        raise AssertionError(path)


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    Entity.objects.filter(family=family, name="Alder Family Investments LP").update(campfire_entity_id=LP)
    for s in SUGGESTED_ACCOUNTS:
        AccountMapping.objects.create(workspace=workspace, role=s.role, campfire_account_id=int(s.number), campfire_account_label=f"{s.number} {s.name}")
    return family


def the_call() -> CapitalCall:
    return CapitalCall.objects.select_related("commitment__account__entity", "commitment__instrument").get()


@pytest.mark.django_db
def test_call_to_bill_to_paid(alder: Family) -> None:
    call = the_call()
    assert any("Campfire vendor" in p for p in calls.problems(call))
    commitment = call.commitment
    commitment.campfire_vendor_id, commitment.campfire_vendor_name = 501, "Summit Growth Partners"
    commitment.save()
    call = the_call()
    assert calls.problems(call) == []
    unfunded_before = summarize(commitment, TODAY).unfunded

    cf = FakeCampfire()
    call = calls.send(call, cf, actor="Pat")  # type: ignore[arg-type]
    assert call.status == S.SUBMITTED and call.bill_sent_by == "Pat"
    body = cf.posted[0]
    assert (body["source"], body["source_id"], body["vendor"], body["entity"]) == ("Family Office", f"fo-call-{call.pk}", "501", str(LP))
    assert body["lines"][0]["account"] == "1500" and body["lines"][0]["amount"] == 500000.0
    assert (body["terms"], body["bill_date"], body["due_date"]) == ("net_15", call.notice_date.isoformat(), call.due_date.isoformat())

    # Not approved yet: nothing in Campfire's bill list, the call keeps waiting.
    assert calls.sync(call, cf).status == S.SUBMITTED  # type: ignore[arg-type]
    cf.approve(call)
    call = calls.sync(call, cf)  # type: ignore[arg-type]
    assert (call.status, call.campfire_bill_id) == (S.BILLED, 7001)

    paid_on = TODAY + timedelta(days=8)
    cf.bills[7001].update(payment_status="paid", paid_date=paid_on.isoformat())
    call = calls.sync(call, cf)  # type: ignore[arg-type]
    assert (call.status, call.paid_on) == (S.PAID, paid_on)
    contribution = call.contribution
    assert (contribution.txn_type, contribution.amount, contribution.trade_date) == (Transaction.Type.CONTRIBUTION, Decimal("500000"), paid_on)
    assert summarize(commitment, paid_on).unfunded == unfunded_before - Decimal("500000")
    assert calls.sync(call, cf).contribution == contribution  # type: ignore[arg-type]  # once only
    # The bill booked it in the GL; the bridge doesn't post the contribution again.
    preview = build_preview(alder.workspace, through=paid_on)
    assert preview.billed_contributions == 1 and all(contribution not in j.transactions for j in preview.journals)


@pytest.mark.django_db
def test_bill_events_and_deleted_bill(alder: Family) -> None:
    c = the_call().commitment
    c.campfire_vendor_id = 501
    c.save()
    cf = FakeCampfire(post_status=201)  # a workspace where bills post directly
    call = calls.send(the_call(), cf)  # type: ignore[arg-type]
    assert (call.status, call.campfire_bill_id) == (S.BILLED, 9999)
    cf.bills[9999] = {**cf.posted[0], "id": 9999, "payment_status": "paid", "paid_date": TODAY.isoformat(), "is_deleted": False}
    ws = alder.workspace
    with mock.patch("gl.handlers.CampfireClient", return_value=cf):
        events.dispatch(CampfireEvent.objects.create(workspace=ws, customer_id=ws.customer_id, topic="Bill.paid", object_id="9999", payload={}))
    assert the_call().status == S.PAID

    # A bill deleted in Campfire before payment sends the call back.
    other = CapitalCall.objects.create(commitment=c, notice_date=TODAY, due_date=TODAY + timedelta(days=10), amount=Decimal("100"),
                                       status=S.BILLED, campfire_bill_id=4242)
    events.dispatch(CampfireEvent.objects.create(workspace=ws, customer_id=ws.customer_id, topic="Bill.deleted", object_id="4242", payload={}))
    other.refresh_from_db()
    assert (other.status, other.campfire_bill_id) == (S.RECEIVED, None) and "deleted" in other.bill_note


@pytest.mark.django_db
def test_refused_bill_leaves_the_call(alder: Family) -> None:
    c = the_call().commitment
    c.campfire_vendor_id = 501
    c.save()
    cf = FakeCampfire(post_status=400)
    with pytest.raises(calls.CallError, match="refused"):
        calls.send(the_call(), cf)  # type: ignore[arg-type]
    call = the_call()
    assert call.status == S.RECEIVED and "400" in call.bill_note


@pytest.mark.django_db
def test_calls_page_vendor_match_send_and_distribution(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import unquote

    from tests.conftest import context_token

    ws = alder.workspace
    cf = FakeCampfire()
    with mock.patch("gl.call_views.CampfireClient", return_value=cf):
        r = client.get(f"/fo/frame/calls?cf_ctx={context_token(customer_id=ws.customer_id, surface='nav', user_id=3)}")
        page = r.content.decode()
        assert "Summit Growth Partners IV" in page and "Choose the Campfire vendor" in page
        token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
        post = lambda data: client.post("/fo/frame/calls", {"fo_t": token, **data}).content.decode()  # noqa: E731

        assert "Matched 2 funds" in post({"action": "match_vendors"})
        assert "awaiting approval in Campfire" in post({"action": "send", "call": the_call().pk})
        assert the_call().status == S.SUBMITTED

        summit = the_call().commitment
        before = summarize(summit, TODAY)
        out = post({"action": "distribution", "commitment": summit.pk, "date": TODAY.isoformat(), "roc": "100,000", "recallable": "40000", "income": "10000"})
        assert "Recorded the Summit Growth Partners IV" in out
        after = summarize(summit, TODAY)
        assert after.distributed == before.distributed + Decimal("110000")
        assert after.recallable == before.recallable + Decimal("40000")
        assert "can&#x27;t exceed" in post({"action": "distribution", "commitment": summit.pk, "date": TODAY.isoformat(), "roc": "1", "recallable": "2"})
