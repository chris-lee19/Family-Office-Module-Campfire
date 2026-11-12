"""Positions with market values, NAV roll-forward, stale marks and the valuation anomaly hold (REC-06)."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q, Sum

from investments.models import Account, Instrument, Price, Transaction, Valuation
from investments.services.ledger import ZERO, AccountState, replay

T = Transaction.Type
PRICE_STALE_DAYS = 7
MARK_STALE_DAYS = 120
DEFAULT_CHANGE_THRESHOLD_PCT = Decimal("50")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Holding:
    account: Account
    instrument: Instrument | None  # None = cash
    quantity: Decimal | None
    cost_basis: Decimal
    market_value: Decimal
    value_date: date | None
    method: str  # price | nav_rollforward | appraisal | cost | cash
    is_stale: bool

    @property
    def unrealized(self) -> Decimal:
        return self.market_value - self.cost_basis if self.instrument else ZERO

    @property
    def method_label(self) -> str:
        return {"price": "Price", "nav_rollforward": "NAV", "appraisal": "Appraisal", "cost": "At cost", "cash": "Cash"}[self.method]

    @property
    def name(self) -> str:
        return self.instrument.name if self.instrument else "Cash"


def latest_price(instrument: Instrument, as_of: date) -> Price | None:
    return instrument.prices.filter(as_of__lte=as_of).order_by("-as_of").first()


def latest_accepted_valuation(account: Account, instrument: Instrument, as_of: date) -> Valuation | None:
    return (
        Valuation.objects.filter(account=account, instrument=instrument, as_of__lte=as_of, status=Valuation.Status.ACCEPTED)
        .order_by("-as_of", "-id")
        .first()
    )


def net_flows(account: Account, instrument: Instrument, after: date, through: date) -> Decimal:
    """Contributions/purchases in minus distributions/sales out, strictly after `after` through `through`."""
    rows = Transaction.objects.filter(account=account, instrument=instrument, trade_date__gt=after, trade_date__lte=through)
    inflow = rows.filter(txn_type__in=[T.CONTRIBUTION, T.BUY, T.TRANSFER_IN]).aggregate(s=Sum("amount"))["s"] or ZERO
    outflow = rows.filter(txn_type__in=[*Transaction.DISTRIBUTION_TYPES, T.SELL, T.TRANSFER_OUT]).aggregate(s=Sum("amount"))["s"] or ZERO
    return inflow - outflow


def rolled_forward_value(account: Account, instrument: Instrument, as_of: date) -> tuple[Decimal, Valuation] | None:
    """Last accepted mark plus cash flows since: the standard estimate between statements."""
    mark = latest_accepted_valuation(account, instrument, as_of)
    if mark is None:
        return None
    return max(ZERO, mark.value + net_flows(account, instrument, mark.as_of, as_of)), mark


def holdings(account: Account, as_of: date, state: AccountState | None = None) -> list[Holding]:
    state = state or replay(account, as_of)
    result: list[Holding] = []
    instruments = Instrument.objects.in_bulk(state.instrument_ids())
    for instrument_id, instrument in sorted(instruments.items(), key=lambda kv: kv[1].name):
        cost = state.cost_basis(instrument_id)
        if instrument.is_unitized:
            qty = state.quantity(instrument_id)
            price = latest_price(instrument, as_of)
            if price is None:
                result.append(Holding(account, instrument, qty, cost, cost, None, "cost", True))
                continue
            stale = (as_of - price.as_of) > timedelta(days=PRICE_STALE_DAYS)
            result.append(Holding(account, instrument, qty, cost, (qty * price.price).quantize(CENT), price.as_of, "price", stale))
        else:
            rolled = rolled_forward_value(account, instrument, as_of)
            if rolled is None:
                result.append(Holding(account, instrument, None, cost, cost, None, "cost", True))
                continue
            value, mark = rolled
            method = "appraisal" if mark.method == Valuation.Method.APPRAISAL else "nav_rollforward"
            stale = (as_of - mark.as_of) > timedelta(days=MARK_STALE_DAYS)
            result.append(Holding(account, instrument, None, cost, value, mark.as_of, method, stale))
    if account.tracks_cash and state.cash:
        result.append(Holding(account, None, None, state.cash, state.cash, as_of, "cash", False))
    return result


def change_threshold_pct(account: Account) -> Decimal:
    raw = account.family.workspace.install_config.get("valuation_change_threshold_pct")
    try:
        return Decimal(str(raw)) if raw not in (None, "") else DEFAULT_CHANGE_THRESHOLD_PCT
    except ArithmeticError:
        return DEFAULT_CHANGE_THRESHOLD_PCT


def record_valuation(
    *, account: Account, instrument: Instrument, as_of: date, value: Decimal, method: str,
    batch=None, source_ref: str = "", threshold_pct: Decimal | None = None,  # type: ignore[no-untyped-def]
) -> Valuation:
    """Store a new mark, holding it for review when it moves too far from the expected value.

    The expected value is the prior accepted mark rolled forward for cash flows since, so a NAV that
    rose because a call was paid in is not flagged.
    """
    threshold = threshold_pct if threshold_pct is not None else change_threshold_pct(account)
    status, change = Valuation.Status.ACCEPTED, None
    expected = rolled_forward_value(account, instrument, as_of)
    if expected is not None and expected[0] > 0:
        change = ((value - expected[0]) / expected[0] * 100).quantize(CENT)
        if abs(change) > threshold:
            status = Valuation.Status.HELD
    return Valuation.objects.create(
        account=account, instrument=instrument, as_of=as_of, value=value, method=method,
        status=status, change_pct=change, batch=batch, source_ref=source_ref,
    )


def held_valuations(family) -> list[Valuation]:  # type: ignore[no-untyped-def]
    return list(
        Valuation.objects.filter(account__family=family, status=Valuation.Status.HELD)
        .select_related("account", "instrument")
        .order_by("-as_of")
    )


def accounts_for(family, as_of: date):  # type: ignore[no-untyped-def]
    return (
        Account.objects.filter(family=family)
        .filter(Q(opened_on__isnull=True) | Q(opened_on__lte=as_of))
        .filter(Q(closed_on__isnull=True) | Q(closed_on__gte=as_of))
        .select_related("entity", "person", "family__workspace")
    )
