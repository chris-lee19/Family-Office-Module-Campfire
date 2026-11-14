"""The family reporting pack and its two new reports: net worth split by entity and by family member, and the
Investment Performance Report."""

from datetime import date
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest

from campfire_integration.client import NotConnected
from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Entity, Family
from gl.services import pack, statements
from investments.demo import needs_price_history, seed_alder_investments, seed_price_history
from investments.models import Account, Instrument, Price, Transaction
from investments.services import performance as pf
from tests import test_dashboard, test_statements

TODAY = date(2026, 11, 13)
D = Decimal
LP, ARE = test_dashboard.LP, test_dashboard.ARE


class FakeCampfire(test_dashboard.FakeCampfire):
    """The dashboard's stand-in for Campfire, plus the statements the pack reads."""

    def get_json(self, path: str, *, params: Any = None) -> Any:
        if path == statements.INCOME_STATEMENT_PATH:
            return test_statements.IS
        if path == statements.TRIAL_BALANCE_PATH:
            return test_statements.TB
        return super().get_json(path, params=params)


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    Entity.objects.filter(family=family, name=LP).update(campfire_entity_id=4)
    return family


@pytest.mark.django_db
def test_net_worth_by_entity_and_by_family_member(workspace: Workspace, alder: Family) -> None:
    from gl.services import fairvalue

    cf = FakeCampfire()
    scope = next(s for s in statements.classify(workspace, statements.scopes(cf)) if s.rollup)  # type: ignore[arg-type]
    ids = {s.label: s.entity_id for s in statements.scopes(cf) if not s.rollup}  # type: ignore[arg-type]
    gains = fairvalue.unrealized_by_entity(workspace, TODAY)
    gain = gains[4]
    tax = (gain * D("0.20")).quantize(D("0.01"))

    by_entity = statements.net_worth_split(cf, scope, TODAY, "entity", gains, frozenset(), D("20"))  # type: ignore[arg-type]
    assert by_entity.title == "Statement of Net Worth by entity" and by_entity.columns == [ARE, LP, "Total"]
    row = {r.label: r.values for r in by_entity.rows}
    assert row["Operating cash"] == [D("200.00"), D("500.00"), D("700.00")] and row["Investments at cost"] == [0, D("4000.00"), D("4000.00")]
    assert row["Investments: adjustment to estimated current value"] == [0, gain, gain]
    assert row["Total assets"] == [D("200.00"), D("4500.00") + gain, D("4700.00") + gain]
    assert row["Estimated income taxes on unrealized appreciation"] == [0, tax, tax]  # the family's estimate sits with the entity that has the gain
    assert row["Net worth"] == [D("-600.00"), D("4500.00") + gain - tax, D("3900.00") + gain - tax]
    # The Total column is the consolidated statement's figure.
    sheet = statements.balance_sheet(cf, scope, TODAY.replace(day=1), TODAY, "total", statements.FairValues(D(0), gain, frozenset(), D("20")))  # type: ignore[arg-type]
    assert next(r.values[0] for r in sheet.rows if r.label == "Net worth" and r.kind == "total") == row["Net worth"][-1]

    # By family member: each entity's column times the share that traces to each person.
    shares = statements.look_through(workspace, TODAY, ids)
    assert shares[4] == {"Claire Moreno": D("0.25"), "James Alder": D("0.25"), "Robert Alder": D("0.25"), "Eleanor Alder": D("0.25")}  # linked by id
    assert shares[2] == shares[4]  # the real estate company isn't linked: matched by its name; the LP owns all of it
    by_person = statements.net_worth_split(cf, scope, TODAY, "person", gains, frozenset(), D("20"), shares)  # type: ignore[arg-type]
    assert sorted(by_person.columns[:-1]) == ["Claire Moreno", "Eleanor Alder", "James Alder", "Robert Alder"] and by_person.columns[-1] == "Total"
    for r in by_person.rows:
        if r.values[0] is not None:
            assert sum(r.values[:-1]) == r.values[-1], r.label  # every row adds up to the consolidated figure, to the cent
    net = next(r.values for r in by_person.rows if r.label == "Net worth")
    assert net[-1] == row["Net worth"][-1] and max(net[:-1]) - min(net[:-1]) <= D("0.01")  # four equal shares
    # An entity nobody is linked to goes to its own column instead of being lost.
    lost = statements.net_worth_split(cf, scope, TODAY, "person", gains, frozenset(), D("20"), {4: shares[4]})  # type: ignore[arg-type]
    assert statements.NOT_HELD in lost.columns and next(r.values for r in lost.rows if r.label == "Net worth")[-1] == net[-1]
    assert statements._spread(D("10.00"), [D(1), D(1), D(1)]) == [D("3.34"), D("3.33"), D("3.33")] and statements._spread(D("5"), [D(0)]) == [0]


