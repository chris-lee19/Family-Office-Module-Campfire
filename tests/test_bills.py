"""Bills: entered in the app, approved by a second person, sent to Campfire, and marked paid."""

import json
from datetime import date
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
import requests

from campfire_integration.models import Workspace
from gl.models import BillRequest
from gl.services import bills, statements
from gl.services.calls import BILL_PATH
from records.models import AuditEntry, ControlSettings

TODAY = date(2026, 11, 13)
S = BillRequest.Status
ENTITIES = [{"id": 1, "name": "Alder Family (Consolidated)", "parent": None, "active": True}, {"id": 2, "name": "Alder 2012 Dynasty Trust", "parent": 1, "active": True}]
ACCOUNTS = {"results": [{"id": 51, "number": "6510", "name": "Trustee fees", "account_type": "OPERATING_EXPENSES", "account_subtype": "EXPENSE"},
                        {"id": 11, "number": "1040", "name": "Operating cash", "account_type": "ASSET", "account_subtype": "BANK"},
                        {"id": 41, "number": "7400", "name": "Rental income", "account_type": "OTHER_INCOME", "account_subtype": "OTHER_INCOME"}], "next": None}
VENDORS = {"results": [{"id": 7, "name": "Northwind Trust Company"}], "next": None}
BILLS = {"results": [
    {"id": 900, "bill_number": "NTC-Q4", "vendor_name": "Northwind Trust Company (VEN-0000046)", "entity_name": "Alder 2012 Dynasty Trust", "bill_date": "2026-11-11",
     "due_date": "2026-12-11", "total_amount": 11000.0, "amount_due": 11000.0, "payment_status": "open", "message_on_bill": "Q4 trustee fee", "lines": [{"account_name": "6510 - Trustee fees"}]},
    {"id": 901, "bill_number": "BVS-1", "vendor_name": "Bayview Securities", "entity_name": "Alder Family Foundation", "bill_date": "2026-10-09", "due_date": "2026-11-08",
     "total_amount": 1150.0, "amount_due": 1150.0, "payment_status": "open", "lines": [{"description": "Custody fee"}]},
    {"id": 902, "bill_number": "OLD-1", "vendor_name": "Hollis & Park LLP", "entity_name": "Alder 2012 Dynasty Trust", "due_date": "2026-09-01", "total_amount": 500.0,
     "amount_due": 0.0, "payment_status": "paid", "lines": []},
    {"id": 903, "bill_number": "GONE", "is_deleted": True, "total_amount": 1.0, "amount_due": 1.0}], "next": None}
ENTRY = {"entity": "2", "vendor": "7", "account": "51", "bill_number": "NTC-2027-Q1", "bill_date": "2026-11-12", "due_date": "2026-12-12",
         "description": "Q1 trustee fee", "amount": "11,500"}


class FakeCampfire:
    def __init__(self, status: int = 201) -> None:
        self.status, self.sent = status, []  # type: ignore[var-annotated]

    def get_json(self, path: str, *, params: Any = None) -> Any:
        return {statements.ENTITY_PATH: ENTITIES, "/coa/api/account": ACCOUNTS, "/coa/api/vendor": VENDORS, BILL_PATH: BILLS}[path]

    def request(self, method: str, path: str, *, json: Any = None, params: Any = None, timeout: float = 15) -> requests.Response:
        self.sent.append((f"{method} {path}", json))
        r = requests.Response()
        r.status_code = self.status
        r._content = b'{"id": 7001}'
        return r


def test_bills_from_campfire_open_first_with_days() -> None:
    rows = bills.bills(FakeCampfire(), TODAY)  # type: ignore[arg-type]
    assert [(b.id, b.status, b.is_open, b.overdue, b.days) for b in rows] == [(901, "open", True, True, -5), (900, "open", True, False, 28), (902, "paid", False, False, -73)]
    assert rows[1].vendor == "Northwind Trust Company" and rows[1].account == "6510 - Trustee fees" and rows[0].description == "Custody fee"


