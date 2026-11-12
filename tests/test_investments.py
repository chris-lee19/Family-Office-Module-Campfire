from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Entity, Family
from investments.demo import seed_alder_investments
from investments.models import Account, CapitalCall, Commitment, Instrument, Price, Transaction, Valuation
from investments.services.ledger import LedgerError, replay
from investments.services.portfolio import UNALLOCATED, build_portfolio
from investments.services.privates import irr, mark_call_paid, summarize, xirr
from investments.services.valuation import holdings, record_valuation
from investments.taxonomy import ensure_default_taxonomy
from tests.conftest import context_token

TODAY = date(2026, 6, 30)
T = Transaction.Type


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    return family


def acct(family: Family, name: str) -> Account:
    return Account.objects.get(family=family, name=name)


@pytest.mark.django_db
def test_portfolio_totals_and_look_through(alder: Family) -> None:
    p = build_portfolio(alder, TODAY)
    assert p.total == Decimal("23874900.00")
    # LP (5.88M) and RE LLC (10.2M) are 25% each to four people through the ownership chain.
    assert dict(p.by_person) == {
        "Robert Alder": Decimal("6482675.00"),   # 4,020,000 look-through + his revocable trust
        "Eleanor Alder": Decimal("4453600.00"),  # 4,020,000 look-through + her IRA (person-owned)
        "Claire Moreno": Decimal("5693562.50"),  # + half the dynasty trust
        "James Alder": Decimal("5693562.50"),
        UNALLOCATED: Decimal("1551500.00"),      # foundation: no family owner
    }
    assert sum(p.by_person.values()) == p.total
    assert p.by_tax_treatment["Tax-deferred"] == Decimal("433600.00")
    assert p.by_tax_treatment["Tax-exempt"] == Decimal("1551500.00")
    assert p.by_liquidity["Illiquid"] == Decimal("16080000.00")
    assert p.unfunded == Decimal("2100000.00")


@pytest.mark.django_db
def test_fifo_lots_cash_and_realized_gain(alder: Family) -> None:
    state = replay(acct(alder, "Robert Alder RT – Custody"), TODAY)
    ustm = Instrument.objects.get(ticker="USTM")
    assert state.quantity(ustm.pk) == Decimal("5000")
    assert state.cost_basis(ustm.pk) == Decimal("1080000.00")  # 4000 left of the $210 lot + 1000 @ $240
    assert state.cash == Decimal("611475.00")
    [sale] = state.realized
    assert (sale.proceeds, sale.cost, sale.gain, sale.term) == (Decimal("529975.00"), Decimal("420000.00"), Decimal("109975.00"), "long")


@pytest.mark.django_db
def test_sale_across_lots_short_term_and_oversell(workspace: Workspace) -> None:
    family = seed_alder(workspace)
    classes = ensure_default_taxonomy(workspace)
    stock = Instrument.objects.create(workspace=workspace, name="Example Co", kind="equity", asset_class=classes["public_equity"], is_unitized=True)
    account = Account.objects.create(family=family, entity=Entity.objects.get(family=family, short_name="AFI LP"), name="Test", account_type="brokerage")
    d = date(2026, 1, 2)
    for i, (qty, amt) in enumerate((("100", "1000"), ("100", "2000"))):
        Transaction.objects.create(account=account, instrument=stock, txn_type=T.BUY, trade_date=d + timedelta(days=i), quantity=Decimal(qty), amount=Decimal(amt))
    Transaction.objects.create(account=account, instrument=stock, txn_type=T.SELL, trade_date=d + timedelta(days=30), quantity=Decimal("150"), amount=Decimal("3000"))
    state = replay(account, TODAY)
    assert [(r.quantity, r.proceeds, r.cost, r.term) for r in state.realized] == [
        (Decimal("100"), Decimal("2000.00"), Decimal("1000"), "short"),
        (Decimal("50"), Decimal("1000.00"), Decimal("1000.00"), "short"),
    ]
    assert state.quantity(stock.pk) == Decimal("50") and state.cost_basis(stock.pk) == Decimal("1000.00")
    assert state.cash == Decimal("0")  # -1000 - 2000 + 3000

    Transaction.objects.create(account=account, instrument=stock, txn_type=T.SELL, trade_date=d + timedelta(days=40), quantity=Decimal("60"), amount=Decimal("1"))
    with pytest.raises(LedgerError, match="only 50"):
        replay(account, TODAY)


