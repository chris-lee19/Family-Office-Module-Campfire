"""Budgets, Budget vs. Actual and the Lifestyle Expense Report, and the spending tags on bills, entries and bank lines."""

from datetime import date
from decimal import Decimal as D
from typing import Any
from unittest import mock

import pytest
import requests

from campfire_integration.models import Workspace
from gl.models import Budget, BudgetLine, DimensionTag, FamilyProperty
from gl.services import budget as svc
from gl.services import budget_demo, lifestyle
from gl.services import statements as st
from gl.services.setup import TAG_GROUP_PATH
from records.models import AuditEntry, ControlSettings

TODAY = date(2026, 11, 14)
ENTITIES = [{"id": 1, "name": "Alder Family (Consolidated)", "parent": None, "active": True},
            {"id": 2, "name": "Robert Alder Revocable Trust", "parent": 1, "active": True},
            {"id": 3, "name": "Alder Real Estate Holdings LLC", "parent": 1, "active": True}]
NAMES = {2: "Robert Alder Revocable Trust", 3: "Alder Real Estate Holdings LLC"}
CHART = {"results": [
    {"id": 41, "number": "7400", "name": "Rental income", "account_type": "OTHER_INCOME"},
    {"id": 64, "number": "6640", "name": "Aviation", "account_type": "OPERATING_EXPENSES"},
    {"id": 63, "number": "6630", "name": "Travel", "account_type": "OPERATING_EXPENSES"},
    {"id": 53, "number": "6530", "name": "Legal fees", "account_type": "OPERATING_EXPENSES"},
    {"id": 69, "number": "6900", "name": "Charitable grants", "account_type": "OPERATING_EXPENSES"},
    {"id": 11, "number": "1040", "name": "Operating cash", "account_type": "ASSET"}], "next": None}
M = [f"2026-{m:02d}-01" for m in range(1, 11)]  # January to October


def _is_row(account: int, kind: str, name: str, number: str, values: list[float]) -> dict[str, Any]:
    return {"account_name": name, "account_number": number, "hierarchy": [kind, str(account)], **{f"{k}_Total": v for k, v in zip(M, values)}}


# The consolidated income statement, January to October: rent on budget, aviation over, travel a little over,
# legal fees nobody budgeted, grants under.
IS = {"headers": {**{k: ["Total"] for k in M}, "Total": ["Total"]}, "income_statement": [
    {"display_name": "Other Income", "hierarchy": ["OTHER_INCOME"]},
    _is_row(41, "OTHER_INCOME", "Rental income", "7400", [62_000] * 10),
    {"display_name": "Operating Expenses", "hierarchy": ["OPERATING_EXPENSES"]},
    _is_row(64, "OPERATING_EXPENSES", "Aviation", "6640", [60_000, 0, 0, 0, 60_000, 0, 28_500, 0, 60_000, 0]),
    _is_row(63, "OPERATING_EXPENSES", "Travel", "6630", [0, 0, 12_000, 0, 0, 0, 0, 0, 21_500, 0]),
    _is_row(53, "OPERATING_EXPENSES", "Legal fees", "6530", [0, 0, 0, 0, 0, 6_200, 0, 0, 0, 0]),
    _is_row(69, "OPERATING_EXPENSES", "Charitable grants", "6900", [15_000] * 10),
    {"display_name": "Net Income", "hierarchy": ["NET_INCOME"]}]}
# The same split by the family member tag group (one column for the period).
IS_BY_MEMBER = {"headers": {"2026-01-01": ["Robert Alder", "Eleanor Alder", "Other", "Total"]}, "income_statement": [
    {"account_name": "Aviation", "account_number": "6640", "hierarchy": ["OPERATING_EXPENSES", "64"], "2026-01-01_Robert Alder": 208_500, "2026-01-01_Total": 208_500},
    {"account_name": "Travel", "account_number": "6630", "hierarchy": ["OPERATING_EXPENSES", "63"], "2026-01-01_Robert Alder": 33_500,
     "2026-01-01_Eleanor Alder": 57_500, "2026-01-01_Other": 1_000, "2026-01-01_Total": 92_000},
    {"account_name": "Charitable grants", "account_number": "6900", "hierarchy": ["OPERATING_EXPENSES", "69"], "2026-01-01_Other": 150_000, "2026-01-01_Total": 150_000},
    {"account_name": "Rental income", "account_number": "7400", "hierarchy": ["OTHER_INCOME", "41"], "2026-01-01_Other": 620_000, "2026-01-01_Total": 620_000}]}