def test_performance_periods_and_breakpoints() -> None:
    assert pf.period_start("mtd", TODAY) == date(2026, 10, 31) and pf.period_start("qtd", TODAY) == date(2026, 9, 30)
    assert pf.period_start("ytd", TODAY) == date(2025, 12, 31) and pf.period_start("y1", TODAY) == date(2025, 11, 13) and pf.period_start("itd", TODAY) is None
    assert pf.period_start("y1", date(2028, 2, 29)) == date(2027, 2, 28)
    assert pf.breakpoints(date(2026, 8, 20), date(2026, 11, 13)) == [date(2026, 8, 20), date(2026, 8, 31), date(2026, 9, 30), date(2026, 10, 31), date(2026, 11, 13)]
    assert pf.breakpoints(date(2026, 10, 31), date(2026, 11, 13)) == [date(2026, 10, 31), date(2026, 11, 13)]


@pytest.mark.django_db
def test_time_weighted_return_ignores_when_money_was_added(workspace: Workspace) -> None:
    """100 shares at 10; the price goes to 11 by the end of June; 100 more are bought at 11 on June 30; the price ends
    July at 12.10. The holder doubled the position half-way, but the securities returned 10% and then 10%: 21%."""
    from investments.models import AssetClass

    family = Family.objects.create(workspace=workspace, name="Test", code="test")
    entity = Entity.objects.create(family=family, name="Test Trust", entity_type="revocable_trust", tax_classification="grantor_trust")
    ac = AssetClass.objects.create(workspace=workspace, code="eq", name="Public equity")
    etf = Instrument.objects.create(workspace=workspace, name="Index ETF", kind=Instrument.Kind.FUND, asset_class=ac, is_unitized=True)
    account = Account.objects.create(family=family, entity=entity, name="Custody", account_type=Account.AccountType.BROKERAGE)
    buy = lambda when, qty, amount: Transaction.objects.create(account=account, instrument=etf, txn_type="buy", trade_date=when, quantity=D(qty), amount=D(amount))  # noqa: E731
    Transaction.objects.create(account=account, txn_type="deposit", trade_date=date(2026, 5, 31), amount=D("3000"))
    buy(date(2026, 5, 31), 100, "1000")
    buy(date(2026, 6, 30), 100, "1100")
    for when, px in ((date(2026, 5, 31), "10"), (date(2026, 6, 30), "11"), (date(2026, 7, 31), "12.10")):
        Price.objects.create(instrument=etf, as_of=when, price=D(px))
    Transaction.objects.create(account=account, instrument=etf, txn_type="dividend", trade_date=date(2026, 7, 15), amount=D("24.20"))  # income counts as return

    report = pf.build(family, date(2026, 7, 31))
    total = report.total
    assert total is not None and total.market_value == D("2420.00") and total.since == date(2026, 5, 31)
    june = (D("2200") - D("1000") - D("1100")) / D("1000")  # the second purchase arrives on the last day: no weight in June
    july = (D("2420") - D("2200") + D("24.20")) / (D("2200") - D("24.20") * D(16) / D(31))  # the dividend left the holding mid-month
    assert june == D("0.1") and abs(total.returns["itd"] - ((1 + june) * (1 + july) - 1)) < D("0.000001") and not total.annualized
    assert abs(total.returns["mtd"] - july) < D("0.000001")
    assert total.returns["ytd"] is None and total.returns["y1"] is None  # not held for the whole period: no figure rather than a partial one
    assert [r.label for r in report.by_asset_class] == ["Public equity"] and [r.label for r in report.by_holding] == ["Index ETF"]
    assert report.growth[0][1] == 100 and report.growth[-1][1] == (100 * (1 + june) * (1 + july)).quantize(D("0.01")) and report.privates == []