@pytest.mark.django_db
def test_private_fund_metrics(alder: Family) -> None:
    summit = Commitment.objects.get(instrument__name__startswith="Summit")
    s = summarize(summit, TODAY)
    assert (s.contributed, s.distributed, s.recallable) == (Decimal("4000000"), Decimal("500000"), Decimal("200000"))
    assert s.unfunded == Decimal("1200000.00")  # 5M - 4M called + 200k recallable
    assert s.nav == Decimal("4700000.00") and not s.nav_stale
    assert (s.dpi, s.tvpi) == (Decimal("0.12"), Decimal("1.30"))
    assert s.pending_calls == Decimal("500000")
    assert Decimal("0.11") < irr(summit, TODAY, s.nav) < Decimal("0.12")  # type: ignore[operator]


def test_xirr_known_values() -> None:
    assert xirr([(date(2025, 1, 1), Decimal("-100")), (date(2026, 1, 1), Decimal("110"))]) == pytest.approx(Decimal("0.10"), abs=Decimal("0.0001"))
    assert xirr([(date(2025, 1, 1), Decimal("-100"))]) is None  # no inflow: undefined


@pytest.mark.django_db
def test_misread_nav_is_held_and_not_used(alder: Family) -> None:
    crescent = acct(alder, "AFI LP – Crescent Venture Fund II")
    held = Valuation.objects.get(account=crescent, status=Valuation.Status.HELD)
    assert held.value == Decimal("6850000.00") and held.change_pct == Decimal("480.51")
    [h] = holdings(crescent, TODAY)
    assert h.market_value == Decimal("1180000.00")  # last accepted mark, not the mis-read
    assert h.is_stale  # last accepted mark is older than 120 days


@pytest.mark.django_db
def test_nav_change_explained_by_cash_flows_is_not_held(alder: Family) -> None:
    crescent = acct(alder, "AFI LP – Crescent Venture Fund II")
    fund = Instrument.objects.get(name__startswith="Crescent")
    Transaction.objects.create(account=crescent, instrument=fund, txn_type=T.CONTRIBUTION, trade_date=TODAY - timedelta(days=10), amount=Decimal("500000"))
    v = record_valuation(account=crescent, instrument=fund, as_of=TODAY, value=Decimal("1700000"), method=Valuation.Method.NAV_STATEMENT)
    assert v.status == Valuation.Status.ACCEPTED and v.change_pct == Decimal("1.19")  # vs 1,180,000 + 500,000


@pytest.mark.django_db
def test_paying_a_capital_call_books_the_contribution_once(alder: Family) -> None:
    call = CapitalCall.objects.get()
    before = summarize(call.commitment, TODAY + timedelta(days=12))
    txn = mark_call_paid(call, TODAY + timedelta(days=9))
    again = mark_call_paid(call, TODAY + timedelta(days=9))
    assert txn.pk == again.pk and txn.amount == Decimal("500000")
    call.refresh_from_db()
    assert call.status == CapitalCall.Status.PAID
    after = summarize(call.commitment, TODAY + timedelta(days=12))
    assert after.unfunded == before.unfunded - Decimal("500000")
    assert after.pending_calls == 0


@pytest.mark.django_db
def test_stale_and_unpriced_holdings(alder: Family) -> None:
    [prop] = holdings(acct(alder, "ARE LLC – Harbor Street Property"), TODAY)
    assert prop.method == "appraisal" and prop.is_stale and prop.cost_basis == Decimal("8620000.00")
    Price.objects.filter(instrument__ticker="INTD").delete()
    intd = next(h for h in holdings(acct(alder, "Robert Alder RT – Custody"), TODAY) if h.instrument and h.instrument.ticker == "INTD")
    assert intd.method == "cost" and intd.is_stale and intd.market_value == intd.cost_basis


@pytest.mark.django_db
def test_investments_frame_and_tenancy(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    other = Workspace.objects.create(customer_id=99, name="Other", access_token="a", refresh_token="r")
    r = client.get(f"/fo/frame/investments?cf_ctx={context_token(customer_id=alder.workspace.customer_id, surface='nav')}")
    body = r.content.decode()
    assert r.status_code == 200 and "Summit Growth Partners IV" in body and "Held" in body
    r = client.get(f"/fo/frame/investments?cf_ctx={context_token(customer_id=other.customer_id, surface='nav')}")
    assert r.status_code == 200 and "Summit" not in r.content.decode()


@pytest.mark.django_db
def test_seed_command_and_reset() -> None:
    out = StringIO()
    call_command("seed_demo", "--customer-id", "321", "--create-workspace", stdout=out)
    call_command("seed_demo", "--customer-id", "321", "--reset", stdout=out)
    assert Family.objects.filter(workspace__customer_id=321).count() == 1
    assert Account.objects.filter(family__workspace__customer_id=321).count() == 7
    assert Instrument.objects.filter(workspace__customer_id=321).count() == 7  # orphans from the first seed removed