# Last year by entity and month (for "start from the books"): rent every month, travel twice, aviation quarterly.
LAST_YEAR = {"headers": {f"2025-{m:02d}-01": ["Robert Alder Revocable Trust", "Alder Real Estate Holdings LLC", "Total"] for m in range(1, 13)}, "income_statement": [
    {"hierarchy": ["OTHER_INCOME", "41"], **{f"2025-{m:02d}-01_Alder Real Estate Holdings LLC": 60_000 for m in range(1, 13)}},
    {"hierarchy": ["OPERATING_EXPENSES", "63"], "2025-03-01_Robert Alder Revocable Trust": 10_000, "2025-09-01_Robert Alder Revocable Trust": 20_000},
    {"hierarchy": ["OPERATING_EXPENSES", "64"], **{f"2025-{m:02d}-01_Robert Alder Revocable Trust": 45_000 for m in (1, 4, 7, 10)}}]}


class FakeCampfire:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    def get_json(self, path: str, *, params: Any = None) -> Any:
        p = dict(params or {})
        if path == st.INCOME_STATEMENT_PATH:
            if p.get("group_by") == "tag":
                return IS_BY_MEMBER
            if p.get("group_by") == "entity":
                return LAST_YEAR if p["start_date"].startswith("2025") else {"headers": {}, "income_statement": []}
            return IS
        if path == TAG_GROUP_PATH:
            return {"results": [{"id": 900, "name": "FO Family Member"}, {"id": 901, "name": "FO Property"}], "next": None}
        return {st.ENTITY_PATH: ENTITIES, "/coa/api/account": CHART}[path]

    def request(self, method: str, path: str, *, json: Any = None, params: Any = None, timeout: float = 15) -> requests.Response:
        self.sent.append((f"{method} {path}", json))
        r = requests.Response()
        r.status_code, r._content = 200, b'{"id": 7001}'
        return r


def _budget(workspace: Workspace, actor_id: str = "cfu:1") -> Budget:
    """The 2026 budget: rent 744,000 even; aviation 180,000 even; travel 30,000 (Mar, Sep); grants 240,000 even."""
    lines = [(3, 41, "7400", "Rental income", True, svc.spread(D(744_000), "even")),
             (2, 64, "6640", "Aviation", False, svc.spread(D(180_000), "even")),
             (2, 63, "6630", "Travel", False, [D(0), D(0), D(10_000), *[D(0)] * 5, D(20_000), D(0), D(0), D(0)]),
             (2, 69, "6900", "Charitable grants", False, svc.spread(D(240_000), "even"))]
    return svc.new_version(workspace, 2026, "Pat", actor_id, "test", [
        {"entity_id": e, "entity": NAMES[e], "account_id": a, "number": n, "account": name, "income": inc, "months": m} for e, a, n, name, inc, m in lines])


def test_spread_and_rounding() -> None:
    assert sum(svc.spread(D(1000), "even")) == D(1000) and svc.spread(D(1000), "even")[-1] == D("83.37")
    assert svc.spread(D(1000), "quarterly") == [D(0), D(0), D("250.00"), D(0), D(0), D("250.00"), D(0), D(0), D("250.00"), D(0), D(0), D("250.00")]
    assert svc.spread(D(500), "once", 4)[3] == D(500) and sum(svc.spread(D(500), "once", 4)) == D(500)
    assert svc._round(D("12345.67")) == D("12300.00") and svc._round(D("87.4")) == D("87.00")
    assert sum(budget_demo.months(("base", 3_200, {6: 4_800}))) == D(3_200 * 12 + 4_800)


