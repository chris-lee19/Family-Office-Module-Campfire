"""Private-markets metrics per commitment: called, distributed, unfunded, NAV, DPI/RVPI/TVPI, IRR."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Sum

from investments.models import CapitalCall, Commitment, Transaction
from investments.services.ledger import ZERO
from investments.services.valuation import Holding, holdings

T = Transaction.Type


@dataclass(frozen=True)
class CommitmentSummary:
    commitment: Commitment
    contributed: Decimal
    distributed: Decimal
    recallable: Decimal
    nav: Decimal
    nav_date: date | None
    nav_stale: bool
    pending_calls: Decimal

    @property
    def unfunded(self) -> Decimal:
        return max(ZERO, self.commitment.amount - self.contributed + self.recallable)

    @property
    def pct_called(self) -> Decimal | None:
        return (self.contributed / self.commitment.amount * 100) if self.commitment.amount else None

    def _multiple(self, numerator: Decimal) -> Decimal | None:
        return (numerator / self.contributed).quantize(Decimal("0.01")) if self.contributed else None

    @property
    def dpi(self) -> Decimal | None:
        return self._multiple(self.distributed)

    @property
    def rvpi(self) -> Decimal | None:
        return self._multiple(self.nav)

    @property
    def tvpi(self) -> Decimal | None:
        return self._multiple(self.distributed + self.nav)


def xirr(cashflows: list[tuple[date, Decimal]]) -> Decimal | None:
    """Annualized IRR of dated cash flows (negative = paid in). None when it isn't defined."""
    if not cashflows or not any(a < 0 for _, a in cashflows) or not any(a > 0 for _, a in cashflows):
        return None
    start = min(d for d, _ in cashflows)
    flows = [((d - start).days / 365.0, float(a)) for d, a in cashflows]

    def npv(rate: float) -> float:
        return sum(a / (1 + rate) ** t for t, a in flows)

    low, high = -0.9999, 10.0
    f_low, f_high = npv(low), npv(high)
    if f_low * f_high > 0:
        return None
    for _ in range(200):  # bisection: slow but cannot diverge
        mid = (low + high) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-7:
            break
        if f_low * f_mid < 0:
            high = mid
        else:
            low, f_low = mid, f_mid
    return Decimal(str(round(mid, 6)))


def _sum(qs) -> Decimal:  # type: ignore[no-untyped-def]
    return qs.aggregate(s=Sum("amount"))["s"] or ZERO


def summarize(commitment: Commitment, as_of: date, holding: Holding | None = None) -> CommitmentSummary:
    txns = Transaction.objects.filter(account=commitment.account, instrument=commitment.instrument, trade_date__lte=as_of)
    distributions = txns.filter(txn_type__in=Transaction.DISTRIBUTION_TYPES)
    if holding is None:
        holding = next((h for h in holdings(commitment.account, as_of) if h.instrument == commitment.instrument), None)
    pending = commitment.calls.exclude(status__in=[CapitalCall.Status.PAID, CapitalCall.Status.CANCELLED])
    return CommitmentSummary(
        commitment=commitment,
        contributed=_sum(txns.filter(txn_type=T.CONTRIBUTION)),
        distributed=_sum(distributions),
        recallable=_sum(distributions.filter(is_recallable=True)),
        nav=holding.market_value if holding else ZERO,
        nav_date=holding.value_date if holding else None,
        nav_stale=holding.is_stale if holding else True,
        pending_calls=_sum(pending),
    )


def irr(commitment: Commitment, as_of: date, nav: Decimal) -> Decimal | None:
    flows: list[tuple[date, Decimal]] = []
    for t in Transaction.objects.filter(account=commitment.account, instrument=commitment.instrument, trade_date__lte=as_of):
        if t.txn_type == T.CONTRIBUTION:
            flows.append((t.trade_date, -t.amount))
        elif t.txn_type in Transaction.DISTRIBUTION_TYPES:
            flows.append((t.trade_date, t.amount))
    if nav:
        flows.append((as_of, nav))
    return xirr(flows)


@db_transaction.atomic
def mark_call_paid(call: CapitalCall, paid_on: date, source_ref: str = "") -> Transaction:
    """Record the contribution for a paid call (from Campfire's bill payment). Idempotent."""
    call = CapitalCall.objects.select_for_update().select_related("commitment__account", "commitment__instrument").get(pk=call.pk)
    if call.contribution_id:
        return call.contribution  # type: ignore[return-value]
    txn = Transaction.objects.create(
        account=call.commitment.account,
        instrument=call.commitment.instrument,
        txn_type=T.CONTRIBUTION,
        trade_date=paid_on,
        amount=call.amount,
        description=f"Capital call due {call.due_date}",
        source_ref=source_ref or f"capital-call:{call.pk}",
    )
    call.status = CapitalCall.Status.PAID
    call.paid_on = paid_on
    call.contribution = txn
    call.save(update_fields=["status", "paid_on", "contribution"])
    return txn


def paid_in_history(commitments: list[Commitment], today: date, max_points: int = 16) -> list[tuple[date, Decimal, Decimal]]:
    """Cumulative paid in and returned across commitments at each quarter end, from the first contribution to today:
    (quarter end, paid in, distributed). Years instead of quarters when the history is long."""
    rows = []
    for c in commitments:
        for t in Transaction.objects.filter(account=c.account, instrument=c.instrument, trade_date__lte=today):
            if t.txn_type == T.CONTRIBUTION:
                rows.append((t.trade_date, t.amount, ZERO))
            elif t.txn_type in Transaction.DISTRIBUTION_TYPES:
                rows.append((t.trade_date, ZERO, t.amount))
    if not rows:
        return []
    first = min(d for d, _, _ in rows)
    quarter_end = lambda y, q: date(y + (q == 4), 1 if q == 4 else 3 * q + 1, 1).replace(day=1)  # noqa: E731  (first day after the quarter)
    ends, y, q = [], first.year, (first.month - 1) // 3 + 1
    while True:
        end = min(date.fromordinal(quarter_end(y, q).toordinal() - 1), today)
        ends.append(end)
        if end >= today:
            break
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)
    if len(ends) > max_points:  # a long history: one point a year, and today
        ends = sorted({e for e in ends if e.month == 12} | {today})
    return [(end, sum((a for d, a, _ in rows if d <= end), ZERO), sum((b for d, _, b in rows if d <= end), ZERO)) for end in ends]
