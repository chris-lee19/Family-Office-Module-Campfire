"""Statement of Cash Receipts and Disbursements: every entry that moved cash, read by what was on its other side."""

from datetime import date
from decimal import Decimal as D
from typing import Any
from unittest import mock

from campfire_integration.models import Workspace
from gl.services import receipts
from gl.services import statements as st

ENTITIES = [{"id": 1, "name": "Alder Family (Consolidated)", "parent": None, "active": True},
            {"id": 2, "name": "Alder Real Estate Holdings LLC", "parent": 1, "active": True}]
CASH = {"2026-08-01": 1000.0, "2026-09-01": 1225.0, "2026-10-01": 1245.0}  # the cash accounts at each month end


def line(pk: int, journal: int, when: str, account: int, name: str, kind: str, subtype: str, debit: float = 0, credit: float = 0, **more: Any) -> dict[str, Any]:
    return {"id": pk, "journal": journal, "posted_at": when, "account": account, "account_name": name, "account_number": name.split(" - ")[0],
            "account_type": kind, "account_subtype": subtype, "debit_amount": debit or None, "credit_amount": credit or None, "journal_memo": "", **more}


BANK = (11, "1040 - Operating cash", "ASSET", "BANK")
LEDGER = [
    # Rent came in.
    line(1, 1, "2026-09-10", *BANK, debit=500), line(2, 1, "2026-09-10", 41, "7400 - Rental income", "OTHER_INCOME", "OTHER_INCOME", credit=500),
    # A mortgage payment: interest and principal. The same entry expenses insurance out of a prepaid balance: no cash in that.
    line(3, 2, "2026-09-15", *BANK, credit=300), line(4, 2, "2026-09-15", 82, "8200 - Interest expense", "OTHER_EXPENSE", "OTHER_EXPENSE", debit=200),
    line(5, 2, "2026-09-15", 26, "2600 - Mortgage payable", "LIABILITY", "LONG_TERM_LIABILITY", debit=100),
    line(6, 2, "2026-09-15", 65, "6550 - Insurance expense", "OPERATING_EXPENSES", "EXPENSE", debit=40),
    line(7, 2, "2026-09-15", 12, "1260 - Prepaid insurance", "ASSET", "PREPAID", credit=40),
    # An entry that moved no cash isn't in the statement.
    line(8, 3, "2026-09-30", 16, "1630 - Unrealized appreciation", "ASSET", "OTHER_ASSET", debit=999),
    line(9, 3, "2026-09-30", 73, "7300 - Unrealized gain", "OTHER_INCOME", "OTHER_INCOME", credit=999),
    # Cash that an opening entry put on the books is neither a receipt nor a disbursement.
    {**line(10, 4, "2026-09-01", *BANK, debit=25), "journal_memo": "Opening balances"},
    {**line(11, 4, "2026-09-01", 39, "3900 - Equity", "EQUITY", "EQUITY", credit=25), "journal_memo": "Opening balances"},
    # A bill was paid: it is shown under what the bill was for (fees and a piece of art), not under accounts payable.
    line(12, 5, "2026-10-05", *BANK, credit=120),
    line(13, 5, "2026-10-05", 21, "210000 - Accounts Payable", "LIABILITY", "ACCOUNTS_PAYABLE", debit=120, bill={"id": 77, "bill_number": "B-1"}),
    # Money sent to a related entity, and money through the investment clearing account: transfers.
    line(14, 6, "2026-10-08", *BANK, credit=50), line(15, 6, "2026-10-08", 15, "1500 - Due from related parties", "ASSET", "OTHER_CURRENT_ASSET", debit=50),
    line(16, 7, "2026-10-28", *BANK, debit=10), line(17, 7, "2026-10-28", 99, "1190 - Investment cash clearing", "ASSET", "OTHER_CURRENT_ASSET", credit=10),
    # A deposit from the bank feed that nobody has categorized.
    line(18, 8, "2026-10-12", *BANK, debit=70), line(19, 8, "2026-10-12", 1, "000000 - Uncategorized", "UNCATEGORIZED", "UNCATEGORIZED", credit=70),
    # An investment sold above cost: the cost comes out of the asset, the gain is income.
    line(20, 9, "2026-10-20", *BANK, debit=260), line(21, 9, "2026-10-20", 150, "1501 - Investments at cost", "ASSET", "OTHER_ASSET", credit=200),
    line(22, 9, "2026-10-20", 72, "7200 - Realized gain", "OTHER_INCOME", "OTHER_INCOME", credit=60),
    # A distribution to the owners.
    line(23, 10, "2026-10-25", *BANK, credit=150), line(24, 10, "2026-10-25", 394, "3940 - Distributions to owners", "EQUITY", "EQUITY", debit=150),
]
BILLS = {"results": [{"id": 77, "lines": [{"account": 51, "account_number": "6510", "account_name": "6510 - Trustee fees", "amount": 90.0},
                                           {"account": 17, "account_number": "1700", "account_name": "1700 - Art and collectibles", "amount": 30.0}]}], "next": None}