@pytest.mark.django_db
def test_versions_lines_and_approval_by_someone_else(workspace: Workspace) -> None:
    ControlSettings.objects.update_or_create(workspace=workspace, defaults={"require_second_person": True})
    b = _budget(workspace)
    assert (b.version, b.is_draft, b.lines.count()) == (1, True, 4) and svc.in_use(workspace, 2026) == b == svc.draft(workspace, 2026)
    with pytest.raises(svc.BudgetError, match="already a draft"):
        svc.new_version(workspace, 2026, "Pat", "cfu:1", "again")
    accounts = {a.id: a for a in svc.pl_accounts(FakeCampfire())}  # type: ignore[arg-type]
    assert set(accounts) == {41, 64, 63, 53, 69} and accounts[41].income  # income and expense accounts only
    line = svc.save_line(b, {"entity": "2", "account": "53", "annual": "6,000", "spread": "once", "month": "6"}, NAMES, accounts, "Pat")
    assert line.amounts[5] == D(6000) and line.total == D(6000) and not line.income
    line = svc.save_line(b, {"entity": "2", "account": "53", **{f"m{m}": "500" for m in range(1, 13)}}, NAMES, accounts, "Pat")  # replaced, month by month
    assert line.total == D(6000) and b.lines.count() == 5
    with pytest.raises(svc.BudgetError, match="Choose the entity"):
        svc.save_line(b, {"entity": "9", "account": "53", "annual": "1"}, NAMES, accounts, "Pat")
    svc.remove_line(b, line.pk, "Pat")
    with pytest.raises(svc.BudgetError, match="someone else"):
        svc.approve(b, "Pat", "cfu:1")  # who made it can't approve it
    svc.approve(b, "Sam", "cfu:2")
    b.refresh_from_db()
    assert b.status == Budget.Status.APPROVED and b.approved_by == "Sam"
    with pytest.raises(svc.BudgetError, match="doesn't change"):
        svc.save_line(b, {"entity": "2", "account": "53", "annual": "1"}, NAMES, accounts, "Pat")
    v2 = svc.new_version(workspace, 2026, "Pat", "cfu:1", "Raise travel", copy_from=b)
    assert v2.version == 2 and v2.lines.count() == 4 and svc.in_use(workspace, 2026) == b and svc.draft(workspace, 2026) == v2  # the approved one stays in use
    svc.discard(v2, "Pat")
    assert svc.budgets(workspace, 2026) == [b]
    assert AuditEntry.objects.filter(kind="budgets").count() >= 7


@pytest.mark.django_db
def test_budget_vs_actual(workspace: Workspace) -> None:
    _budget(workspace)
    c = svc.compare(workspace, FakeCampfire(), st.Scope(1, True, "Alder Family", (2, 3), st.FAMILY), 2026, 10)  # type: ignore[arg-type]
    rows = {r.label: r for r in c.income + c.expenses}
    assert [r.label for r in c.income] == ["Rental income"] and [r.label for r in c.expenses] == ["Legal fees", "Travel", "Aviation", "Charitable grants"]
    rent, aviation, travel, legal, grants = (rows[k] for k in ("Rental income", "Aviation", "Travel", "Legal fees", "Charitable grants"))
    assert (rent.ytd_actual, rent.ytd_budget, rent.ytd_better, rent.flag) == (D(620_000), D(620_000), D(0), "")
    assert (aviation.ytd_actual, aviation.ytd_budget, aviation.ytd_better, aviation.ytd_pct, aviation.flag) == (D(208_500), D(150_000), D(-58_500), D(139), "Over budget")
    assert aviation.outlook == D(208_500 + 30_000) and aviation.outlook_better == D(-58_500)  # the actual so far, then the budget for Nov and Dec
    assert (aviation.m_actual, aviation.m_budget, aviation.m_better) == (D(0), D(15_000), D(15_000))  # October alone
    assert (travel.ytd_better, travel.flag) == (D(-3_500), "Over budget")  # 3,500 over: more than 10% and $1,000
    assert (legal.ytd_budget, legal.flag) == (D(0), svc.NOT_BUDGETED)
    assert (grants.ytd_better, grants.flag) == (D(50_000), "")  # under budget is better, not flagged
    assert [r.label for r in c.flagged] == ["Aviation", "Legal fees", "Travel"]  # worst first
    assert c.total_expenses.ytd_actual == D(208_500 + 33_500 + 6_200 + 150_000) and c.net.ytd_actual == D(620_000) - c.total_expenses.ytd_actual
    # One entity: only its own lines of the budget.
    one = svc.compare(workspace, FakeCampfire(), st.Scope(3, False, "ARE", (3,)), 2026, 10)  # type: ignore[arg-type]
    assert {r.label: r.ytd_budget for r in one.income + one.expenses}["Aviation"] == D(0)
    bars = svc.chart(c)
    assert [b["label"] for b in bars] == ["Legal fees", "Travel", "Aviation", "Charitable grants"] and next(b for b in bars if b["label"] == "Aviation")["over"]
    assert max(b["w_actual"] for b in bars) == 100.0


