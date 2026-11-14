"""Liquidity and Cash Flow Forecast: cash by month from the books, what is known, what is expected, and suggestions."""

from datetime import date
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Family
from gl.models import BillRequest, PlannedCashFlow
from gl.services import cash, statements
from gl.services import liquidity as lq
from gl.services.calls import BILL_PATH
from investments.demo import seed_alder_investments
from investments.models import CapitalCall
from records.models import AuditEntry

TODAY = date(2026, 11, 13)
LP, ARE, RRT = "Alder Family Investments LP", "Alder Real Estate Holdings LLC", "Robert Alder Revocable Trust"
ENTITIES = [{"id": 1, "name": "Alder Family (Consolidated)", "parent": None, "active": True},
            {"id": 2, "name": ARE, "parent": 1, "active": True}, {"id": 3, "name": RRT, "parent": 1, "active": True}, {"id": 4, "name": LP, "parent": 1, "active": True}]
BS = {"headers": {"2026-11-01": [ARE, RRT, LP, "Total"]}, "balance_sheet": [
    {"account_name": "Operating cash - family entities", "account_subtype": "BANK", "hierarchy": ["ASSET", "11"],
     f"2026-11-01_{ARE}": 60000.0, f"2026-11-01_{RRT}": 200000.0, f"2026-11-01_{LP}": 600000.0},
    {"account_name": "Cash held at custodians", "account_subtype": "BANK", "hierarchy": ["ASSET", "12"], f"2026-11-01_{RRT}": 70000.0},
    {"account_name": "Investments at cost", "account_subtype": "OTHER_ASSET", "hierarchy": ["ASSET", "13"], f"2026-11-01_{ARE}": 8620000.0}]}
BILLS = {"results": [
    {"id": 900, "bill_number": "SPM-1", "vendor_name": "Seabright Property Management (VEN-1)", "entity_name": ARE, "due_date": "2026-11-25", "total_amount": 12400.0,
     "amount_due": 12400.0, "payment_status": "open", "lines": []},
    {"id": 901, "bill_number": "HP-1", "vendor_name": "Hollis & Park LLP", "entity_name": RRT, "due_date": "2026-11-08", "total_amount": 7250.0, "amount_due": 7250.0,
     "payment_status": "open", "lines": []},
    {"id": 902, "bill_number": "PAID", "vendor_name": "Hollis & Park LLP", "entity_name": RRT, "due_date": "2026-09-01", "total_amount": 500.0, "amount_due": 0.0,
     "payment_status": "paid", "lines": []},
    {"id": 955, "bill_number": "CALL", "vendor_name": "Summit Growth Partners", "entity_name": LP, "due_date": "2026-11-23", "total_amount": 500000.0, "amount_due": 500000.0,
     "payment_status": "open", "lines": []}], "next": None}


def _entry(journal: int, when: str, entity: str, account: int, name: str, kind: str, amount: float, memo: str = "Operating activity", **extra: Any) -> list[dict[str, Any]]:
    """An entry of cash against one account: amount > 0 is money in."""
    side = {"journal": journal, "posted_at": when, "entity_name": entity, "journal_memo": memo}
    return [{**side, "account": 11, "account_name": "1040 - Operating cash", "account_type": "ASSET", "account_subtype": "BANK",
             "debit_amount": max(amount, 0), "credit_amount": max(-amount, 0)},
            {**side, "account": account, "account_name": name, "account_type": kind, "account_subtype": None,
             "debit_amount": max(-amount, 0), "credit_amount": max(amount, 0), **extra}]


def _months(first: int, last: int, step: int = 1) -> list[str]:
    return [f"2026-{m:02d}-28" for m in range(first, last + 1, step)]


