"""Charts and the ownership structure: the geometry is computed in Python, so it can be checked here."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Entity, Family, OwnershipInterest
from entities.services import structure
from gl.services import bills, cash, charts
from investments.demo import seed_alder_investments
from investments.models import Commitment
from investments.services.privates import paid_in_history
from investments.views import value_trend

TODAY = date(2026, 11, 13)
D = Decimal


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    return family


def test_ranked_bars_pairs_and_changes() -> None:
    rows = charts.ranked([("B", D("50")), ("A", D("200")), ("C", D("-10"))])
    assert [(r["label"], r["width"], r["share"]) for r in rows] == [("A", 100.0, 83.3), ("B", 25.0, 20.8), ("C", 0.0, -4.2)]  # a negative has no bar, only its figure
    kept = charts.ranked([("Overdue", D("5")), ("Later", D("10"))], sort=False, bad=("Overdue",))
    assert [(r["label"], r["bad"]) for r in kept] == [("Overdue", True), ("Later", False)]
    assert charts.ranked([]) == [] and charts.pct(D("1"), D("0")) == 0.0

    flow = charts.pairs([(date(2026, 9, 1), D("100"), D("400")), (date(2026, 10, 1), D("0"), D("200"))], ("Money in", "Money out"))
    assert [(c["short"], c["year"], c["ha"], c["hb"], c["net"]) for c in flow["cols"]] == [("Sep", "2026", 25.0, 100.0, D("-300")), ("Oct", "", 0.0, 50.0, D("-200"))]
    assert (flow["total_a"], flow["total_b"]) == (D("100"), D("600")) and charts.pairs([(date(2026, 9, 1), D("0"), D("0"))], ("a", "b")) == {}

    moved = charts.changes(("Opening capital", D("5000")), [("Contributions", D("0")), ("Income (loss)", D("-70")), ("Other changes", D("120"))], ("Closing capital", D("5050")))
    assert (moved["change"], moved["up"], moved["any"]) == (D("50"), True, True)
    assert [(r["label"], r["width"], r["negative"]) for r in moved["rows"]] == [("Contributions", 0.0, False), ("Income (loss)", 58.3, True), ("Other changes", 100.0, False)]


def test_trend_lines() -> None:
    label = lambda m: charts.month_label(date(2026, m, 28), m == 9)  # noqa: E731
    one = charts.trend([(label(9), [D("100")]), (label(10), [D("90")]), (label(11), [D("130")])], ["Investments"])
    assert one["single"] and one["n"] == 3 and [c["short"] for c in one["cols"]] == ["Sep", "Oct", "Nov"] and one["cols"][0]["year"] == "2026"
    xs = [float(p.split(",")[0]) for p in one["lines"][0]["points"].split()]
    ys = [float(p.split(",")[1]) for p in one["lines"][0]["points"].split()]
    assert xs == [16.67, 50.0, 83.33] and ys[2] < ys[0] < ys[1] and all(0 < y < 100 for y in ys)  # the highest value is nearest the top, with room around
    assert one["area"].startswith("16.67,100 ") and one["first"] == D("100") and not one["first_below"]  # it falls next: the label goes above
    two = charts.trend([(label(9), [D("0"), D("0")]), (label(10), [D("400"), D("50")])], ["Paid in", "Returned"], from_zero=True, reference=("Committed", D("700")))
    assert not two["single"] and two["area"] == "" and [ln["name"] for ln in two["lines"]] == ["Paid in", "Returned"]
    assert two["cols"][0]["values"][0]["y"] == 100.0 and two["reference"]["y"] < two["cols"][1]["values"][0]["y"]  # from zero; the commitment sits above
    assert charts.trend([], ["x"]) == {} and charts.trend([(label(9), [D("0")])], ["x"]) == {}


@pytest.mark.django_db
def test_ownership_structure_layout(alder: Family) -> None:
    s = structure.build(alder, TODAY)
    by = {n.name: n for n in s.nodes}
    assert s.summary == "4 people and 8 entities, 12 ownership interests"
    # People on top, each entity one row below its lowest owner.
    assert {n.layer for n in s.nodes if n.person} == {0} and by["Eleanor Alder Revocable Trust"].layer == 1 and by["Alder Management LLC"].layer == 2
    assert by["Alder Family Investments LP"].layer == 3 and by["Alder Real Estate Holdings LLC"].layer == 4 and by["Alder Family Foundation"].layer == 1
    assert by["Eleanor Alder"].initials == "EA" and by["Alder Real Estate Holdings LLC"].sub == "LLC"
    # Nothing overlaps, and everything is inside the canvas.
    for row in {n.layer for n in s.nodes}:
        xs = sorted(n.x for n in s.nodes if n.layer == row)
        assert all(b - a >= structure.NODE_W for a, b in zip(xs, xs[1:]))
    assert all(0 <= n.x and n.x + structure.NODE_W <= s.width and n.y + structure.NODE_H <= s.height for n in s.nodes)
    lp = by["Alder Family Investments LP"]
    assert sorted(k.label for k in lp.owners) == ["1%", "24.5%", "24.5%", "50%"] and sum(k.percentage for k in lp.owners) == 100
    assert all(k.path.startswith("M") and " C" in k.path for k in s.links)
    assert len({k.label_y for k in lp.owners}) == 2 and len({k.label_x for k in lp.owners}) == 4  # four owners: labels on two levels, each at its own point
    # A cycle in the data doesn't hang the drawing.
    OwnershipInterest.objects.create(owned=Entity.objects.get(name="Alder Management LLC"), owner_entity=Entity.objects.get(name="Alder Real Estate Holdings LLC"),
                                     percentage=D("1"), interest_type="member", effective_from=date(2020, 1, 1))
    assert len(structure.build(alder, TODAY).nodes) == 12
    empty = Family.objects.create(workspace=alder.workspace, name="Empty", code="empty")
    assert structure.build(empty, TODAY).nodes == []


@pytest.mark.django_db
def test_entities_page_draws_the_structure(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import context_token

    page = client.get(f"/fo/frame/entities?cf_ctx={context_token(customer_id=alder.workspace.customer_id, surface='nav', user_id=2)}").content.decode()
    assert "Ownership structure" in page and "data-structure" in page and page.count('class="st-node') == 12 and page.count(' data-to="e') == 24  # a line and a label per interest
    assert 'data-from="p' in page and "24.5%" in page and "/fo/frame/records/people/" in page and "/fo/frame/records/entities/" in page


@pytest.mark.django_db
def test_history_behind_the_investment_charts(alder: Family) -> None:
    history = paid_in_history(list(Commitment.objects.filter(account__family=alder)), TODAY)
    assert len(history) > 2 and history[-1][0] == TODAY and [d for d, _, _ in history] == sorted(d for d, _, _ in history)
    assert all(b[1] >= a[1] and b[2] >= a[2] for a, b in zip(history, history[1:]))  # cumulative: never down
    assert history[-1][1] == sum(D(str(t.amount)) for c in Commitment.objects.filter(account__family=alder)
                                 for t in c.account.transactions.filter(instrument=c.instrument, txn_type="contribution"))
    assert paid_in_history([], TODAY) == []
    trend = value_trend(alder, TODAY)
    assert trend["single"] and 2 <= trend["n"] <= 12 and trend["cols"][-1]["label"] == "Nov 13, 2026" and trend["lines"][0]["last"] > 0


def test_money_in_and_out_by_month_and_bill_ageing() -> None:
    lines = [{"posted_at": "2026-09-30", "debit_amount": 62000.0, "credit_amount": None}, {"posted_at": "2026-09-30", "debit_amount": None, "credit_amount": 3100.0},
             {"posted_at": "2026-11-02", "debit_amount": 100.0}, {"posted_at": "2026-11-03", "credit_amount": 40.0, "is_deleted": True}, {"posted_at": "2020-01-01", "debit_amount": 9.0}]

    class Fake:
        def get_json(self, path: str, *, params: Any = None) -> Any:
            assert path == cash.LEDGER_PATH and params["account_subtype"] == "BANK" and params["start_date"] == "2026-06-01"
            return {"results": lines, "next": None}

    flow = cash.flow_by_month(Fake(), TODAY)  # type: ignore[arg-type]
    assert [m for m, _, _ in flow] == [date(2026, m, 1) for m in range(6, 12)]  # six months, empty ones included
    assert flow[3] == (date(2026, 9, 1), D("62000.00"), D("3100.00")) and flow[5] == (date(2026, 11, 1), D("100.00"), 0) and flow[0][1:] == (0, 0)

    bill = lambda days, due: bills.Bill(1, "A", "Payee", "Entity", None, None, D(due), D(due), "open", "", "", "", days)  # noqa: E731
    aged = bills.ageing([bill(-3, "10"), bill(0, "20"), bill(7, "30"), bill(8, "40"), bill(31, "50"), bill(None, "5")])
    assert aged == [("Overdue", D("10"), 1), ("Due in 7 days", D("55"), 3), ("8 to 30 days", D("40"), 1), ("Later", D("50"), 1)]  # no due date counts as due now