@pytest.mark.django_db
def test_enter_approve_by_someone_else_and_send(workspace: Workspace) -> None:
    lists = ({2: "Alder 2012 Dynasty Trust"}, {7: "Northwind Trust Company"}, {51: "6510 Trustee fees"})
    with pytest.raises(bills.BillError, match="due date"):
        bills.submit(workspace, {**ENTRY, "due_date": "2026-11-01"}, *lists, "Pat", "cfu:1")
    with pytest.raises(bills.BillError, match="Choose"):
        bills.submit(workspace, {**ENTRY, "account": "41"}, *lists, "Pat", "cfu:1")  # not an account bills may use
    req = bills.submit(workspace, ENTRY, *lists, "Pat", "cfu:1")
    assert (req.status, req.amount, req.source_id) == (S.PENDING, Decimal("11500.00"), f"fo-bill-{req.pk}")
    with pytest.raises(bills.BillError, match="already entered"):
        bills.submit(workspace, ENTRY, *lists, "Pat", "cfu:1")

    cf = FakeCampfire()
    with pytest.raises(bills.BillError, match="someone else has to approve"):
        bills.approve(req, cf, "Pat", "cfu:1")  # type: ignore[arg-type]
    assert not cf.sent
    req = bills.approve(req, cf, "Rae", "cfu:2")  # type: ignore[arg-type]
    assert (req.status, req.campfire_bill_id, req.decided_by) == (S.SENT, 7001, "Rae")
    path, body = cf.sent[0]
    assert path == f"POST {BILL_PATH}" and (body["source"], body["source_id"], body["vendor"], body["entity"]) == ("Family Office", req.source_id, "7", "2")
    assert body["lines"] == [{"account": "51", "amount": 11500.0, "description": "Q1 trustee fee", "tags": []}] and body["terms"] == "net_30"
    with pytest.raises(bills.BillError, match="already been decided"):
        bills.approve(req, cf, "Rae", "cfu:2")  # type: ignore[arg-type]
    assert AuditEntry.objects.filter(kind="bills").count() == 2

    # A draft-tier connection: Campfire holds it for its own approval. A refusal leaves it waiting here.
    other = bills.submit(workspace, {**ENTRY, "bill_number": "NTC-2027-Q2"}, *lists, "Pat", "cfu:1")
    with pytest.raises(bills.BillError, match="refused"):
        bills.approve(other, FakeCampfire(400), "Rae", "cfu:2")  # type: ignore[arg-type]
    other.refresh_from_db()
    assert other.status == S.PENDING and "400" in other.error and not other.decided_by
    assert bills.approve(other, FakeCampfire(202), "Rae", "cfu:2").status == S.AWAITING  # type: ignore[arg-type]

    # A one-person office may switch the second person off; a rejection needs a reason.
    third = bills.submit(workspace, {**ENTRY, "bill_number": "NTC-2027-Q3"}, *lists, "Pat", "cfu:1")
    with pytest.raises(bills.BillError, match="Say why"):
        bills.reject(third, "Rae", "cfu:2", " ")
    settings = ControlSettings.for_workspace(workspace)
    settings.require_second_person = False
    settings.save()
    assert bills.reject(third, "Pat", "cfu:1", "Duplicate of Q2").status == S.REJECTED


def test_pay_an_open_bill() -> None:
    cf = FakeCampfire(200)
    bill = bills.bills(cf, TODAY)[1]  # type: ignore[arg-type]
    assert bills.pay(cf, bill, 11, "1040 Operating cash", TODAY, Decimal("11000.00")) == "paid"  # type: ignore[arg-type]
    path, body = cf.sent[0]
    assert path == f"POST {BILL_PATH}/900/pay" and body["empty_transactions"][0] == {
        "account_id": 11, "account_name": "1040 Operating cash", "amount": 11000.0, "payment_date": "2026-11-13", "memo": "Payment of NTC-Q4",
        "external_id": "fo-pay-900-2026-11-13"}
    assert bills.pay(cf, bill, 11, "x", TODAY, Decimal("1000")) == "part-paid"  # type: ignore[arg-type]
    with pytest.raises(bills.BillError, match="still due"):
        bills.pay(cf, bill, 11, "x", TODAY, Decimal("11000.01"))  # type: ignore[arg-type]
    with pytest.raises(bills.BillError, match="Reconnect"):
        bills.pay(FakeCampfire(403), bill, 11, "x", TODAY, Decimal("1"))  # type: ignore[arg-type]


@pytest.mark.django_db
def test_bills_page(client, workspace: Workspace) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import unquote

    from tests.conftest import context_token

    cf = FakeCampfire()
    with mock.patch("gl.bill_views.CampfireClient", return_value=cf), mock.patch("gl.bill_views.timezone.localdate", return_value=TODAY):
        ctx = lambda user: f"/fo/frame/bills-page?cf_ctx={context_token(customer_id=workspace.customer_id, surface='nav', user_id=user)}"  # noqa: E731
        page = client.get(ctx(3)).content.decode()
        assert "$12,150" in page and "5 days overdue" in page and "Northwind Trust Company" in page and "Enter a bill" in page
        assert "7400 Rental income" not in page  # income accounts aren't offered for a bill
        token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
        out = client.post("/fo/frame/bills-page", {"fo_t": token, "action": "enter", **ENTRY}).content.decode()
        assert "now waits for approval" in out and "To approve" in out and "You entered this bill" in out
        req = BillRequest.objects.get()
        assert "someone else has to approve" in client.post("/fo/frame/bills-page", {"fo_t": token, "action": "approve", "bill": req.pk}).content.decode()
        other = unquote(client.get(ctx(4)).content.decode().split('name="fo_t" value="')[1].split('"')[0])
        assert "approved and in Campfire" in client.post("/fo/frame/bills-page", {"fo_t": other, "action": "approve", "bill": req.pk}).content.decode()
        paid = client.post("/fo/frame/bills-page", {"fo_t": other, "action": "pay", "bill": 900, "account": 11, "date": "2026-11-13", "amount": "11000"}).content.decode()
        assert "Northwind Trust Company NTC-Q4: 11,000.00 paid." in paid
    assert [p for p, _ in cf.sent] == [f"POST {BILL_PATH}", f"POST {BILL_PATH}/900/pay"] and json.dumps(cf.sent[1][1])