LEDGER_LINES = (
    [ln for n, d in enumerate(_months(1, 10)) for ln in _entry(100 + n, d, ARE, 41, "7400 - Rental income", "OTHER_INCOME", 62000)]
    + [ln for n, d in enumerate(_months(1, 10)) for ln in _entry(200 + n, d, ARE, 51, "6560 - Property taxes and maintenance", "OPERATING_EXPENSES", -3000 - 100 * n)]
    + [ln for n, d in enumerate(_months(1, 10, 3)) for ln in _entry(300 + n, d, RRT, 15, "1250 - Estimated tax payments", "ASSET", -40000)]
    + [ln for n, d in enumerate(_months(1, 10)) for ln in _entry(400 + n, d, ARE, 16, "1260 - Prepaid insurance", "ASSET", 1500)]
    + _entry(500, "2026-06-15", RRT, 31, "3930 - Contributions from grantors and owners", "EQUITY", 300000)  # once: not suggested
    + _entry(501, "2025-12-31", ARE, 26, "2600 - Mortgage payable", "LIABILITY", 4200000, memo="Opening balances")  # the opening entry
    + _entry(502, "2026-03-28", ARE, 21, "210000 - Accounts Payable", "LIABILITY", -900) + _entry(503, "2026-04-28", ARE, 21, "210000 - Accounts Payable", "LIABILITY", -900)
    + _entry(504, "2026-05-28", ARE, 18, "1180 - Due from related entities", "ASSET", -5000) + _entry(505, "2026-06-28", ARE, 18, "1180 - Due from related entities", "ASSET", -5000)
    + _entry(506, "2026-07-28", ARE, 99, "000000 - Uncategorized", "UNCATEGORIZED", -700) + _entry(507, "2026-08-28", ARE, 99, "000000 - Uncategorized", "UNCATEGORIZED", -700)
    # An accrual: no bank account in the entry, so no cash moved.
    + [{"journal": 600 + n, "posted_at": d, "entity_name": ARE, "journal_memo": "Accrual", "account": 52, "account_name": "6550 - Insurance expense",
        "account_type": "OPERATING_EXPENSES", "account_subtype": None, "debit_amount": 1500, "credit_amount": 0} for n, d in enumerate(_months(1, 10))])


class FakeCampfire:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_json(self, path: str, *, params: Any = None) -> Any:
        self.calls.append((path, dict(params or {})))
        return {statements.ENTITY_PATH: ENTITIES, statements.BALANCE_SHEET_PATH: BS, BILL_PATH: BILLS,
                cash.LEDGER_PATH: {"count": len(LEDGER_LINES), "results": LEDGER_LINES, "next": None}}[path]


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    return family


def _planned(**kw: Any) -> PlannedCashFlow:
    return PlannedCashFlow(**{"campfire_entity_id": 2, "entity_label": ARE, "label": "Rent", "direction": "in", "amount": Decimal("100"), **kw})


def test_dates_a_planned_item_falls_on() -> None:
    assert lq.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28) and lq.add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    assert [m.label for m in lq.months_ahead(TODAY, 3)] == ["Nov 2026", "Dec 2026", "Jan 2027"] and lq.months_ahead(TODAY, 3)[-1].end == date(2027, 1, 31)
    window = (TODAY, date(2027, 10, 31))
    monthly = _planned(frequency="monthly", start=date(2026, 1, 31))
    assert lq.occurrences(monthly, *window)[:4] == [date(2026, 11, 30), date(2026, 12, 31), date(2027, 1, 31), date(2027, 2, 28)]  # the day holds, month ends clamp
    assert len(lq.occurrences(monthly, *window)) == 12
    assert lq.occurrences(_planned(frequency="quarterly", start=date(2026, 9, 15)), *window) == [date(2026, 12, 15), date(2027, 3, 15), date(2027, 6, 15), date(2027, 9, 15)]
    assert lq.occurrences(_planned(frequency="semiannual", start=date(2026, 12, 10)), *window) == [date(2026, 12, 10), date(2027, 6, 10)]
    assert lq.occurrences(_planned(frequency="annual", start=date(2026, 4, 15)), *window) == [date(2027, 4, 15)]
    assert lq.occurrences(_planned(frequency="monthly", start=date(2026, 12, 1), end=date(2027, 1, 31)), *window) == [date(2026, 12, 1), date(2027, 1, 1)]
    assert lq.occurrences(_planned(frequency="once", start=date(2027, 2, 1)), *window) == [date(2027, 2, 1)]
    assert lq.occurrences(_planned(frequency="once", start=date(2026, 11, 1)), *window) == []  # already in the past: it isn't waiting to be paid