ACCOUNTS = [{"id": 51, "account_type": "OPERATING_EXPENSES"}, {"id": 17, "account_type": "ASSET"}]


class FakeCampfire:
    def __init__(self, cash: dict[str, float] | None = None, count: int | None = None) -> None:
        self.cash, self.count = cash or CASH, count
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_json(self, path: str, *, params: Any = None) -> Any:
        p = dict(params or {})
        self.calls.append((path, p))
        if path == st.BALANCE_SHEET_PATH:
            first, last = p["start_date"][:8] + "01", p["end_date"]
            keys = [k for k in self.cash if first <= k <= last]
            return {"headers": {k: ["Total"] for k in keys}, "balance_sheet": [
                {"account_name": "Operating cash", "account_subtype": "BANK", "hierarchy": ["ASSET", "11"], **{f"{k}_Total": self.cash[k] for k in keys}},
                {"account_name": "Real estate", "account_subtype": "FIXED_ASSET", "hierarchy": ["ASSET", "12"], **{f"{k}_Total": 900.0 for k in keys}},
                {"account_name": "ASSET", "hierarchy": ["ASSET"], **{f"{k}_Total": self.cash[k] + 900.0 for k in keys}}]}
        if path == st.LEDGER_PATH:
            rows = [t for t in LEDGER if p["start_date"] <= t["posted_at"] <= p["end_date"]]
            return {"count": self.count or len(rows), "next": None, "results": rows}
        if path.startswith(receipts.BILL_PATH + "/"):  # a bill read on its own: this one has no lines to trace
            return {"id": 77, "lines": []}
        return {st.ENTITY_PATH: ENTITIES, receipts.BILL_PATH: BILLS, receipts.ACCOUNT_PATH: ACCOUNTS}[path]


SCOPE = st.Scope(2, False, "Alder Real Estate Holdings LLC", (2,))


def test_receipts_and_disbursements_add_up_to_the_change_in_cash() -> None:
    cf = FakeCampfire()
    s = receipts.build(cf, SCOPE, date(2026, 9, 1), date(2026, 10, 31), "monthly", frozenset({99}))  # type: ignore[arg-type]
    assert s.title == "Statement of Cash Receipts and Disbursements" and s.columns == ["Sep 2026", "Oct 2026", "Total"]
    assert [(r.kind, r.level, r.label, r.values) for r in s.rows] == [
        ("section", 0, "Receipts", [None, None, None]),
        ("group", 0, "Income", [D("500.00"), D("60.00"), D("560.00")]),
        ("account", 2, "Realized gain", [D(0), D("60.00"), D("60.00")]),
        ("account", 2, "Rental income", [D("500.00"), D(0), D("500.00")]),
        ("group", 0, "From investments and other assets", [D(0), D("200.00"), D("200.00")]),
        ("account", 2, "Investments at cost", [D(0), D("200.00"), D("200.00")]),
        ("group", 0, "Bank lines not categorized yet", [D(0), D("70.00"), D("70.00")]),
        ("total", 0, "Total receipts", [D("500.00"), D("330.00"), D("830.00")]),
        ("section", 0, "Disbursements", [None, None, None]),
        ("group", 0, "Expenses", [D("200.00"), D("90.00"), D("290.00")]),
        ("account", 2, "Trustee fees", [D(0), D("90.00"), D("90.00")]),  # from the bill that was paid
        ("account", 2, "Interest expense", [D("200.00"), D(0), D("200.00")]),  # the insurance out of prepaid isn't cash: left out
        ("group", 0, "Into investments and other assets", [D(0), D("30.00"), D("30.00")]),
        ("account", 2, "Art and collectibles", [D(0), D("30.00"), D("30.00")]),
        ("group", 0, "Repaid on loans and other liabilities", [D("100.00"), D(0), D("100.00")]),
        ("account", 2, "Mortgage payable", [D("100.00"), D(0), D("100.00")]),
        ("group", 0, "Distributions", [D(0), D("150.00"), D("150.00")]),
        ("account", 2, "Distributions to owners", [D(0), D("150.00"), D("150.00")]),
        ("total", 0, "Total disbursements", [D("300.00"), D("270.00"), D("570.00")]),
        ("total", 0, "Excess of receipts over disbursements", [D("200.00"), D("60.00"), D("260.00")]),
        ("section", 0, "Transfers and other changes", [None, None, None]),
        ("account", 1, "Investment cash clearing", [D(0), D("10.00"), D("10.00")]),
        ("account", 1, "Due from related parties", [D(0), D("-50.00"), D("-50.00")]),
        ("account", 1, "Opening balances brought onto the books", [D("25.00"), D(0), D("25.00")]),
        ("total", 0, "Net change in cash", [D("225.00"), D("20.00"), D("245.00")]),
        ("account", 0, "Cash at the start", [D("1000.00"), D("1225.00"), D("1000.00")]),
        ("grand", 0, "Cash at the end", [D("1225.00"), D("1245.00"), D("1245.00")])]
    by = {r.label: r for r in s.rows}
    assert by["Rental income"].account_id == 41 and by["Rental income"].number == "7400" and by["Bank lines not categorized yet"].account_id == 1
    assert "Cash basis" in s.note and "Cash page" in s.note and "under what each bill was for" in s.note and "too long" not in s.note
    assert [c[0] for c in cf.calls].count(receipts.ACCOUNT_PATH) == 1  # only because the bill's accounts weren't in the ledger read

    one = receipts.build(cf, SCOPE, date(2026, 9, 1), date(2026, 10, 31), "total", frozenset({99}))  # type: ignore[arg-type]
    row = {r.label: r.values for r in one.rows}
    assert one.columns == ["Total"] and row["Total receipts"] == [D("830.00")] and row["Total disbursements"] == [D("570.00")]
    assert row["Cash, Sep 1, 2026"] == [D("1000.00")] and row["Cash, Oct 31, 2026"] == [D("1245.00")] and "Other changes in cash" not in row