@pytest.mark.django_db
def test_performance_report_and_demo_price_history(client, workspace: Workspace, alder: Family, settings) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import context_token

    report = pf.build(alder, TODAY)
    assert report.total is not None and len(report.by_asset_class) >= 2 and len(report.privates) == 2 and report.pooled_irr is not None
    assert report.private_tvpi == ((report.private_total("distributed") + report.private_total("nav")) / report.private_total("contributed")).quantize(D("0.01"))
    assert [h.name for h in report.other] == ["Harbor Street Mixed-Use Property"]
    # As first loaded the demo has one price per security: offered a history, which spreads the return over the months.
    assert needs_price_history(alder)
    before = Price.objects.count()
    added = seed_price_history(alder, TODAY)
    assert added > 20 and Price.objects.count() == before + added and seed_price_history(alder, TODAY) == 0 and not needs_price_history(alder)
    spread = pf.build(alder, TODAY)
    assert spread.total.market_value == report.total.market_value  # today's value is unchanged
    assert abs(spread.total.returns["mtd"]) < abs(report.total.returns["mtd"])  # no longer all in the latest month
    Price.objects.filter(source="demo").exclude(as_of=TODAY - (TODAY - date(2026, 11, 11))).delete()

    settings.ALLOW_DEMO_DATA = True
    with mock.patch("investments.performance_views.timezone.localdate", return_value=TODAY):
        page = client.get(f"/fo/frame/performance?cf_ctx={context_token(customer_id=workspace.customer_id, surface='nav', user_id=3)}").content.decode()
        for part in ("Investment performance", "Priced holdings: time-weighted return", "Since inception", "Private funds: IRR and multiples", "Summit Growth Partners IV",
                     "Appraised and direct holdings", "Add month-end demo prices", "Investment Performance Report", "data-print"):
            assert part in page, part
        from urllib.parse import unquote
        token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
        done = client.post("/fo/frame/performance", {"fo_t": token, "action": "demo_prices"}).content.decode()
        assert "month-end demo prices" in done and "Add month-end demo prices" not in done and "Growth of 100" in done


@pytest.mark.django_db
def test_schedule_of_investments(workspace: Workspace, alder: Family) -> None:
    sch = pack.schedule(workspace, TODAY)
    assert sch["count"] == sum(len(g["lines"]) for g in sch["groups"]) and sch["gain"] == sch["value"] - sch["cost"]
    assert [g["value"] for g in sch["groups"]] == sorted((g["value"] for g in sch["groups"]), reverse=True)
    assert sum(g["share"] for g in sch["groups"]) == pytest.approx(D("100"), abs=D("0.3"))
    real = next(g for g in sch["groups"] if g["name"] == "Real estate")
    assert [(ln.holding, ln.owner) for ln in real["lines"]] == [("Harbor Street Mixed-Use Property", ARE)] and real["lines"][0].valued.startswith("Appraisal, ")
    assert any(ln.holding == "Cash" and ln.gain == 0 for g in sch["groups"] for ln in g["lines"])