@pytest.mark.django_db
def test_start_from_the_books_and_the_spreadsheet(workspace: Workspace) -> None:
    suggested = svc.suggest(FakeCampfire(), 2026, TODAY, D(3))  # type: ignore[arg-type]
    by = {(ln["entity"], ln["account"]): ln for ln in suggested.lines}
    assert suggested.basis == "2025 as booked, plus 3%"
    assert by[("Alder Real Estate Holdings LLC", "Rental income")]["months"] == [D(61_800)] * 12  # 60,000 plus 3%, rounded
    assert by[("Robert Alder Revocable Trust", "Travel")]["months"][2] == D(10_300) and sum(by[("Robert Alder Revocable Trust", "Travel")]["months"]) == D(30_900)
    assert by[("Robert Alder Revocable Trust", "Aviation")]["months"][3] == D(46_400)  # 45,000 plus 3%, to the hundred

    b = svc.new_version(workspace, 2026, "Pat", "cfu:1", suggested.basis, suggested.lines)
    content = svc.to_xlsx(b)
    accounts = svc.pl_accounts(FakeCampfire())  # type: ignore[arg-type]
    b.lines.all().delete()
    assert svc.from_xlsx(b, content, NAMES, accounts, "Pat") == 3
    assert sorted((ln.account_label, ln.total) for ln in b.lines.all()) == [("Aviation", D(185_600)), ("Rental income", D(741_600)), ("Travel", D(30_900))]

    from openpyxl import Workbook, load_workbook
    import io
    wb = load_workbook(io.BytesIO(content))
    wb.active.append(["No Such Entity", "6630", "Travel", *[1] * 12])
    out = io.BytesIO()
    wb.save(out)
    with pytest.raises(svc.BudgetError, match="Nothing was changed.*no entity named 'No Such Entity'"):
        svc.from_xlsx(b, out.getvalue(), NAMES, accounts, "Pat")
    assert b.lines.count() == 3  # all or nothing
    with pytest.raises(svc.BudgetError, match="isn't a spreadsheet"):
        svc.from_xlsx(b, b"not a workbook", NAMES, accounts, "Pat")
    assert Workbook  # imported for the layout check above