def test_what_the_ledger_does_not_explain_is_stated_and_an_untraced_bill_stays_a_bill() -> None:
    cf = FakeCampfire({**CASH, "2026-10-01": 1250.0}, count=9000)  # 5 more cash than the entries explain; a ledger too long to read
    with mock.patch.dict(BILLS, {"results": []}):
        s = receipts.build(cf, SCOPE, date(2026, 10, 1), date(2026, 10, 31), "total")  # type: ignore[arg-type]
    row = {r.label: r.values[0] for r in s.rows if r.values[0] is not None}
    assert row["Bills paid"] == row["Accounts Payable"] == D("120.00") and "Trustee fees" not in row
    # Without the clearing accounts named, the clearing line is money out of an asset; the related entity is still a transfer.
    assert row["Investment cash clearing"] == D("10.00") and row["From investments and other assets"] == D("210.00")
    assert row["Other changes in cash"] == D("5.00") and row["Net change in cash"] == D("25.00") and row["Cash, Oct 31, 2026"] == D("1250.00")
    assert "couldn't be traced" in s.note and "too long to read in full" in s.note


def test_receipts_tab_on_the_statements_page(client, workspace: Workspace) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import unquote

    from tests.conftest import context_token

    with mock.patch("gl.statement_views.CampfireClient", return_value=FakeCampfire()):
        first = client.get(f"/fo/frame/statements?cf_ctx={context_token(customer_id=workspace.customer_id, surface='nav', user_id=3)}").content.decode()
        token = unquote(first.split('name="fo_t" value="')[1].split('"')[0])
        assert "Statement of Cash Receipts and Disbursements" in first and "view=rd" in first  # a tab beside the other statements
        page = client.get("/fo/frame/statements", {"fo_t": token, "view": "rd", "entity": "2", "period": "custom", "start": "2026-09-01", "end": "2026-10-31"}).content.decode()
        assert "Total receipts" in page and "Excess of receipts over disbursements" in page and "Cash, Oct 31, 2026" in page and 'class="st-group' in page
        head = page.split('class="print-only print-head"')[1].split("</header>")[0]
        assert "<h1>Statement of Cash Receipts and Disbursements</h1>" in head and "Cash basis · September 1, 2026 to October 31, 2026" in head
        kpis = page.split('class="kpis no-print"')[1].split("</div>\n")[0]
        assert "Cash, Oct 31, 2026" in kpis and "Receipts" in kpis and "Disbursements" in kpis and "1,245" in kpis and "840" in kpis  # no clearing account is mapped here, so its 10 is a receipt
        assert "/fo/frame/statements/account/41?" in page and "Figures come from" not in page  # its own note says where the figures come from
        sheet = client.get("/fo/frame/statements", {"fo_t": token, "view": "rd", "entity": "2", "period": "custom", "start": "2026-09-01", "end": "2026-10-31",
                                                    "cols": "monthly", "format": "csv"}).content.decode()
        assert "7400,Rental income,500.00,0.00,500.00" in sheet and ",Cash at the end,1225.00,1245.00,1245.00" in sheet