@pytest.mark.django_db
def test_pack_is_built_from_the_same_reports(workspace: Workspace, alder: Family) -> None:
    built = pack.build(workspace, FakeCampfire(), date(2026, 9, 1), TODAY, TODAY)  # type: ignore[arg-type]
    assert [s.key for s in built.sections] == list(pack.KEYS) and not [s.key for s in built.sections if s.error]
    by = {s.key: s for s in built.sections}
    assert built.period == "September 1, 2026 to November 13, 2026" and built.scope.label == "Alder Family (Consolidated): all entities"  # type: ignore[union-attr]
    net = next(r.values[0] for r in by["net_worth"].data.rows if r.label == "Net worth" and r.kind == "total")
    assert D(by["glance"].data["nw"]["value"]) == net  # the glance page, the statement and its two splits all carry one figure
    assert next(r.values[-1] for r in by["by_entity"].data.rows if r.label == "Net worth") == net
    assert next(r.values[-1] for r in by["by_person"].data.rows if r.label == "Net worth") == net
    assert by["changes"].data.title == "Statement of Changes in Net Worth" and by["schedule"].data["count"] > 5
    assert by["receipts"].data.title == "Statement of Cash Receipts and Disbursements" and by["receipts"].data.rows[-1].kind == "grand"
    assert by["performance"].data[0]["r"].total is not None and len(by["liquidity"].data["fc"].months) == 12
    assert [c.entity.name for c in by["capital"].data] == [LP] and by["structure"].data[0][1].links
    assert by["by_entity"].wide and not by["net_worth"].wide
    # Only what was asked for; and a pack as of an earlier date says the forecast still starts today.
    two = pack.build(workspace, FakeCampfire(), date(2026, 9, 1), date(2026, 10, 31), TODAY, ("schedule", "liquidity"))  # type: ignore[arg-type]
    assert [s.key for s in two.sections] == ["schedule", "liquidity"] and "always starts from today" in two.sections[1].note


@pytest.mark.django_db
def test_pack_page_and_books_that_cannot_be_read(client, workspace: Workspace, alder: Family) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import unquote

    from tests.conftest import context_token

    with mock.patch("gl.pack_views.CampfireClient", return_value=FakeCampfire()), mock.patch("gl.pack_views.timezone.localdate", return_value=TODAY):
        page = client.get(f"/fo/frame/pack?cf_ctx={context_token(customer_id=workspace.customer_id, surface='nav', user_id=3)}").content.decode()
        assert "Reporting pack" in page and "Build the pack" in page and page.count('name="s"') == len(pack.KEYS) and "pack-cover" not in page  # nothing is read until asked
        token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
        built = client.get("/fo/frame/pack", {"fo_t": token, "build": 1, "period": "ytd"}).content.decode()
        for part in ("Family Reporting Pack", "January 1, 2026 to November 13, 2026", "Unaudited", "At a glance", "Statement of Net Worth by family member",
                     "Schedule of Investments", "Investment Performance Report", "Projected cash at month end", "Capital Account Statements", "data-structure",
                     "Print or save as PDF"):
            assert part in built, part
        assert built.count('class="pack-section') == len(pack.KEYS) and built.count("pack-section wide") == 7
        few = client.get("/fo/frame/pack", {"fo_t": token, "build": 1, "s": ["schedule", "structure"]}).content.decode()
        assert few.count('class="pack-section') == 2 and "Statement of Changes in Net Worth</h2>" not in few

    def down(workspace: Workspace) -> Any:
        client_ = mock.Mock()
        client_.get_json.side_effect = NotConnected("no connection")
        return client_

    with mock.patch("gl.pack_views._client", side_effect=down), mock.patch("gl.pack_views.timezone.localdate", return_value=TODAY):
        offline = client.get("/fo/frame/pack", {"fo_t": token, "build": 1}).content.decode()
        assert offline.count("Couldn&#x27;t read the books from Campfire") >= 5  # each section that needs the books says so
        assert "Schedule of Investments" in offline and "Priced holdings: time-weighted return" in offline and "data-structure" in offline  # the rest is still there
