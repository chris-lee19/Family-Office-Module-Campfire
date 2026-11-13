"""Cash: balances by entity from the books, bank lines waiting to be categorized, and categorizing one."""

import json
from datetime import date
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
import requests

from campfire_integration.models import Workspace
from gl.services import cash, statements
from records.models import AuditEntry

TODAY = date(2026, 11, 13)
ENTITIES = [{"id": 1, "name": "Alder Family (Consolidated)", "parent": None, "active": True},
            {"id": 2, "name": "Alder Real Estate Holdings LLC", "parent": 1, "active": True},
            {"id": 3, "name": "Robert Alder Revocable Trust", "parent": 1, "active": True}]
ARE, RRT = "Alder Real Estate Holdings LLC", "Robert Alder Revocable Trust"
BS = {"headers": {"2026-11-01": [ARE, RRT, "Total"]}, "balance_sheet": [
    {"account_name": "Operating cash - family entities", "account_subtype": "BANK", "hierarchy": ["ASSET", "11"], f"2026-11-01_{ARE}": 649050.0, f"2026-11-01_{RRT}": 198520.0},
    {"account_name": "Cash held at custodians", "account_subtype": "BANK", "hierarchy": ["ASSET", "12"], f"2026-11-01_{RRT}": 69500.0},
    {"account_name": "Investments at cost", "account_subtype": "OTHER_ASSET", "hierarchy": ["ASSET", "13"], f"2026-11-01_{ARE}": 8620000.0},
    {"account_name": "ASSET", "account_subtype": None, "hierarchy": ["ASSET"], f"2026-11-01_{ARE}": 9269050.0}]}
BANKS = {"results": [{"id": 71, "name": "ARE - Operating", "entity_name": ARE}, {"id": 72, "name": "RRT - Operating", "entity_name": RRT}], "next": None}
BANK_LINES = {"results": [
    {"id": 501, "account": 71, "account_name": "ARE - Operating", "entity_name": ARE, "posted_at": "2026-11-02", "amount": 62000.0, "merchant_name": "HARBOR STREET TENANTS ACH RENT"},
    {"id": 502, "account": 71, "account_name": "ARE - Operating", "entity_name": ARE, "posted_at": "2026-11-04", "amount": -3100.0, "merchant_name": "SEABRIGHT PROPERTY MGMT"},
    {"id": 503, "account": 71, "account_name": "ARE - Operating", "entity_name": ARE, "posted_at": "2026-10-04", "amount": -3100.0, "merchant_name": "SEABRIGHT PROPERTY MGMT"},
    {"id": 504, "account": 72, "account_name": "RRT - Operating", "entity_name": RRT, "posted_at": "2026-11-03", "amount": -25000.0, "merchant_name": "TRANSFER TO R ALDER PERSONAL"},
    {"id": 505, "account": 72, "account_name": "RRT - Operating", "entity_name": RRT, "posted_at": "2026-01-03", "amount": -1.0, "merchant_name": "TOO OLD"},
    {"id": 506, "account": 72, "account_name": "RRT - Operating", "entity_name": RRT, "posted_at": "2026-11-05", "amount": -9.0, "merchant_name": "EXCLUDED", "excluded": True}], "next": None}


def _gl(line: int, bank: int, account: int, name: str, kind: str = "UNCATEGORIZED") -> list[dict[str, Any]]:
    return [{"id": line, "parent_bank_transaction": bank, "account": 11, "account_name": "1040 - Operating cash", "account_subtype": "BANK", "account_type": "ASSET"},
            {"id": line + 1, "parent_bank_transaction": bank, "account": account, "account_name": name, "account_subtype": kind, "account_type": kind}]


LEDGER = {"results": _gl(9000, 501, 99, "000000 - Uncategorized") + _gl(9010, 502, 99, "000000 - Uncategorized")
          + _gl(9020, 503, 51, "6560 - Property taxes and maintenance", "OPERATING_EXPENSES") + _gl(9030, 504, 99, "000000 - Uncategorized"), "next": None}
ACCOUNTS = {"results": [{"id": 51, "number": "6560", "name": "Property taxes and maintenance", "account_type": "OPERATING_EXPENSES"},
                        {"id": 41, "number": "7400", "name": "Rental income", "account_type": "OTHER_INCOME"},
                        {"id": 11, "number": "1040", "name": "Operating cash", "account_type": "ASSET", "account_subtype": "BANK"},
                        {"id": 99, "number": "000000", "name": "Uncategorized", "account_type": "UNCATEGORIZED"}], "next": None}


