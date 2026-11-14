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
    {"id": "11", "number": "1040", "name": "Operating cash", "account_type": "ASSET", "beginning_balances": {"debits": 80, "credits": 0},
     "activity": {"debits": 62, "credits": 42}, "ending_balances": {"debits": 100, "credits": 0}},
    {"id": "12", "number": "1600", "name": "Real estate", "account_type": "ASSET", "beginning_balances": {"debits": 873.5, "credits": 0}, "ending_balances": {"debits": 900, "credits": 0}},
    {"id": "21", "number": "2600", "name": "Mortgage payable", "account_type": "LIABILITY", "beginning_balances": {"debits": 0, "credits": 400}, "ending_balances": {"debits": 0, "credits": 400}},
    {"id": "32", "number": "3940", "name": "Distributions to owners", "account_type": "EQUITY", "activity": {"debits": 30, "credits": 0}, "ending_balances": {"debits": 30, "credits": 0}},
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
    assert (options[0].kind, options[0].members, options[1].kind, options[1].members) == (st.FAMILY, (2, 3), st.ENTITY, (2,))
    # The name of a statement depends on whose books it is.
    assert st.NAMES[st.FAMILY]["bs"] == "Statement of Net Worth" and st.NAMES[st.FAMILY]["is"] == "Statement of Changes in Net Worth"
    assert st.NAMES[st.INVESTMENT]["bs"] == "Statement of Assets and Liabilities" and st.NAMES[st.ENTITY]["bs"] == "Entity Balance Sheet"
    assert st.NAMES[st.ENTITY]["is"] == "Statement of Income and Expenses"
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
    assert bs.columns == ["Oct 31, 2026"] and bs.title == "Entity Balance Sheet"  # one legal entity: its conventional books, at cost
    assert [(r.kind, r.label, r.values[0]) for r in bs.rows] == [
        ("section", "Assets", None), ("account", "Operating cash", Decimal("100.00")), ("account", "Real estate", Decimal("900.00")),
        ("total", "Total assets", Decimal("1000.00")), ("section", "Liabilities", None), ("account", "Mortgage payable", Decimal("400.00")),
        ("total", "Total liabilities", Decimal("400.00")), ("section", "Equity", None), ("account", "Members' capital", Decimal("550.00")),
        ("account", "Income less expenses, year to date", Decimal("50.00")), ("total", "Equity", Decimal("600.00")),
        ("grand", "Total liabilities and equity", Decimal("1000.00"))]
    assert bs.rows[1].account_id == 11 and bs.rows[9].account_id is None and not bs.note

    monthly = st.income_statement(cf, scope, date(2026, 9, 1), date(2026, 10, 31), "monthly")  # type: ignore[arg-type]
    assert monthly.columns == ["Sep 2026", "Oct 2026", "Total"]
    assert [r.label for r in monthly.rows][:7] == ["Investment and other income", "Rental income", "Total investment and other income", "Expenses",
                                                   "Property taxes and maintenance", "Total expenses", "Excess of income over expenses"]  # empty ones left out
    assert monthly.rows[6].values == [Decimal("59.00"), Decimal("17.50"), Decimal("76.50")]
    # Then the bridge to net worth, for the whole period: contributions and distributions, opening and closing net worth.
    bridge = {r.label: r.values for r in monthly.rows[7:]}
    assert monthly.title == "Statement of Income and Expenses"
    assert bridge["Distributions to owners"] == [None, None, Decimal("-30.00")] and bridge["Change in equity"] == [None, None, Decimal("46.50")]
    assert bridge["Equity, Sep 1, 2026"][-1] == Decimal("553.50") and monthly.rows[-1].label == "Equity, Oct 31, 2026"
    assert monthly.rows[-1].values[-1] == Decimal("600.00") and monthly.rows[-1].kind == "grand" and "Other changes" not in bridge
    assert st.income_statement(cf, scope, date(2026, 9, 1), date(2026, 10, 31), "total").columns == ["Total"]  # type: ignore[arg-type]