@pytest.mark.django_db
def test_forecast_from_cash_bills_calls_and_what_is_expected(workspace: Workspace, alder: Family) -> None:
    call = CapitalCall.objects.filter(commitment__account__family=alder, status__in=CapitalCall.OPEN_STATUSES).select_related("commitment__instrument").get()
    CapitalCall.objects.filter(pk=call.pk).update(campfire_bill_id=955)  # its bill is in Campfire: counted once, as the call
    BillRequest.objects.create(workspace=workspace, campfire_entity_id=2, entity_label=ARE, campfire_vendor_id=7, vendor_name="Harborline Insurance Brokers",
                               bill_number="HIB-1", bill_date=TODAY, due_date=date(2026, 12, 8), description="Renewal", campfire_account_id=51,
                               account_label="6550 Insurance expense", amount=Decimal("21600"))
    names = lq.entity_names(FakeCampfire())  # type: ignore[arg-type]
    assert names == {2: ARE, 3: RRT, 4: LP}
    lq.add(workspace, {"entity": "2", "label": "Mortgage payment", "direction": "out", "amount": "23,400", "frequency": "monthly", "start": "2026-11-30"}, names, "Pat")
    lq.add(workspace, {"entity": "2", "label": "Rent", "direction": "in", "amount": "62000", "frequency": "monthly", "start": "2026-12-01", "end": "2027-01-31"}, names, "Pat")

    fc = lq.build(workspace, FakeCampfire(), TODAY, horizon=3)  # type: ignore[arg-type]
    assert [m.label for m in fc.months] == ["Nov 2026", "Dec 2026", "Jan 2027"]
    # Cash is the books'. An entity whose custody cash isn't in the books yet gets it from the investment records, and the page says so.
    assert fc.operating == Decimal("860000.00") and fc.opening == fc.operating + fc.custody and fc.custody > Decimal("70000.00")
    assert any("comes from the investment records" in n for n in fc.notes) and not any(n.startswith(RRT) for n in fc.notes)
    out = {ln.label: ln.values for ln in fc.outflows}
    assert out["Bills to pay"] == [Decimal("19650.00"), 0, 0]  # the paid bill and the call's bill are left out; the overdue one counts now
    assert out["Bills waiting for approval"] == [0, Decimal("21600.00"), 0]
    assert out["Capital calls (notices received)"] == [call.amount, 0, 0] and call.due_date.month == 11
    assert out["Mortgage payment"] == [Decimal("23400.00")] * 3
    assert [(ln.label, ln.values) for ln in fc.inflows] == [("Rent", [0, Decimal("62000.00"), Decimal("62000.00")])]
    assert fc.money_out[0] == Decimal("19650.00") + call.amount + Decimal("23400.00") and fc.closing[0] == fc.opening - fc.money_out[0]
    assert fc.closing[2] == fc.opening + sum(fc.money_in) - sum(fc.money_out) and fc.starts == [fc.opening, *fc.closing[:2]]
    late = [f for f in fc.flows if f.late]
    assert [(f.label, f.when, f.kind) for f in late] == [("Hollis & Park LLP", TODAY, lq.BILL)]
    assert {f.kind for f in fc.due_soon} == {lq.BILL, lq.REQUEST, lq.CALL} and len(fc.due_soon) == 4

    # Each entity pays from its own cash: the real estate company runs short even though the family doesn't.
    by = {e.entity: e for e in fc.entities}
    are = by[ARE]
    assert are.cash == Decimal("60000.00") and are.closing == [Decimal("24200.00"), Decimal("41200.00"), Decimal("79800.00")] and not are.short
    PlannedCashFlow.objects.filter(label="Rent").delete()
    fc = lq.build(workspace, FakeCampfire(), TODAY, horizon=3)  # type: ignore[arg-type]
    are = {e.entity: e for e in fc.entities}[ARE]
    assert are.short and not are.covered and (are.low, are.low_month) == (Decimal("-44200.00"), "Jan 2027") and fc.low > 0
    assert [e.entity for e in fc.short_entities] == [ARE] and fc.entities[0].entity == ARE  # the ones in trouble first
    rrt = {e.entity: e for e in fc.entities}[RRT]
    assert rrt.custody == Decimal("70000.00") and rrt.liquid > 0  # liquid investments from the investment records

    # Liquidity beyond cash, and the commitments that haven't been called.
    tiers = dict(fc.tiers)
    assert tiers["Cash"] == fc.opening and tiers["Liquid investments"] > 0 and tiers["Illiquid investments"] > 0
    summit = next(u for u in fc.unfunded if u.fund == call.commitment.instrument.name)
    assert summit.noticed == call.amount and summit.not_called == summit.unfunded - call.amount and summit.entity == LP
    assert fc.coverage == ((fc.opening + fc.liquid) / fc.total_unfunded).quantize(Decimal("0.1"))
    assert not any(f.kind == lq.ESTIMATE for f in fc.flows)  # only notices, unless asked
    paced = lq.build(workspace, FakeCampfire(), TODAY, horizon=3, pace=12)  # type: ignore[arg-type]
    estimate = [f for f in paced.flows if f.kind == lq.ESTIMATE and f.label == summit.fund]
    assert [f.when for f in estimate] == [date(2026, 12, 31), date(2027, 1, 31)] and -estimate[0].amount == (summit.not_called / 12).quantize(Decimal("0.01"))
    assert "Capital calls (estimate from unfunded commitments)" in {ln.label for ln in paced.outflows}

    # One entity on its own.
    one = lq.build(workspace, FakeCampfire(), TODAY, horizon=3, entity_id=2)  # type: ignore[arg-type]
    assert one.entity == ARE and one.opening == Decimal("60000.00") and {f.entity for f in one.flows} == {ARE} and one.low == Decimal("-44200.00")
    assert len(one.entities) == len(fc.entities)  # the table of entities stays whole


