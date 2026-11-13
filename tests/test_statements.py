"""Financial statements: Campfire's figures in the app's reading order, with the ledger behind an account."""

from datetime import date
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest

from campfire_integration.models import Workspace
from gl.services import statements as st
from gl.services.setup import SUGGESTED_ACCOUNTS  # noqa: F401  (keeps the app registry warm for the page test)

ENTITIES = [{"id": 1, "name": "Alder Family (Consolidated)", "parent": None, "active": True},
            {"id": 2, "name": "Alder Real Estate Holdings LLC", "parent": 1, "active": True},
            {"id": 3, "name": "Alder Family - Eliminations", "parent": 1, "active": True, "is_elimination_entity": True},
            {"id": 9, "name": "Entity 1 (USD)", "parent": None, "active": False}]
LE = "LIABILITIES_AND_EQUITY"
BS = {"headers": {"2026-10-01": ["Total"]}, "balance_sheet": [
    {"account_number": "1600", "account_name": "Real estate", "hierarchy": ["ASSET", "12"], "2026-10-01_Total": 900.0},
    {"account_number": "1040", "account_name": "Operating cash", "hierarchy": ["ASSET", "11"], "2026-10-01_Total": 100.0},
    {"account_name": "ASSET", "hierarchy": ["ASSET"], "2026-10-01_Total": 1000.0},
    {"account_number": "2600", "account_name": "Mortgage payable", "hierarchy": [LE, "LIABILITY", "21"], "2026-10-01_Total": 400.0},
    {"account_name": "LIABILITY", "hierarchy": [LE, "LIABILITY"], "2026-10-01_Total": 400.0},
    {"account_number": "3920", "account_name": "Members' capital", "hierarchy": [LE, "EQUITY", "31"], "2026-10-01_Total": 550.0},
    {"account_name": "NET INCOME", "hierarchy": [LE, "EQUITY", "NET_INCOME"], "2026-10-01_Total": 50.0},
    {"account_name": "RETAINED EARNINGS", "hierarchy": [LE, "EQUITY", "RETAINED_EARNINGS"], "2026-10-01_Total": 0.0},
    {"account_name": "EQUITY", "hierarchy": [LE, "EQUITY"], "2026-10-01_Total": 600.0},
    {"account_name": "LIABILITIES AND EQUITY", "hierarchy": [LE], "2026-10-01_Total": 1000.0},
    {"account_name": "UNCATEGORIZED", "hierarchy": ["UNCATEGORIZED_TOTAL"]}]}
IS = {"headers": {"2026-09-01": ["Total"], "2026-10-01": ["Total"], "Total": ["Total"]}, "income_statement": [
    {"display_name": "Revenue", "hierarchy": ["REVENUE"], "2026-09-01_Total": 0.0, "2026-10-01_Total": 0.0, "Total": 0.0},
    {"display_name": "Other Income", "hierarchy": ["OTHER_INCOME"], "2026-09-01_Total": 62.0, "2026-10-01_Total": 62.0, "Total": 124.0},
    {"account_number": "7400", "account_name": "Rental income", "hierarchy": ["OTHER_INCOME", "41"], "2026-09-01_Total": 62.0, "2026-10-01_Total": 62.0, "Total": 124.0},
    {"display_name": "Operating Expenses", "hierarchy": ["OPERATING_EXPENSES"], "2026-09-01_Total": 3.0, "2026-10-01_Total": 44.5, "Total": 47.5},
    {"account_number": "6560", "account_name": "Property taxes and maintenance", "hierarchy": ["OPERATING_EXPENSES", "51"], "2026-09-01_Total": 3.0, "2026-10-01_Total": 44.5, "Total": 47.5},
    {"account_number": "6999", "account_name": "Never used", "hierarchy": ["OPERATING_EXPENSES", "52"], "2026-09-01_Total": 0.0, "2026-10-01_Total": 0.0, "Total": 0.0},
    {"display_name": "Net Income", "hierarchy": ["NET_INCOME"], "2026-09-01_Total": 59.0, "2026-10-01_Total": 17.5, "Total": 76.5}]}
TB = {"trial_balance": {"accounts": [
    {"id": "11", "number": "1040", "name": "Operating cash", "beginning_balances": {"debits": 80, "credits": 0}, "activity": {"debits": 62, "credits": 42},
     "ending_balances": {"debits": 100, "credits": 0}},
    {"id": "41", "number": "7400", "name": "Rental income", "beginning_balances": {}, "activity": {"debits": 0, "credits": 62}, "ending_balances": {"debits": 0, "credits": 62}},
    {"id": "total", "name": "Total", "activity": {"debits": 62, "credits": 104}, "ending_balances": {"debits": 100, "credits": 62}}]}}
LEDGER = {"count": 2, "next": None, "results": [
    {"posted_at": "2026-10-31", "account_name": "7400 - Rental income", "journal_memo": "Operating activity, October 2026", "bank_description": "Rent",
     "journal_order": "0000454", "journal_type_name": "Journal Entry", "credit_amount": 62.0, "balance_after_transaction": 124.0, "entity_name": "ARE"},
    {"posted_at": "2026-09-30", "account_name": "7400 - Rental income", "journal_memo": "Operating activity, September 2026", "bank_description": "Rent",
     "journal_order": "0000390", "journal_type_name": "Journal Entry", "credit_amount": 62.0, "balance_after_transaction": 62.0, "entity_name": "ARE"}]}


class FakeCampfire:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_json(self, path: str, *, params: Any = None) -> Any:
        self.calls.append((path, dict(params or {})))
        return {st.ENTITY_PATH: ENTITIES, st.BALANCE_SHEET_PATH: BS, st.INCOME_STATEMENT_PATH: IS, st.TRIAL_BALANCE_PATH: TB, st.LEDGER_PATH: LEDGER}[path]