@pytest.mark.django_db
def test_lifestyle_expense_report(workspace: Workspace) -> None:
    from entities.demo import seed_alder

    seed_alder(workspace)
    assert lifestyle.report(workspace, FakeCampfire(), st.Scope(1, True, "Alder Family", (2, 3), st.FAMILY), date(2026, 1, 1), date(2026, 10, 31), "person") is not None
    _budget(workspace)
    life = lifestyle.report(workspace, FakeCampfire(), st.Scope(1, True, "Alder Family", (2, 3), st.FAMILY), date(2026, 1, 1), date(2026, 10, 31), "person")  # type: ignore[arg-type]
    assert life is not None and life.columns == ["Robert Alder", "Eleanor Alder", lifestyle.NOT_ASSIGNED]
    assert [(c.label, c.total) for c in life.categories] == [("Aviation", D(208_500)), ("Travel", D(92_000))]  # grants carry no member tag: not in it
    assert life.other_spending == D(150_000) and life.total == D(300_500) and life.assigned == D(299_500)
    assert life.column_totals == [D(242_000), D(57_500), D(1_000)]
    assert life.budgeted and life.budget_months == (1, 10) and [c.budget for c in life.categories] == [D(150_000), D(30_000)]
    part = lifestyle.report(workspace, FakeCampfire(), st.Scope(1, True, "Alder Family", (2, 3), st.FAMILY), date(2026, 1, 1), date(2026, 10, 14), "person")  # type: ignore[arg-type]
    assert part.budget_months == (1, 9)  # October isn't over: whole months only
    charts = lifestyle.chart(life)
    assert [r["label"] for r in charts["columns"]] == ["Robert Alder", "Eleanor Alder"] and charts["categories"][0]["label"] == "Aviation"
    # No tag group in Campfire: no report.
    cf = FakeCampfire()
    cf.get_json = lambda path, *, params=None: {"results": [], "next": None} if path == TAG_GROUP_PATH else FakeCampfire().get_json(path, params=params)  # type: ignore[method-assign]
    assert lifestyle.report(workspace, cf, st.Scope(1, True, "Alder Family"), date(2026, 1, 1), date(2026, 10, 31), "person") is None  # type: ignore[arg-type]


@pytest.mark.django_db
def test_spending_tags_on_bills_entries_and_bank_lines(workspace: Workspace) -> None:
    from gl.services import bills, cash, journals

    DimensionTag.objects.create(workspace=workspace, dimension="person", key="1", campfire_tag_id=500, label="Robert Alder")
    DimensionTag.objects.create(workspace=workspace, dimension="property", key="p1", campfire_tag_id=600, label="Seacliff Avenue residence")
    assert lifestyle.is_set_up(workspace) and lifestyle.labels(workspace) == {500: "Robert Alder", 600: "Seacliff Avenue residence"}
    assert lifestyle.picked(workspace, {"tag_person": "500", "tag_property": "999"}) == [500]  # another workspace's tag is ignored

    req = bills.submit(workspace, {"entity": "2", "vendor": "7", "account": "63", "bill_number": "T-1", "bill_date": "2026-11-12", "due_date": "2026-12-12",
                                   "description": "Charter", "amount": "100", "tag_person": "500", "tag_property": "600"},
                       NAMES, {7: "Skyway Charter"}, {63: "6630 Travel"}, "Pat", "cfu:1")
    assert req.tag_ids == [500, 600] and bills.payload(req)["lines"][0]["tags"] == [500, 600]

    entry = journals.submit_manual(workspace, {"entity": "2", "date": "2026-10-31", "memo": "Trip", "account_1": "63", "debit_1": "100", "account_2": "11",
                                               "credit_2": "100", "tag_person": "500"}, NAMES, {63: "6630 Travel", 11: "1040 Operating cash"}, "Pat", "cfu:1",
                                   frozenset({63}))
    sent = journals.payload(entry, 0)["transactions"]
    assert entry.tag_ids == [500] and [t["tags"] for t in sent] == [[500], []]  # the travel line, not the cash line

    cf = FakeCampfire()
    cash.categorize(cf, 12345, 63, None, [500, 600])  # type: ignore[arg-type]
    assert cf.sent == [("PATCH /coa/api/transaction/12345", {"account": 63, "tags": [{"id": 500}, {"id": 600}]})]