@pytest.mark.django_db
def test_planned_items_are_checked_and_audited(workspace: Workspace) -> None:
    names = {2: ARE}
    good = {"entity": "2", "label": "Estimated taxes", "direction": "out", "amount": "$40,000", "frequency": "quarterly", "start": "2027-01-15"}
    for bad, why in (({"entity": "9"}, "Choose the entity"), ({"label": " "}, "Choose the entity"), ({"direction": "sideways"}, "money in or money out"),
                     ({"frequency": "daily"}, "how often"), ({"start": ""}, "first date"), ({"end": "2026-12-31"}, "on or after"),
                     ({"amount": "0"}, "more than zero"), ({"amount": "lots"}, "as a number")):
        with pytest.raises(lq.LiquidityError, match=why):
            lq.add(workspace, {**good, **bad}, names, "Pat")
    assert not PlannedCashFlow.objects.exists()
    item = lq.add(workspace, good, names, "Pat")
    assert (item.amount, item.entity_label, item.created_by, item.end) == (Decimal("40000.00"), ARE, "Pat", None)
    once = lq.add(workspace, {**good, "frequency": "once", "end": "2027-06-30"}, names, "Pat")
    assert once.end is None  # a single date has no last date
    other = Workspace.objects.create(customer_id=999, name="Other")
    with pytest.raises(lq.LiquidityError, match="isn't in this workspace"):
        lq.remove(other, item.pk, "Sam")
    lq.remove(workspace, item.pk, "Sam")
    assert list(PlannedCashFlow.objects.values_list("pk", flat=True)) == [once.pk]
    assert [(e.action, e.actor) for e in AuditEntry.objects.filter(kind="planned-cash-flows").order_by("id")] == [
        (AuditEntry.Action.CREATED, "Pat"), (AuditEntry.Action.CREATED, "Pat"), (AuditEntry.Action.DELETED, "Sam")]