def test_scopes_and_periods() -> None:
    options = st.scopes(FakeCampfire())  # type: ignore[arg-type]
    assert [(s.value, s.label) for s in options] == [("1r", "Alder Family (Consolidated): all entities"), ("2", "Alder Real Estate Holdings LLC"),
                                                     ("3", "Alder Family - Eliminations")]  # groups first, eliminations last, inactive left out
    assert options[0].params() == {"entity": 1, "entity_rollup": "true"} and options[1].params() == {"entity": 2}
    today = date(2026, 11, 13)
    assert st.period_dates("ytd", today) == (date(2026, 1, 1), today)
    assert st.period_dates("month", today) == (date(2026, 10, 1), date(2026, 10, 31))
    assert st.period_dates("quarter", today) == (date(2026, 7, 1), date(2026, 9, 30))
    assert st.period_dates("year", today) == (date(2025, 1, 1), date(2025, 12, 31))
    assert st.period_dates("custom", today, "2026-03-01", "2026-03-31") == (date(2026, 3, 1), date(2026, 3, 31))
    assert st.period_dates("custom", today, "2026-03-31", "2026-03-01") == (date(2026, 1, 1), today)  # backwards dates: year to date


def test_balance_sheet_and_income_statement_in_reading_order() -> None:
    cf, scope = FakeCampfire(), st.Scope(2, False, "ARE")
    bs = st.balance_sheet(cf, scope, date(2026, 1, 1), date(2026, 10, 31), "total")  # type: ignore[arg-type]
    assert cf.calls[-1][1]["start_date"] == "2026-10-01"  # one column: the balances on the end date
    assert bs.columns == ["Oct 31, 2026"]
    assert [(r.kind, r.label, r.values[0]) for r in bs.rows] == [
        ("section", "Assets", None), ("account", "Operating cash", Decimal("100.00")), ("account", "Real estate", Decimal("900.00")),
        ("total", "Total assets", Decimal("1000.00")), ("section", "Liabilities", None), ("account", "Mortgage payable", Decimal("400.00")),
        ("total", "Total liabilities", Decimal("400.00")), ("section", "Equity", None), ("account", "Members' capital", Decimal("550.00")),
        ("account", "Net income, year to date", Decimal("50.00")), ("total", "Total equity", Decimal("600.00")),
        ("grand", "Total liabilities and equity", Decimal("1000.00"))]
    assert bs.rows[1].account_id == 11 and bs.rows[9].account_id is None and not bs.note

    monthly = st.income_statement(cf, scope, date(2026, 9, 1), date(2026, 10, 31), "monthly")  # type: ignore[arg-type]
    assert monthly.columns == ["Sep 2026", "Oct 2026", "Total"]
    assert [r.label for r in monthly.rows] == ["Investment and other income", "Rental income", "Total investment and other income", "Expenses",
                                               "Property taxes and maintenance", "Total expenses", "Net income"]  # empty sections and accounts left out
    assert monthly.rows[-1].values == [Decimal("59.00"), Decimal("17.50"), Decimal("76.50")]
    assert st.income_statement(cf, scope, date(2026, 9, 1), date(2026, 10, 31), "total").columns == ["Total"]  # type: ignore[arg-type]


def test_trial_balance_and_ledger() -> None:
    cf, scope = FakeCampfire(), st.Scope(2, False, "ARE")
    tb = st.trial_balance(cf, scope, date(2026, 10, 1), date(2026, 10, 31))  # type: ignore[arg-type]
    assert tb.rows[0].values == [Decimal("80.00"), Decimal("62.00"), Decimal("42.00"), Decimal("100.00"), Decimal("0.00")]
    assert tb.rows[-1].kind == "grand" and tb.rows[-1].values[0] is None
    label, lines, total = st.ledger(cf, 41, scope, date(2026, 9, 1), date(2026, 10, 31))  # type: ignore[arg-type]
    assert (label, total) == ("7400 - Rental income", 2) and [ln.posted for ln in lines] == ["2026-09-30", "2026-10-31"]
    assert lines[0].credit == Decimal("62.00") and lines[1].balance == Decimal("124.00")
    assert cf.calls[-1][1]["account"] == 41 and cf.calls[-1][1]["entity"] == 2


@pytest.mark.django_db
def test_statements_pages(client, workspace: Workspace) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import unquote

    from tests.conftest import context_token

    cf = FakeCampfire()
    with mock.patch("gl.statement_views.CampfireClient", return_value=cf):
        page = client.get(f"/fo/frame/statements?cf_ctx={context_token(customer_id=workspace.customer_id, surface='nav', user_id=3)}").content.decode()
        assert "Total liabilities and equity" in page and "Alder Family (Consolidated): all entities" in page and "1,000" in page
        assert "/fo/frame/statements/account/11?" in page and "view=is" in page and "Download CSV" in page
        token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
        income = client.get("/fo/frame/statements", {"fo_t": token, "view": "is", "entity": "2", "cols": "monthly"}).content.decode()
        assert "Net income" in income and "Oct 2026" in income and "Alder Real Estate Holdings LLC" in income
        sheet = client.get("/fo/frame/statements", {"fo_t": token, "view": "tb", "format": "csv"})
        assert sheet["Content-Type"] == "text/csv" and "1040,Operating cash,80.00,62.00,42.00,100.00,0.00" in sheet.content.decode()
        ledger = client.get("/fo/frame/statements/account/41", {"fo_t": token, "entity": "2", "start": "2026-09-01", "end": "2026-10-31"}).content.decode()
        assert "7400 - Rental income" in ledger and "Operating activity, October 2026" in ledger and "124.00" in ledger
    assert client.post("/fo/frame/statements", {"fo_t": token}).status_code == 405