class FakeCampfire:
    def __init__(self, patch_status: int = 200) -> None:
        self.patch_status = patch_status
        self.patched: list[tuple[str, Any]] = []

    def get_json(self, path: str, *, params: Any = None) -> Any:
        return {statements.ENTITY_PATH: ENTITIES, statements.BALANCE_SHEET_PATH: BS, cash.BANK_ACCOUNT_PATH: BANKS, cash.BANK_TXN_PATH: BANK_LINES,
                cash.LEDGER_PATH: LEDGER, "/coa/api/account": ACCOUNTS, "/coa/api/vendor": {"results": [{"id": 7, "name": "Seabright Property Management"}], "next": None}}[path]

    def request(self, method: str, path: str, *, json: Any = None, params: Any = None, timeout: float = 15) -> requests.Response:
        self.patched.append((f"{method} {path}", json))
        r = requests.Response()
        r.status_code = self.patch_status
        r._content = b"{}"
        return r


def test_overview_balances_lines_and_suggestions() -> None:
    c = cash.overview(FakeCampfire(), TODAY)  # type: ignore[arg-type]
    assert c.scope_label == "Alder Family (Consolidated): all entities"
    assert [(e.entity, e.operating, e.custody, e.total, e.to_categorize, e.bank_accounts) for e in c.entities] == [
        (ARE, Decimal("649050.00"), Decimal("0"), Decimal("649050.00"), 2, ["ARE - Operating"]),
        (RRT, Decimal("198520.00"), Decimal("69500.00"), Decimal("268020.00"), 1, ["RRT - Operating"])]  # investments aren't cash
    assert (c.operating, c.custody) == (Decimal("847570.00"), Decimal("69500.00"))
    assert [ln.id for ln in c.lines] == [502, 504, 501, 503]  # newest first; too old and excluded lines left out
    assert [ln.id for ln in c.waiting] == [502, 504, 501]
    done = next(ln for ln in c.lines if ln.id == 503)
    assert done.categorized and done.category == "6560 - Property taxes and maintenance"
    again = next(ln for ln in c.lines if ln.id == 502)
    assert again.gl_line_id == 9011 and again.suggestion == {"account": 51, "label": "6560 - Property taxes and maintenance"}  # same payee as last month
    assert next(ln for ln in c.lines if ln.id == 501).suggestion is None
    assert (c.money_in, c.money_out) == (Decimal("62000.00"), Decimal("31200.00"))
    assert cash.by_month(c.lines) == [("2026-10", Decimal("0"), Decimal("3100.00")), ("2026-11", Decimal("62000.00"), Decimal("28100.00"))]


def test_categorize_outcomes() -> None:
    cf = FakeCampfire()
    assert cash.categorize(cf, 9011, 51, 7) == "categorized"  # type: ignore[arg-type]
    assert cf.patched == [("PATCH /coa/api/transaction/9011", {"account": 51, "vendor": 7})]
    assert cash.categorize(FakeCampfire(202), 9011, 51) == "sent for approval"  # type: ignore[arg-type]
    with pytest.raises(cash.CashError, match="Reconnect"):
        cash.categorize(FakeCampfire(403), 9011, 51)  # type: ignore[arg-type]
    with pytest.raises(cash.CashError, match="refused"):
        cash.categorize(FakeCampfire(400), 9011, 51)  # type: ignore[arg-type]


@pytest.mark.django_db
def test_cash_page(client, workspace: Workspace) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import unquote

    from tests.conftest import context_token

    cf = FakeCampfire()
    with mock.patch("gl.cash_views.CampfireClient", return_value=cf):
        page = client.get(f"/fo/frame/cash?cf_ctx={context_token(customer_id=workspace.customer_id, surface='nav', user_id=3)}").content.decode()
        assert "$917,070" in page and "To categorize" in page and "SEABRIGHT PROPERTY MGMT" in page and "Suggested from last time" in page
        assert '<optgroup label="Expenses">' in page and "1040 Operating cash" not in page and "Uncategorized</option>" not in page
        token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
        post = lambda data: client.post("/fo/frame/cash", {"fo_t": token, "action": "categorize", **data}).content.decode()  # noqa: E731
        assert "Choose the account" in post({"line": 502, "account": 99})  # not back to Uncategorized
        assert "isn&#x27;t waiting" in post({"line": 503, "account": 51})  # already categorized
        assert not cf.patched
        out = post({"line": 502, "account": 51, "vendor": 7})
        assert "SEABRIGHT PROPERTY MGMT: categorized as 6560 Property taxes and maintenance." in out
        assert cf.patched == [("PATCH /coa/api/transaction/9011", {"account": 51, "vendor": 7})]
    entry = AuditEntry.objects.get(kind="bank-lines")
    assert entry.changes == {"category": ["Uncategorized", "6560 Property taxes and maintenance"]} and entry.object_id == "502"
    assert json.dumps(entry.changes)