@pytest.mark.django_db
def test_suggestions_from_what_moved_cash_in_the_books(workspace: Workspace) -> None:
    cf = FakeCampfire()
    sg = lq.suggest(workspace, cf, TODAY)  # type: ignore[arg-type]
    assert (sg.start, sg.end) == (date(2025, 11, 1), date(2026, 10, 31)) and cf.calls[-1][1]["end_date"] == TODAY.isoformat() and not sg.partial
    got = {(s.entity, s.label): s for s in sg.items}
    assert set(got) == {(ARE, "Rental income"), (ARE, "Property taxes and maintenance"), (ARE, "Prepaid insurance"), (RRT, "Estimated tax payments")}
    # Left out: the opening entry, bills paid (open bills are counted on their own), related-party balances, bank lines
    # nobody categorized, an accrual that moved no cash, and what happened only once.
    assert sg.one_offs == 1
    rent = got[(ARE, "Rental income")]
    assert (rent.direction, rent.amount, rent.frequency, rent.start, rent.entity_id, rent.account_id, rent.checked) == (
        "in", Decimal("62000.00"), "monthly", date(2026, 11, 28), 2, 41, True)
    assert "10 of the last 10 months" in rent.basis
    upkeep = got[(ARE, "Property taxes and maintenance")]
    assert (upkeep.direction, upkeep.amount) == ("out", Decimal("3450.00"))  # the average month
    taxes = got[(RRT, "Estimated tax payments")]
    assert (taxes.direction, taxes.amount, taxes.frequency, taxes.start) == ("out", Decimal("40000.00"), "quarterly", date(2027, 1, 28))
    prepaid = got[(ARE, "Prepaid insurance")]
    assert not prepaid.checked and "usually not cash" in prepaid.basis  # offered, not ticked

    # Adding one keeps where it came from, so it isn't suggested twice.
    data = {"s1-entity": "2", "s1-account": "41", "s1-label": rent.label, "s1-direction": rent.direction, "s1-amount": str(rent.amount),
            "s1-frequency": rent.frequency, "s1-start": rent.start.isoformat()}
    item = lq.add(workspace, data, {2: ARE, 3: RRT}, "Pat", prefix="s1-")
    assert item.campfire_account_id == 41
    assert (ARE, "Rental income") not in {(s.entity, s.label) for s in lq.suggest(workspace, FakeCampfire(), TODAY).items}  # type: ignore[arg-type]


@pytest.mark.django_db
def test_liquidity_page(client, workspace: Workspace, alder: Family) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import unquote

    from tests.conftest import context_token

    with mock.patch("gl.liquidity_views.CampfireClient", return_value=FakeCampfire()), mock.patch("gl.liquidity_views.timezone.localdate", return_value=TODAY):
        url = f"/fo/frame/liquidity?cf_ctx={context_token(customer_id=workspace.customer_id, surface='nav', user_id=3)}"
        page = client.get(url).content.decode()
        assert "Liquidity and Cash Flow Forecast" in page and "Operating $860,000" in page and "Projected cash at month end" in page and "Bills to pay" in page
        assert "Nothing added yet" in page and 'class="fc-col' in page and "Unfunded commitments" in page and "Past due" in page
        token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
        suggest = client.get("/fo/frame/liquidity", {"fo_t": token, "suggest": 1}).content.decode()
        assert "Suggested from the books" in suggest and 'name="s1-entity"' in suggest and "Rental income" in suggest and "usually not cash" in suggest
        post = lambda data: client.post("/fo/frame/liquidity", {"fo_t": token, **data}).content.decode()  # noqa: E731
        assert "Tick the items to add" in post({"action": "add_suggested"})
        added = post({"action": "add_suggested", "pick": ["1"], "s1-entity": "2", "s1-account": "41", "s1-label": "Rental income", "s1-direction": "in",
                      "s1-amount": "62000.00", "s1-frequency": "monthly", "s1-start": "2026-11-28"})
        assert "Added 1 recurring item from the books" in added and PlannedCashFlow.objects.get().label == "Rental income"
        wrong = post({"action": "add", "entity": "2", "label": "Distribution", "direction": "out", "amount": "", "frequency": "monthly", "start": "2026-12-01"})
        assert "Enter the amount as a number" in wrong and 'value="Distribution"' in wrong and PlannedCashFlow.objects.count() == 1
        ok = post({"action": "add", "entity": "2", "label": "Distribution", "direction": "out", "amount": "500000", "frequency": "monthly", "start": "2026-12-01"})
        assert "Added to the forecast: Distribution" in ok and "run short of cash" in ok and "Short of cash" in ok
        item = PlannedCashFlow.objects.get(label="Distribution")
        assert "Removed from the forecast: Distribution" in post({"action": "delete", "item": item.pk}) and PlannedCashFlow.objects.count() == 1
        csv = client.get("/fo/frame/liquidity", {"fo_t": token, "format": "csv", "months": 3})
        rows = csv.content.decode().splitlines()
        assert csv["Content-Type"] == "text/csv" and rows[1] == ",Nov 2026,Dec 2026,Jan 2027,Total" and rows[2].startswith("Cash at the start,")
        assert any(r.startswith("Rental income,62000.00,62000.00,62000.00,186000.00") for r in rows)
        one = client.get("/fo/frame/liquidity", {"fo_t": token, "entity": 2, "pace": 24}).content.decode()
        assert "$60,000" in one and "Assume called evenly over 24 months" in one and f"· {ARE} ·" in one
