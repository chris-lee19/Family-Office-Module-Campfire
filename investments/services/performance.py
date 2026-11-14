"""Investment Performance Report: how the investments have done.

Priced securities get a time-weighted return, so money added or withdrawn doesn't flatter or punish the result: the
period is cut at each month end, each piece is measured by Modified Dietz (gain over the opening value plus the
day-weighted money put in), and the pieces are linked. Purchases and transfers in are money put in; sales, transfers
out, dividends and interest are money taken out, so income counts as return. Trading fees are in the purchases and
sales. Cash and fees charged to cash are not part of it: this is the return of the securities.

Private funds are measured the way their cash flows ask for: IRR and multiples of paid-in capital.
"""

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from entities.models import Family
from investments.models import AssetClass, Commitment, Transaction
from investments.services.ledger import ZERO, LedgerError
from investments.services.privates import CommitmentSummary, irr, summarize, xirr
from investments.services.valuation import Holding, accounts_for, holdings

T = Transaction.Type
MONEY_IN = (T.BUY, T.TRANSFER_IN)
MONEY_OUT = (T.SELL, T.TRANSFER_OUT, T.DIVIDEND, T.INTEREST)
PERIODS = (("mtd", "Month to date"), ("qtd", "Quarter to date"), ("ytd", "Year to date"), ("y1", "1 year"), ("itd", "Since inception"))
Key = tuple[int, int]  # (account id, instrument id)


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def period_start(key: str, as_of: date) -> date | None:
    """The date a period's return starts from (the close of that day); None for since inception."""
    if key == "mtd":
        return as_of.replace(day=1) - timedelta(days=1)
    if key == "qtd":
        return date(as_of.year, 3 * ((as_of.month - 1) // 3) + 1, 1) - timedelta(days=1)
    if key == "ytd":
        return date(as_of.year - 1, 12, 31)
    if key == "y1":
        try:
            return as_of.replace(year=as_of.year - 1)
        except ValueError:  # Feb 29
            return as_of.replace(year=as_of.year - 1, day=28)
    return None


def breakpoints(start: date, end: date) -> list[date]:
    """The start, every month end strictly between, and the end."""
    out, d = [start], month_end(start)
    while d < end:
        if d > start:
            out.append(d)
        d = month_end(d + timedelta(days=1))
    return out + [end]


@dataclass
class ReturnRow:
    label: str
    market_value: Decimal
    returns: dict[str, Decimal | None] = field(default_factory=dict)  # period key -> return as a fraction
    since: date | None = None
    annualized: bool = False  # the since-inception figure is a yearly rate (more than a year of history)
    detail: str = ""

    @property
    def cells(self) -> list[Decimal | None]:
        return [self.returns.get(key) for key, _ in PERIODS]


@dataclass
class Performance:
    family: Family
    as_of: date
    total: ReturnRow | None = None
    by_asset_class: list[ReturnRow] = field(default_factory=list)
    by_account: list[ReturnRow] = field(default_factory=list)
    by_holding: list[ReturnRow] = field(default_factory=list)
    growth: list[tuple[date, Decimal]] = field(default_factory=list)  # 100 at the start, monthly, the last year
    privates: list[tuple[CommitmentSummary, Decimal | None]] = field(default_factory=list)
    pooled_irr: Decimal | None = None
    other: list[Holding] = field(default_factory=list)  # appraised and direct holdings: value against cost

    def private_total(self, name: str) -> Decimal:
        return sum(((c.commitment.amount if name == "commitment" else getattr(c, name)) for c, _ in self.privates), ZERO)

    @property
    def private_tvpi(self) -> Decimal | None:
        paid = self.private_total("contributed")
        return ((self.private_total("distributed") + self.private_total("nav")) / paid).quantize(Decimal("0.01")) if paid else None


class _Book:
    """Values and flows of the priced holdings, read once and reused for every row and period."""

    def __init__(self, family: Family, as_of: date) -> None:
        self.as_of = as_of
        self.accounts = {a.pk: a for a in accounts_for(family, as_of)}
        self._values: dict[tuple[int, date], dict[int, Decimal]] = {}
        self.flows: dict[Key, list[tuple[date, Decimal]]] = {}
        self.first: dict[Key, date] = {}
        self.instruments: dict[int, object] = {}
        for t in Transaction.objects.filter(account__in=self.accounts, trade_date__lte=as_of, instrument__isnull=False,
                                            txn_type__in=[*MONEY_IN, *MONEY_OUT]).select_related("instrument"):
            if not t.instrument.is_unitized:  # type: ignore[union-attr]
                continue
            key = (t.account_id, t.instrument_id)
            if t.txn_type in MONEY_IN:
                amount = t.amount + t.fees
            elif t.txn_type == T.SELL:
                amount = -(t.amount - t.fees)
            else:
                amount = -t.amount
            self.flows.setdefault(key, []).append((t.trade_date, amount))
            self.first[key] = min(self.first.get(key, t.trade_date), t.trade_date)
            self.instruments[t.instrument_id] = t.instrument

    def value(self, key: Key, when: date) -> Decimal:
        account_id, instrument_id = key
        if (account_id, when) not in self._values:
            try:
                rows = holdings(self.accounts[account_id], when)
            except LedgerError:
                rows = []
            self._values[(account_id, when)] = {h.instrument.pk: h.market_value for h in rows if h.instrument is not None}
        return self._values[(account_id, when)].get(instrument_id, ZERO)

    def linked(self, keys: list[Key], start: date, end: date) -> Decimal | None:
        """The time-weighted return of a group of holdings from the close of `start` to the close of `end`."""
        growth, measured = Decimal(1), False
        points = breakpoints(start, end)
        for a, b in zip(points, points[1:]):
            days = (b - a).days
            opening = sum((self.value(k, a) for k in keys), ZERO)
            closing = sum((self.value(k, b) for k in keys), ZERO)
            moved = [(d, amount) for k in keys for d, amount in self.flows.get(k, []) if a < d <= b]
            net = sum((amount for _, amount in moved), ZERO)
            base = opening + sum((amount * Decimal((b - d).days) / Decimal(days) for d, amount in moved), ZERO)
            if base <= 0:
                continue  # nothing invested in this piece
            growth *= 1 + (closing - opening - net) / base
            measured = True
        return growth - 1 if measured else None

    def row(self, label: str, keys: list[Key], detail: str = "") -> ReturnRow:
        since = min((self.first[k] for k in keys), default=None)
        row = ReturnRow(label, sum((self.value(k, self.as_of) for k in keys), ZERO), since=since, detail=detail)
        for key, _ in PERIODS:
            start = period_start(key, self.as_of)
            if since is None:
                row.returns[key] = None
            elif start is None:
                begin = since - timedelta(days=1)
                r = self.linked(keys, begin, self.as_of)
                years = Decimal((self.as_of - begin).days) / Decimal(365)
                if r is not None and years > 1 and r > -1:
                    r, row.annualized = Decimal(str((1 + float(r)) ** (1 / float(years)) - 1)), True
                row.returns[key] = r
            else:  # a period's return only where the holdings were there for all of it
                row.returns[key] = self.linked(keys, start, self.as_of) if since <= start else None
        return row


def build(family: Family, as_of: date) -> Performance:
    out = Performance(family, as_of)
    book = _Book(family, as_of)
    keys = [k for k in book.flows if book.value(k, as_of) or any(d <= as_of for d, _ in book.flows[k])]
    if keys:
        classes = {a.pk: a for a in AssetClass.objects.filter(workspace=family.workspace_id)}

        def top(instrument_id: int) -> str:
            ac = classes[book.instruments[instrument_id].asset_class_id]  # type: ignore[attr-defined]
            return (classes.get(ac.parent_id, ac) if ac.parent_id else ac).name

        out.total = book.row("All priced holdings", keys)
        groups: dict[str, list[Key]] = {}
        for k in keys:
            groups.setdefault(top(k[1]), []).append(k)
        out.by_asset_class = sorted((book.row(name, ks) for name, ks in groups.items()), key=lambda r: -r.market_value)
        by_account: dict[int, list[Key]] = {}
        for k in keys:
            by_account.setdefault(k[0], []).append(k)
        out.by_account = sorted((book.row(book.accounts[a].name, ks, book.accounts[a].custodian) for a, ks in by_account.items()), key=lambda r: -r.market_value)
        out.by_holding = sorted((book.row(book.instruments[k[1]].name, [k], book.accounts[k[0]].name) for k in keys  # type: ignore[attr-defined]
                                 if book.value(k, as_of)), key=lambda r: -r.market_value)
        # Growth of 100 over the last year (or since the first holding, when that is later).
        begin = max(period_start("y1", as_of), min(book.first[k] for k in keys) - timedelta(days=1))  # type: ignore[type-var]
        level, points = Decimal(100), breakpoints(begin, as_of)
        out.growth = [(points[0], level)]
        for a, b in zip(points, points[1:]):
            r = book.linked(keys, a, b)
            level = level * (1 + (r or ZERO))
            out.growth.append((b, level.quantize(Decimal("0.01"))))

    flows: list[tuple[date, Decimal]] = []
    for c in Commitment.objects.filter(account__family=family).select_related("instrument", "account"):
        summary = summarize(c, as_of)
        out.privates.append((summary, irr(c, as_of, summary.nav)))
        for t in Transaction.objects.filter(account=c.account, instrument=c.instrument, trade_date__lte=as_of):
            if t.txn_type == T.CONTRIBUTION:
                flows.append((t.trade_date, -t.amount))
            elif t.txn_type in Transaction.DISTRIBUTION_TYPES:
                flows.append((t.trade_date, t.amount))
        if summary.nav:
            flows.append((as_of, summary.nav))
    out.privates.sort(key=lambda p: -p[0].commitment.amount)
    out.pooled_irr = xirr(flows)
    committed = {c.commitment.account_id for c, _ in out.privates}
    for account in book.accounts.values():
        if account.pk in committed:
            continue
        try:
            out.other += [h for h in holdings(account, as_of) if h.instrument is not None and not h.instrument.is_unitized]
        except LedgerError:
            continue
    out.other.sort(key=lambda h: -h.market_value)
    return out