def test_bank_lines_nobody_categorized_are_a_line_so_the_statement_balances() -> None:
    """Campfire leaves them out of equity; the statement shows them, and its net worth equals assets less liabilities."""
    import copy
    waiting = copy.deepcopy(BS)
    for row in waiting["balance_sheet"]:
        if row["account_name"] in ("Operating cash", "ASSET"):
            row["2026-10-01_Total"] += 25.0  # money came in that nobody has categorized yet
    cf = FakeCampfire()
    cf.get_json = lambda path, *, params=None: waiting  # type: ignore[method-assign]
    bs = st.balance_sheet(cf, st.Scope(2, False, "ARE"), date(2026, 1, 1), date(2026, 10, 31), "total")  # type: ignore[arg-type]
    row = {r.label: r.values[0] for r in bs.rows}
    assert row["Bank lines not categorized yet"] == Decimal("25.00") and row["Equity"] == Decimal("625.00")
    assert row["Total assets"] == row["Total liabilities and equity"] == Decimal("1025.00") and "Cash page" in bs.note


def test_family_statements_at_estimated_current_value_with_estimated_taxes() -> None:
    cf, family = FakeCampfire(), st.Scope(1, True, "Alder Family", (2, 3), st.FAMILY)
    # The investment records say holdings are worth 200 more than cost at the end (120 at the start); nothing is booked.
    fair = st.FairValues(start=Decimal("120"), end=Decimal("200"), booked_accounts=frozenset(), tax_rate=Decimal("25"))
    bs = st.balance_sheet(cf, family, date(2026, 1, 1), date(2026, 10, 31), "total", fair)  # type: ignore[arg-type]
    row = {r.label: r.values[0] for r in bs.rows if r.kind != "section"}
    assert bs.title == "Statement of Net Worth"
    assert row["Investments: adjustment to estimated current value"] == Decimal("200") and row["Total assets"] == Decimal("1200.00")
    assert row["Estimated income taxes on unrealized appreciation"] == Decimal("50.00") and row["Total liabilities"] == Decimal("450.00")
    assert row["Net worth"] == Decimal("750.00") and row["Total liabilities and net worth"] == Decimal("1200.00")  # 600 + 200 - 50
    changes = st.income_statement(cf, family, date(2026, 9, 1), date(2026, 10, 31), "total", fair)  # type: ignore[arg-type]
    line = {r.label: r.values[-1] for r in changes.rows if r.kind != "section"}
    assert changes.title == "Statement of Changes in Net Worth"
    assert line["Change in unrealized appreciation not in the books"] == Decimal("80") and line["Change in estimated income taxes"] == Decimal("-20.00")
    assert line["Net worth, Sep 1, 2026"] == Decimal("643.50") and line["Net worth, Oct 31, 2026"] == Decimal("750.00")  # ties to the statement of net worth
    assert line["Change in net worth"] == Decimal("106.50")  # 76.50 income - 30 distributions + 80 - 20

    # What is already booked as unrealized appreciation isn't added twice (account 12 carries it here).
    booked = st.FairValues(start=Decimal("120"), end=Decimal("900"), booked_accounts=frozenset({12}), tax_rate=Decimal("0"))
    again = {r.label: r.values[0] for r in st.balance_sheet(cf, family, date(2026, 1, 1), date(2026, 10, 31), "total", booked).rows}  # type: ignore[arg-type]
    assert "Investments: adjustment to estimated current value" not in again and "Estimated income taxes on unrealized appreciation" not in again

    # An investment vehicle reports like an investment company: net assets at fair value, no estimate of taxes.
    vehicle = st.Scope(2, False, "Alder Family Investments LP", (2,), st.INVESTMENT)
    sal = st.balance_sheet(cf, vehicle, date(2026, 1, 1), date(2026, 10, 31), "total", fair)  # type: ignore[arg-type]
    labels = [r.label for r in sal.rows]
    assert sal.title == "Statement of Assets and Liabilities" and "Net assets" in labels and "Total liabilities and net assets" in labels
    assert "Estimated income taxes on unrealized appreciation" not in labels and sal.rows[-1].values[0] == Decimal("1200.00")
    ops = st.income_statement(cf, vehicle, date(2026, 9, 1), date(2026, 10, 31), "total", fair)  # type: ignore[arg-type]
    assert ops.title == "Statement of Operations and Changes in Net Assets" and "Net increase (decrease) from operations" in [r.label for r in ops.rows]
    assert ops.rows[-1].label == "Net assets, Oct 31, 2026" and ops.rows[-1].values[-1] == Decimal("800.00")