@pytest.mark.django_db
def test_demo_budget_and_properties(workspace: Workspace) -> None:
    from entities.demo import seed_alder

    assert not budget_demo.needs_demo_budget(workspace, 2026)  # not the demo family
    seed_alder(workspace)
    assert budget_demo.needs_demo_budget(workspace, 2026)
    names = {e["name"]: e["id"] for e in ENTITIES}
    cf = FakeCampfire()
    with mock.patch.object(st, "scopes", return_value=[st.Scope(i, False, n) for n, i in names.items() if i != 1]):
        b = budget_demo.create(workspace, cf, 2026, "Pat", "cfu:1")  # type: ignore[arg-type]
    # Only the plan lines whose entity and account are in this (small) chart: Robert's aviation and travel, the rent.
    assert sorted((ln.entity_label, ln.account_label, ln.total) for ln in b.lines.all()) == [
        ("Alder Real Estate Holdings LLC", "Rental income", D(744_000)), ("Robert Alder Revocable Trust", "Aviation", D(180_000)),
        ("Robert Alder Revocable Trust", "Travel", D(40_000))]
    assert b.is_draft and list(FamilyProperty.objects.values_list("name", flat=True)) == ["Lakeview cottage", "Seacliff Avenue residence"]
    assert not budget_demo.needs_demo_budget(workspace, 2026)


@pytest.mark.django_db
def test_budget_page(client, workspace: Workspace) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import unquote

    from tests.conftest import context_token

    cf = FakeCampfire()
    with mock.patch("gl.budget_views.CampfireClient", return_value=cf), mock.patch("gl.budget_views.timezone.localdate", return_value=TODAY):
        page = client.get(f"/fo/frame/budget?cf_ctx={context_token(customer_id=workspace.customer_id, surface='nav', user_id=3)}").content.decode()
        token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
        assert "No budget for 2026 yet" in page and "Lifestyle Expense Report" in page and '<option value="10" selected' in page  # through the last month end
        start = client.post("/fo/frame/budget", {"fo_t": token, "view": "budget", "year": "2026", "action": "start", "how": "books", "uplift": "0"}).content.decode()
        assert "Started the 2026 budget from 2025 as booked: 3 lines" in start and "Approve version 1" in start and "Rental income" in start
        b = Budget.objects.get(workspace=workspace)
        line = client.post("/fo/frame/budget", {"fo_t": token, "view": "budget", "year": "2026", "action": "save_line", "budget": b.pk,
                                                 "entity": "2", "account": "53", "annual": "6000", "spread": "even"}).content.decode()
        assert "Saved Robert Alder Revocable Trust · Legal fees: 6,000 for the year." in line
        xlsx = client.get("/fo/frame/budget", {"fo_t": token, "view": "budget", "year": "2026", "format": "xlsx", "budget": b.pk})
        assert xlsx["Content-Type"].startswith("application/vnd.openxmlformats") and xlsx.content[:2] == b"PK"
        compare = client.get("/fo/frame/budget", {"fo_t": token, "year": "2026", "month": "10"}).content.decode()
        assert "Draft budget." in compare and "Spending against the budget, year to date" in compare and "Aviation" in compare and "Over budget" in compare
        assert "/fo/frame/statements/account/64?" in compare  # an account opens its ledger lines
        sheet = client.get("/fo/frame/budget", {"fo_t": token, "year": "2026", "month": "10", "format": "csv"}).content.decode()
        assert "6640,Aviation,0.00," in sheet
        life = client.get("/fo/frame/budget", {"fo_t": token, "view": "lifestyle"}).content.decode()
        assert "Set up spending tags" in life and "Jan 1, 2026 to Oct 31, 2026 (the last month end)" in life and "Robert Alder" in life
        prop = client.post("/fo/frame/budget", {"fo_t": token, "view": "lifestyle", "action": "add_property", "name": "Ski house", "kind": "vacation"}).content.decode()
        assert "Added Ski house." in prop and FamilyProperty.objects.filter(name="Ski house").exists()
    b.refresh_from_db()
    assert b.lines.count() == 4 and b.is_draft


def test_access_rules_for_the_page() -> None:
    from campfire_integration import access

    assert access.required("budget", "POST", "approve") == access.APPROVE
    assert access.required("budget", "POST", "setup_tags") == access.MANAGE
    assert access.required("budget", "POST", "save_line") == access.PREPARE
    assert BudgetLine and AuditEntry  # used above
