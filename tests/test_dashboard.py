"""The executive dashboard: net worth and its trend, by entity, liquidity, bills and reconciliation, kept as a snapshot."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
from django.utils import timezone

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Entity, Family
from gl.models import AccountMapping, DashboardSnapshot, GLSettings, Role
from gl.services import cash, dashboard, reconcile, statements
from gl.services.calls import BILL_PATH
from investments.demo import seed_alder_investments

TODAY = date(2026, 11, 13)
LP, ARE = "Alder Family Investments LP", "Alder Real Estate Holdings LLC"
LE = "LIABILITIES_AND_EQUITY"
ENTITIES = [{"id": 1, "name": "Alder Family (Consolidated)", "parent": None, "active": True},
            {"id": 2, "name": ARE, "parent": 1, "active": True}, {"id": 4, "name": LP, "parent": 1, "active": True}]
# The family's books by month: nothing before September, then cash, investments at cost and a mortgage.
BOOKS = {"2026-09": (500.0, 4000.0, 1000.0), "2026-10": (600.0, 4000.0, 900.0), "2026-11": (700.0, 4000.0, 800.0)}
BILLS = {"results": [
    {"id": 900, "bill_number": "A", "vendor_name": "Seabright Property Management", "entity_name": ARE, "due_date": "2026-11-10", "total_amount": 100.0, "amount_due": 100.0,
     "payment_status": "open", "lines": []},
    {"id": 901, "bill_number": "B", "vendor_name": "Hollis & Park LLP", "entity_name": ARE, "due_date": "2026-11-18", "total_amount": 50.0, "amount_due": 50.0,
     "payment_status": "open", "lines": []}], "next": None}


def _months(start: str, end: str) -> list[str]:
    y, m = int(start[:4]), int(start[5:7])
    out = []
    while f"{y}-{m:02d}" <= end[:7]:
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


class FakeCampfire:
    def __init__(self, reconciliation: int = 0) -> None:
        self.calls = 0
        self.reconciliation = reconciliation  # answer the reconciliation list with this status (a missing permission)

    def get_json(self, path: str, *, params: Any = None) -> Any:
        self.calls += 1
        params = params or {}
        if path == statements.BALANCE_SHEET_PATH and params.get("group_by") == "entity":
            key = params["start_date"]
            col = lambda name: f"{key}_{name}"  # noqa: E731
            return {"headers": {key: [ARE, LP, "Total"]}, "balance_sheet": [
                {"account_number": "1040", "account_name": "Operating cash", "account_subtype": "BANK", "hierarchy": ["ASSET", "11"], col(ARE): 200.0, col(LP): 500.0},
                {"account_number": "1501", "account_name": "Investments at cost", "hierarchy": ["ASSET", "13"], col(LP): 4000.0},
                {"account_name": "ASSET", "hierarchy": ["ASSET"], col(ARE): 200.0, col(LP): 4500.0},
                {"account_number": "2600", "account_name": "Mortgage payable", "hierarchy": [LE, "LIABILITY", "21"], col(ARE): 800.0},
                {"account_name": "LIABILITY", "hierarchy": [LE, "LIABILITY"], col(ARE): 800.0}]}
        if path == statements.BALANCE_SHEET_PATH:
            months = _months(params["start_date"], params["end_date"])
            cell = lambda n: {f"{m}-01_Total": BOOKS.get(m, (0.0, 0.0, 0.0))[n] for m in months}  # noqa: E731
            both = {f"{m}-01_Total": sum(BOOKS.get(m, (0.0, 0.0, 0.0))[:2]) for m in months}
            return {"headers": {f"{m}-01": ["Total"] for m in months}, "balance_sheet": [
                {"account_number": "1040", "account_name": "Operating cash", "account_subtype": "BANK", "hierarchy": ["ASSET", "11"], **cell(0)},
                {"account_number": "1501", "account_name": "Investments at cost", "hierarchy": ["ASSET", "13"], **cell(1)},
                {"account_name": "ASSET", "hierarchy": ["ASSET"], **both},
                {"account_number": "2600", "account_name": "Mortgage payable", "hierarchy": [LE, "LIABILITY", "21"], **cell(2)},
                {"account_name": "LIABILITY", "hierarchy": [LE, "LIABILITY"], **cell(2)},
                {"account_name": "EQUITY", "hierarchy": [LE, "EQUITY"], **{k: v - cell(2)[k] for k, v in both.items()}},
                {"account_name": LE, "hierarchy": [LE], **both}]}
        if path == reconcile.REPORT_PATH:
            if self.reconciliation:
                raise reconcile.CampfireError(mock.Mock(status_code=self.reconciliation, text="no", url=path, request=mock.Mock(method="GET")))
            return {"count": 0, "results": [], "next": None}
        return {statements.ENTITY_PATH: ENTITIES, BILL_PATH: BILLS, cash.LEDGER_PATH: {"count": 0, "results": [], "next": None}}[path]


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    Entity.objects.filter(family=family, name=LP).update(campfire_entity_id=4)  # the fund interests are booked in the LP
    return family


@pytest.mark.django_db
def test_net_worth_is_the_statements_figure_with_its_trend_and_entities(workspace: Workspace, alder: Family) -> None:
    from gl.services import fairvalue

    GLSettings.objects.update_or_create(workspace=workspace, defaults={"estimated_tax_rate": Decimal("20")})
    unrealized = fairvalue.unrealized_for(workspace, [4], TODAY)
    assert unrealized > 0
    nw = dashboard.net_worth(workspace, FakeCampfire(), TODAY)  # type: ignore[arg-type]
    assert nw is not None and nw["name"] == "Statement of Net Worth" and "ASC 274" in nw["basis"]
    tax = (unrealized * Decimal("0.20")).quantize(Decimal("0.01"))
    # Books: assets 4,700 less the mortgage 800; then the holdings' value above cost, less the estimated taxes on it.
    assert Decimal(nw["value"]) == Decimal("3900.00") + unrealized - tax
    assert (Decimal(nw["assets"]), Decimal(nw["liabilities"]), Decimal(nw["estimated_tax"])) == (Decimal("4700.00") + unrealized, Decimal("800.00"), tax)
    # The same figure the Statement of Net Worth shows.
    scope = next(s for s in statements.classify(workspace, statements.scopes(FakeCampfire())) if s.rollup)  # type: ignore[arg-type]
    fair = statements.FairValues(ZERO := Decimal(0), unrealized, frozenset(), Decimal("20"))
    sheet = statements.balance_sheet(FakeCampfire(), scope, TODAY.replace(day=1), TODAY, "total", fair)  # type: ignore[arg-type]
    assert next(r.values[0] for r in sheet.rows if r.label == "Net worth" and r.kind == "total") == Decimal(nw["value"]) and ZERO == 0
    # The trend starts where the books start, one point a month, ending today.
    assert [p["label"] for p in nw["trend"]] == ["Sep 2026", "Oct 2026", "Nov 13, 2026"] and nw["trend"][-1]["value"] == nw["value"]
    assert [p["x"] for p in nw["trend"]] == [16.67, 50.0, 83.33] and all(0 <= p["y"] <= 100 for p in nw["trend"])
    assert nw["month_label"] == "Oct 31" and Decimal(nw["month_change"]) == Decimal(nw["value"]) - Decimal(nw["trend"][1]["value"]) and nw["year_change"] == ""
    assert nw["line"].count(",") == 3 and nw["area"].startswith("16.67,100 ") and nw["area"].endswith(" 83.33,100")
    # By entity: books plus the value above cost of what each holds; together they are net worth before the estimated taxes.
    by = {e["name"]: Decimal(e["value"]) for e in nw["entities"]}
    assert by == {LP: Decimal("4500.00") + unrealized, ARE: Decimal("-600.00")} and sum(by.values()) == Decimal(nw["value"]) + tax
    assert nw["entities"][0]["width"] == 100.0 and nw["entities"][1]["width"] == 0.0  # a negative entity has no bar, only its figure

    # Unrealized appreciation the books already carry isn't added twice.
    AccountMapping.objects.create(workspace=workspace, role=Role.UNREALIZED, campfire_account_id=13, campfire_account_label="1501")
    booked = dashboard.net_worth(workspace, FakeCampfire(), TODAY)  # type: ignore[arg-type]
    assert Decimal(booked["assets"]) == Decimal("700.00") + unrealized  # the 4,000 on that account is replaced, not added to


@pytest.mark.django_db
def test_liquidity_bills_and_reconciliation_parts(workspace: Workspace, alder: Family) -> None:
    lq = dashboard.cash_and_liquidity(workspace, FakeCampfire(), TODAY)  # type: ignore[arg-type]
    assert Decimal(lq["operating"]) == Decimal("700.00") and len(lq["chart"]["cols"]) == 12 and lq["chart"]["cols"][0]["short"] == "Nov"
    assert lq["due_count"] >= 2 and lq["late"] == 1 and lq["coming"][0]["late"] and lq["coming"][0]["day"] == "13"  # the overdue bill counts today
    assert {e["name"] for e in lq["entities"]} >= {ARE, LP} and Decimal(lq["unfunded"]) > 0 and lq["coverage"]
    bl = dashboard.bills(FakeCampfire(), TODAY)  # type: ignore[arg-type]
    assert (bl["total"], bl["count"]) == ("150.00", 2)
    assert [(b["label"], b["amount"], b["count"], b["bad"]) for b in bl["buckets"]] == [
        ("Overdue", "100.00", 1, True), ("Due in 7 days", "50.00", 1, False), ("8 to 30 days", "0.00", 0, False), ("Later", "0.00", 0, False)]
    rc = dashboard.reconciliation(workspace, FakeCampfire(), TODAY)  # type: ignore[arg-type]
    assert (rc["total"], rc["current"], rc["behind"], rc["target"]) == (2, 0, 2, "2026-10-31")
    assert "Promote the app's new version" in dashboard.reconciliation(workspace, FakeCampfire(403), TODAY)["error"]  # type: ignore[arg-type]
    assert dashboard.compact(Decimal("22796838")) == "$22.8M" and dashboard.compact(Decimal("-463033")) == "-$463K" and dashboard.compact(Decimal("950")) == "$950"


@pytest.mark.django_db
def test_home_shows_the_snapshot_and_refreshes_it(client, workspace: Workspace, alder: Family) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import unquote

    from tests.conftest import context_token

    cf = FakeCampfire()
    with mock.patch("records.attention_views.CampfireClient", return_value=cf), mock.patch("records.attention_views.timezone.localdate", return_value=TODAY):
        page = client.get(f"/fo/frame/home?cf_ctx={context_token(customer_id=workspace.customer_id, surface='nav', user_id=3)}").content.decode()
        # The first visit reads the books and keeps them.
        assert "Where the family stands" in page and "Net worth at each month end" in page and 'class="trend-line"' in page
        for part in ("Needs a decision", "Waiting for approval", "Coming due", "Projected cash at month end", "Net worth by entity", "Investments by asset class",
                     "Cash by entity", "Private fund commitments", "Bank reconciliation", "Bills to pay", "0 of 2 accounts reconciled"):
            assert part in page, part
        snap = DashboardSnapshot.objects.get(workspace=workspace)
        # The Records page keeps its own template: the dashboard lives beside it, not in its place.
        records = client.get("/fo/frame/records", {"fo_t": unquote(page.split('name="fo_t" value="')[1].split('"')[0])}).content.decode()
        assert "<h1>Records</h1>" in records and "Onboard a family from a spreadsheet" in records and "Where the family stands" not in records
        assert snap.as_of == TODAY and set(snap.data) == {"net_worth", "liquidity", "bills", "reconciliation"} and "data-auto-refresh" not in page
        token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
        # A normal visit shows the snapshot without calling Campfire.
        before = cf.calls
        assert "Where the family stands" in client.get("/fo/frame/home", {"fo_t": token}).content.decode() and cf.calls == before
        # Once it is stale the page still opens at once, and asks for a refresh behind it.
        DashboardSnapshot.objects.update(built_at=timezone.now() - timedelta(minutes=40))
        stale = client.get("/fo/frame/home", {"fo_t": token}).content.decode()
        assert cf.calls == before and 'data-auto-refresh="/fo/frame/home?fo_t=' in stale
        r = client.post("/fo/frame/home", {"fo_t": token, "action": "refresh"}, headers={"X-Requested-With": "fetch"})
        assert r.status_code == 204 and cf.calls > before
        assert "data-auto-refresh" not in client.get("/fo/frame/home", {"fo_t": token}).content.decode()

    # Campfire unreachable: the page still opens, on the last snapshot, and a background refresh says it failed.
    def down(workspace: Workspace) -> Any:
        raise dashboard.reconcile.CampfireError(mock.Mock(status_code=503, text="down", url="x", request=mock.Mock(method="GET")))

    with mock.patch("records.attention_views._client", side_effect=down), mock.patch("records.attention_views.timezone.localdate", return_value=TODAY):
        r = client.post("/fo/frame/home", {"fo_t": token, "action": "refresh"}, headers={"X-Requested-With": "fetch"})
        assert r.status_code == 502
        shown = client.post("/fo/frame/home", {"fo_t": token, "action": "refresh"}).content.decode()
        assert "Couldn&#x27;t read the books from Campfire" in shown and "Where the family stands" in shown
        DashboardSnapshot.objects.all().delete()
        bare = client.get("/fo/frame/home", {"fo_t": token}).content.decode()  # never connected: the investment records still show
        assert "Net worth, cash and the forecast appear here once the workspace is connected" in bare and "Investments by asset class" in bare