def test_tax_basis_and_unrealized_gain_report() -> None:
    holdings = [("Alder LP", "Summit Fund", Decimal("3800"), Decimal("4700")), ("Alder LP", "Crescent Fund", Decimal("1100"), Decimal("1000")),
                ("Alder Trust", "Index ETF", Decimal("920"), Decimal("1090"))]
    rep = st.tax_basis_report(st.Scope(1, True, "Alder Family", (2, 3), st.FAMILY), holdings, Decimal("20"))
    assert rep.title == "Tax Basis and Unrealized Gain Report" and rep.columns == ["Tax basis (cost)", "Fair value", "Unrealized gain (loss)", "Estimated tax"]
    assert [(r.kind, r.label) for r in rep.rows] == [("section", "Alder LP"), ("account", "Crescent Fund"), ("account", "Summit Fund"), ("total", "Total Alder LP"),
                                                     ("section", "Alder Trust"), ("account", "Index ETF"), ("total", "Total Alder Trust"), ("grand", "Total")]
    assert rep.rows[1].values[:3] == [Decimal("1100"), Decimal("1000"), Decimal("-100")]
    assert rep.rows[-1].values == [Decimal("5820"), Decimal("6790"), Decimal("970"), Decimal("194.00")]
    assert st.tax_basis_report(st.Scope(2, False, "x"), holdings, Decimal("0")).rows[-1].values[-1] is None


def test_trial_balance_and_ledger() -> None:
    cf, scope = FakeCampfire(), st.Scope(2, False, "ARE")
    tb = st.trial_balance(cf, scope, date(2026, 10, 1), date(2026, 10, 31))  # type: ignore[arg-type]
    assert tb.rows[0].values == [Decimal("80.00"), Decimal("62.00"), Decimal("42.00"), Decimal("100.00"), Decimal("0.00")]
    assert tb.rows[-1].kind == "grand" and tb.rows[-1].values[0] is None and len(tb.rows) == 6
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
        assert "Total liabilities and net worth" in page and "Statement of Net Worth" in page and "Alder Family (Consolidated): all entities" in page and "1,000" in page
        assert "Statement of Changes in Net Worth" in page and "Tax Basis and Unrealized Gain Report" in page and "Estimated current value (ASC 274)" in page
        assert "/fo/frame/statements/account/11?" in page and "view=is" in page and "Download CSV" in page
        # On paper: a heading that says whose statement it is, on what basis and as of when; a button to print it.
        head = page.split('class="print-only print-head"')[1].split("</header>")[0]
        assert "<h1>Statement of Net Worth</h1>" in head and "Estimated current value (ASC 274) · As of " in head and "Unaudited" in head and "data-print" in page
        token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
        income = client.get("/fo/frame/statements", {"fo_t": token, "view": "is", "entity": "2", "cols": "monthly"}).content.decode()
        assert "Excess of income over expenses" in income and "Change in equity" in income and "Oct 2026" in income and "Alder Real Estate Holdings LLC" in income
        assert "Statement of Income and Expenses" in income and "Entity Balance Sheet" in income  # a single entity gets the entity names
        rate = client.post("/fo/frame/statements", {"fo_t": token, "action": "tax_rate", "rate": "23.8", "view": "bs"}).content.decode()
        assert "23.8% of unrealized appreciation" in rate and "Statement of Net Worth" in rate
        assert "between 0 and 60" in client.post("/fo/frame/statements", {"fo_t": token, "action": "tax_rate", "rate": "90"}).content.decode()
        assert "Tax basis (cost)" in client.get("/fo/frame/statements", {"fo_t": token, "view": "tx"}).content.decode()
        sheet = client.get("/fo/frame/statements", {"fo_t": token, "view": "tb", "format": "csv"})
        assert sheet["Content-Type"] == "text/csv" and "1040,Operating cash,80.00,62.00,42.00,100.00,0.00" in sheet.content.decode()
        ledger = client.get("/fo/frame/statements/account/41", {"fo_t": token, "entity": "2", "start": "2026-09-01", "end": "2026-10-31"}).content.decode()
        assert "7400 - Rental income" in ledger and "Operating activity, October 2026" in ledger and "124.00" in ledger
    assert client.post("/fo/frame/statements", {"fo_t": token, "action": "other"}).status_code == 405
