"""Liquidity and Cash Flow Forecast: the cash each entity has, what is coming in, and what it has to pay.

Opening cash is the books' (Campfire): operating bank accounts and cash at custodians. Then, month by month:
- what is already known: open bills on their due dates, bills waiting for approval in the app, capital call notices;
- what the team expects (`PlannedCashFlow`): distributions to the family, tax payments, rent, debt service, fund
  distributions. These can be suggested from the books: what moved cash regularly over the last twelve months;
- optionally, the unfunded commitments that have no notice yet, spread evenly over a chosen number of months.

Nothing here is posted anywhere: the forecast is a view. Liquidity beyond cash (investments that can be sold in
days, and the tiers after that) comes from the investment records.
"""

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from campfire_integration.client import CampfireClient
from campfire_integration.models import Workspace
from entities.models import Family
from gl.models import AccountMapping, BillRequest, PlannedCashFlow, Role
from gl.services import bills as bill_svc
from gl.services import cash as cash_svc
from gl.services import statements
from investments.models import AssetClass, CapitalCall, Commitment
from investments.services.portfolio import BASE_CURRENCY, build_portfolio
from investments.services.privates import summarize
from records.models import AuditEntry

ZERO = Decimal(0)
HORIZONS = (3, 6, 12)
PACES = (0, 12, 24, 36, 48)  # months over which unfunded commitments are assumed to be called (0 = not included)
LOOKBACK_MONTHS = 12
LEDGER_CAP = 6000
D, F = PlannedCashFlow.Direction, PlannedCashFlow.Frequency
BILL, REQUEST, CALL, ESTIMATE, PLANNED = "bill", "request", "call", "estimate", "planned"
SOURCE_LABELS = {BILL: "Bill", REQUEST: "Bill to approve", CALL: "Capital call", ESTIMATE: "Estimate", PLANNED: "Planned"}
ROW_LABELS = {BILL: "Bills to pay", REQUEST: "Bills waiting for approval", CALL: "Capital calls (notices received)",
              ESTIMATE: "Capital calls (estimate from unfunded commitments)"}
# Accounts the investment bridge moves cash through: buying and selling isn't money the family receives or owes.
NOT_OPERATING_ROLES = (Role.CASH, Role.CASH_CLEARING, Role.TRANSFERS, Role.INVESTMENTS, Role.REALIZED_GAIN, Role.UNREALIZED, Role.UNREALIZED_GAIN)
RELATED_PARTY = ("due to", "due from", "intercompany")
NOT_CASH = ("prepaid", "depreciation", "amortization", "accrued")  # moved inside a cash entry, but isn't money in or out
TIERS = (("cash", "Cash"), (AssetClass.Liquidity.LIQUID, "Liquid investments"), (AssetClass.Liquidity.SEMI_LIQUID, "Semi-liquid investments"),
         (AssetClass.Liquidity.ILLIQUID, "Illiquid investments"))


class LiquidityError(Exception):
    pass


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


@dataclass(frozen=True)
class Month:
    start: date
    end: date

    @property
    def label(self) -> str:
        return f"{self.start:%b %Y}"

    @property
    def short(self) -> str:
        return f"{self.start:%b}"


@dataclass
class Flow:
    when: date
    entity: str
    label: str
    amount: Decimal  # in > 0, out < 0
    kind: str
    detail: str = ""
    late: bool = False  # was due before today: counted in the first month

    @property
    def source(self) -> str:
        return SOURCE_LABELS[self.kind]

    @property
    def row(self) -> str:
        return ROW_LABELS.get(self.kind, self.label)


@dataclass
class Line:
    label: str
    values: list[Decimal]

    @property
    def total(self) -> Decimal:
        return sum(self.values, ZERO)


@dataclass
class EntityRow:
    entity: str
    operating: Decimal = ZERO
    custody: Decimal = ZERO
    liquid: Decimal = ZERO  # investments that can be sold in days
    inflow: Decimal = ZERO
    outflow: Decimal = ZERO
    closing: list[Decimal] = field(default_factory=list)
    low: Decimal = ZERO
    low_month: str = ""

    @property
    def cash(self) -> Decimal:
        return self.operating + self.custody

    @property
    def short(self) -> bool:
        return self.low < 0

    @property
    def covered(self) -> bool:
        """Short of cash, but its own liquid investments would cover it."""
        return self.short and self.low + self.liquid >= 0

    @property
    def end(self) -> Decimal:
        return self.closing[-1] if self.closing else self.cash


@dataclass
class Unfunded:
    entity: str
    fund: str
    commitment: Decimal
    unfunded: Decimal
    noticed: Decimal  # open capital call notices, already in the forecast

    @property
    def not_called(self) -> Decimal:
        return max(ZERO, self.unfunded - self.noticed)


@dataclass
class Forecast:
    today: date
    months: list[Month]
    entity: str  # "" = all entities
    opening: Decimal = ZERO
    operating: Decimal = ZERO
    custody: Decimal = ZERO
    inflows: list[Line] = field(default_factory=list)
    outflows: list[Line] = field(default_factory=list)
    money_in: list[Decimal] = field(default_factory=list)
    money_out: list[Decimal] = field(default_factory=list)
    closing: list[Decimal] = field(default_factory=list)
    entities: list[EntityRow] = field(default_factory=list)
    flows: list[Flow] = field(default_factory=list)
    unfunded: list[Unfunded] = field(default_factory=list)
    tiers: list[tuple[str, Decimal]] = field(default_factory=list)
    pace: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def starts(self) -> list[Decimal]:
        return [self.opening, *self.closing[:-1]]

    @property
    def net(self) -> list[Decimal]:
        return [i - o for i, o in zip(self.money_in, self.money_out)]

    @property
    def low(self) -> Decimal:
        return min(self.closing) if self.closing else self.opening

    @property
    def low_month(self) -> Month | None:
        return self.months[self.closing.index(self.low)] if self.closing else None

    @property
    def end(self) -> Decimal:
        return self.closing[-1] if self.closing else self.opening

    @property
    def due_soon(self) -> list[Flow]:
        cutoff = self.today + timedelta(days=30)
        return [f for f in self.flows if f.amount < 0 and f.when <= cutoff and f.kind in (BILL, REQUEST, CALL)]

    @property
    def liquid(self) -> Decimal:
        return dict(self.tiers).get("Liquid investments", ZERO)

    @property
    def total_unfunded(self) -> Decimal:
        return sum((u.unfunded for u in self.unfunded), ZERO)

    @property
    def coverage(self) -> Decimal | None:
        """Cash and liquid investments as a multiple of the unfunded commitments."""
        return ((self.opening + self.liquid) / self.total_unfunded).quantize(Decimal("0.1")) if self.total_unfunded else None

    @property
    def short_entities(self) -> list[EntityRow]:
        return [e for e in self.entities if e.short]


def months_ahead(today: date, count: int) -> list[Month]:
    first = today.replace(day=1)
    return [Month(add_months(first, n), month_end(add_months(first, n))) for n in range(count)]


def occurrences(item: PlannedCashFlow, start: date, end: date) -> list[date]:
    """The dates a planned item falls on between two dates (both included)."""
    last = min(end, item.end) if item.end else end
    if item.frequency == F.ONCE:
        return [item.start] if start <= item.start <= last else []
    out, n, step = [], 0, PlannedCashFlow.STEP[item.frequency]
    while (when := add_months(item.start, n * step)) <= last:
        if when >= start:
            out.append(when)
        n += 1
    return out


def entity_names(client: CampfireClient) -> dict[int, str]:
    """Campfire's entities that keep books (id -> name), without the consolidated groups."""
    return {s.entity_id: s.label for s in statements.scopes(client) if not s.rollup}


def _owner(account: Any, names: dict[int, str]) -> str:
    if account.entity_id:
        return names.get(account.entity.campfire_entity_id, account.entity.name)
    return f"{account.person.display_name} (individual)"


def _known_flows(workspace: Workspace, client: CampfireClient, today: date, names: dict[int, str]) -> tuple[list[Flow], dict[int, Decimal]]:
    """Bills, bills waiting for approval and capital call notices. Also what each commitment has in open notices."""
    flows: list[Flow] = []
    calls = list(CapitalCall.objects.filter(commitment__account__family__workspace=workspace, status__in=CapitalCall.OPEN_STATUSES)
                 .select_related("commitment__instrument", "commitment__account__entity", "commitment__account__person"))
    call_bills = {c.campfire_bill_id for c in calls if c.campfire_bill_id}
    for b in bill_svc.bills(client, today):
        if not b.is_open or b.id in call_bills:  # a capital call's bill is counted as the call
            continue
        due = b.due_date or today
        flows.append(Flow(max(due, today), b.entity, b.vendor or "Bill", -b.due, BILL, f"Bill {b.number}".strip(), due < today))
    for r in BillRequest.objects.filter(workspace=workspace, status=BillRequest.Status.PENDING):
        flows.append(Flow(max(r.due_date, today), names.get(r.campfire_entity_id, r.entity_label), r.vendor_name, -r.amount, REQUEST,
                          f"Bill {r.bill_number}", r.due_date < today))
    noticed: dict[int, Decimal] = defaultdict(Decimal)
    for c in calls:
        noticed[c.commitment_id] += c.amount
        flows.append(Flow(max(c.due_date, today), _owner(c.commitment.account, names), c.commitment.instrument.name, -c.amount, CALL,
                          c.get_status_display(), c.due_date < today))
    return flows, noticed


def _investments(workspace: Workspace, today: date, names: dict[int, str]) -> tuple[dict[str, Decimal], dict[str, Decimal], dict[str, Decimal]]:
    """From the investment records: holdings by liquidity tier, liquid holdings by entity, custody cash by entity."""
    tiers: dict[str, Decimal] = defaultdict(Decimal)
    liquid: dict[str, Decimal] = defaultdict(Decimal)
    custody: dict[str, Decimal] = defaultdict(Decimal)
    classes = {a.id: a for a in AssetClass.objects.filter(workspace=workspace)}
    for family in Family.objects.filter(workspace=workspace):
        for h in build_portfolio(family, today).holdings:
            if h.account.currency != BASE_CURRENCY:
                continue
            owner = _owner(h.account, names)
            if h.instrument is None:
                custody[owner] += h.market_value
                continue
            tier = classes[h.instrument.asset_class_id].liquidity
            tiers[tier] += h.market_value
            if tier == AssetClass.Liquidity.LIQUID:
                liquid[owner] += h.market_value
    return tiers, liquid, custody


def _commitments(workspace: Workspace, today: date, names: dict[int, str], noticed: dict[int, Decimal]) -> list[Unfunded]:
    out = []
    for c in Commitment.objects.filter(account__family__workspace=workspace).select_related("instrument", "account__entity", "account__person"):
        s = summarize(c, today)
        if s.unfunded:
            out.append(Unfunded(_owner(c.account, names), c.instrument.name, c.amount, s.unfunded, noticed.get(c.pk, ZERO)))
    return sorted(out, key=lambda u: -u.unfunded)


def build(workspace: Workspace, client: CampfireClient, today: date, horizon: int = 12, pace: int = 0, entity_id: int | None = None) -> Forecast:
    months = months_ahead(today, horizon)
    last = months[-1].end
    names = entity_names(client)
    only = names.get(entity_id, "") if entity_id else ""
    fc = Forecast(today, months, only, pace=pace)

    balances, _ = cash_svc._balances(client, today)
    tiers, liquid, invest_cash = _investments(workspace, today, names)
    rows: dict[str, EntityRow] = {}
    for name, e in balances.items():
        rows[name] = EntityRow(name, e.operating, e.custody)
    for name, amount in invest_cash.items():  # an entity whose custody cash isn't in the books yet
        row = rows.setdefault(name, EntityRow(name))
        if not row.custody and amount:
            row.custody = amount
            fc.notes.append(f"{name}: cash at custodians comes from the investment records (not in the books yet).")
    for name, amount in liquid.items():
        rows.setdefault(name, EntityRow(name)).liquid = amount

    flows, noticed = _known_flows(workspace, client, today, names)
    for item in PlannedCashFlow.objects.filter(workspace=workspace):
        sign = 1 if item.direction == D.IN else -1
        entity = names.get(item.campfire_entity_id, item.entity_label)
        flows += [Flow(when, entity, item.label, sign * item.amount, PLANNED, item.get_frequency_display()) for when in occurrences(item, today, last)]
    fc.unfunded = _commitments(workspace, today, names, noticed)
    if pace:
        first = add_months(today.replace(day=1), 1)
        for u in fc.unfunded:
            each = (u.not_called / pace).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if each:
                flows += [Flow(month_end(add_months(first, n)), u.entity, u.fund, -each, ESTIMATE, f"Unfunded, called over {pace} months")
                          for n in range(min(pace, horizon - 1))]
    flows = [f for f in flows if f.when <= last]
    flows.sort(key=lambda f: (f.when, f.entity, f.label))
    index = lambda when: (when.year - months[0].start.year) * 12 + when.month - months[0].start.month  # noqa: E731

    # Each entity on its own: where its cash goes, and whether it runs short.
    for f in flows:
        rows.setdefault(f.entity, EntityRow(f.entity))
    for row in rows.values():
        net = [ZERO] * horizon
        for f in flows:
            if f.entity == row.entity:
                net[index(f.when)] += f.amount
                row.inflow += max(f.amount, ZERO)
                row.outflow += -min(f.amount, ZERO)
        running = row.cash
        for n in net:
            running += n
            row.closing.append(running)
        row.low = min(row.closing)
        row.low_month = months[row.closing.index(row.low)].label
    fc.entities = sorted((r for r in rows.values() if r.cash or r.liquid or r.inflow or r.outflow), key=lambda r: (not r.short, -r.cash))

    # The family (or the one entity chosen): the forecast by month.
    shown = [f for f in flows if not only or f.entity == only]
    scope_rows = [r for r in rows.values() if not only or r.entity == only]
    fc.flows = shown
    fc.operating, fc.custody = sum((r.operating for r in scope_rows), ZERO), sum((r.custody for r in scope_rows), ZERO)
    fc.opening = fc.operating + fc.custody
    grouped: dict[tuple[bool, str], list[Decimal]] = {}
    for f in shown:
        grouped.setdefault((f.amount > 0, f.row), [ZERO] * horizon)[index(f.when)] += f.amount
    order = {label: n for n, label in enumerate(ROW_LABELS.values())}
    lines = sorted(grouped.items(), key=lambda kv: (order.get(kv[0][1], 99), -abs(sum(kv[1], ZERO))))
    fc.inflows = [Line(label, values) for (is_in, label), values in lines if is_in]
    fc.outflows = [Line(label, [-v for v in values]) for (is_in, label), values in lines if not is_in]
    fc.money_in = [sum((ln.values[n] for ln in fc.inflows), ZERO) for n in range(horizon)]
    fc.money_out = [sum((ln.values[n] for ln in fc.outflows), ZERO) for n in range(horizon)]
    running = fc.opening
    for n in range(horizon):
        running += fc.money_in[n] - fc.money_out[n]
        fc.closing.append(running)
    if only:  # the tiers of one entity: its cash and its liquid investments
        fc.tiers = [("Cash", fc.opening), ("Liquid investments", sum((r.liquid for r in scope_rows), ZERO))]
        fc.unfunded = [u for u in fc.unfunded if u.entity == only]
    else:
        fc.tiers = [(label, fc.opening if key == "cash" else tiers.get(key, ZERO)) for key, label in TIERS]
    return fc


# ---- planned items ----

def _amount(raw: Any) -> Decimal:
    try:
        value = Decimal(str(raw or "").replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        raise LiquidityError("Enter the amount as a number.") from None
    if value <= 0:
        raise LiquidityError("The amount must be more than zero.")
    return value.quantize(Decimal("0.01"))


def _date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw or "")[:10])
    except ValueError:
        return None


def add(workspace: Workspace, data: Any, names: dict[int, str], actor: str, prefix: str = "") -> PlannedCashFlow:
    """Add an expected receipt or payment. `names`: the workspace's Campfire entities (id -> name)."""
    get = lambda key: data.get(prefix + key)  # noqa: E731
    entity = int(get("entity")) if str(get("entity") or "").isdigit() else 0
    label = str(get("label") or "").strip()
    start, end = _date(get("start")), _date(get("end"))
    if entity not in names or not label:
        raise LiquidityError("Choose the entity and say what it is.")
    if get("direction") not in D.values or get("frequency") not in F.values:
        raise LiquidityError("Choose money in or money out, and how often.")
    if start is None or (end is not None and end < start):
        raise LiquidityError("Enter the first date, and a last date on or after it if there is one.")
    account = int(get("account")) if str(get("account") or "").isdigit() else None
    item = PlannedCashFlow.objects.create(
        workspace=workspace, campfire_entity_id=entity, entity_label=names[entity][:255], label=label[:120], direction=get("direction"),
        amount=_amount(get("amount")), frequency=get("frequency"), start=start, end=None if get("frequency") == F.ONCE else end,
        campfire_account_id=account, created_by=actor)
    AuditEntry.objects.create(workspace=workspace, actor=actor, action=AuditEntry.Action.CREATED, kind="planned-cash-flows", object_id=str(item.pk),
                              object_repr=str(item)[:255], changes={"planned": ["", str(item)[:255]]})
    return item


def remove(workspace: Workspace, pk: int, actor: str) -> PlannedCashFlow:
    item = PlannedCashFlow.objects.filter(workspace=workspace, pk=pk).first()
    if item is None:
        raise LiquidityError("That item isn't in this workspace any more.")
    AuditEntry.objects.create(workspace=workspace, actor=actor, action=AuditEntry.Action.DELETED, kind="planned-cash-flows", object_id=str(item.pk),
                              object_repr=str(item)[:255], changes={"planned": [str(item)[:255], ""]})
    item.delete()
    return item


# ---- suggestions from the books ----

@dataclass
class Suggestion:
    entity_id: int
    entity: str
    account_id: int
    label: str
    direction: str
    amount: Decimal
    frequency: str
    start: date
    basis: str  # what the books showed
    checked: bool = True

    @property
    def frequency_label(self) -> str:
        return str(F(self.frequency).label)

    @property
    def direction_label(self) -> str:
        return str(D(self.direction).label)


@dataclass
class Suggestions:
    items: list[Suggestion]
    start: date
    end: date
    one_offs: int = 0
    partial: bool = False  # the ledger was too long to read in full


def _ledger(client: CampfireClient, start: date, end: date) -> tuple[list[dict[str, Any]], bool]:
    items: list[dict[str, Any]] = []
    path, query = cash_svc.LEDGER_PATH, {"start_date": start.isoformat(), "end_date": end.isoformat(), "limit": 200}
    total = 0
    while path and len(items) < LEDGER_CAP:
        page = client.get_json(path, params=query)
        total = int(page.get("count") or 0)
        items += page.get("results", [])
        nxt = page.get("next")
        path, query = (nxt.split(".com", 1)[-1], None) if nxt else ("", None)  # type: ignore[assignment]
    return items, total > len(items)


def suggest(workspace: Workspace, client: CampfireClient, today: date) -> Suggestions:
    """What moved cash regularly over the last twelve complete months: each entry that touched a bank account,
    read by the accounts on its other side. One-off movements aren't suggested."""
    names = entity_names(client)
    ids = {name: pk for pk, name in names.items()}
    window_end = today.replace(day=1) - timedelta(days=1)
    window_start = add_months(today.replace(day=1), -LOOKBACK_MONTHS)
    lines, partial = _ledger(client, window_start, today)
    skip = set(AccountMapping.objects.filter(workspace=workspace, role__in=NOT_OPERATING_ROLES).values_list("campfire_account_id", flat=True))
    journals: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for t in lines:
        if not t.get("is_deleted"):
            journals[t.get("journal")].append(t)
    # (entity, account) -> month -> cash in (+) or out (-)
    moved: dict[tuple[str, int], dict[date, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    labels: dict[tuple[str, int], str] = {}
    last_seen: dict[tuple[str, int], date] = {}
    first_month: dict[str, date] = {}
    for entry in journals.values():
        if not any(t.get("account_subtype") == "BANK" for t in entry) or "opening" in str(entry[0].get("journal_memo") or "").lower():
            continue  # no cash moved, or the entry that set up the opening balances
        for t in entry:
            when, account = _date(t.get("posted_at")), t.get("account")
            name = str(t.get("account_name") or "")
            if (t.get("account_subtype") == "BANK" or when is None or account is None or account in skip or t.get("bill")
                    or str(t.get("account_type") or "").startswith("UNCATEGORIZED") or any(w in name.lower() for w in RELATED_PARTY)
                    or "accounts payable" in name.lower()):
                continue
            key = (str(t.get("entity_name") or ""), int(account))
            amount = cash_svc._dec(t.get("credit_amount")) - cash_svc._dec(t.get("debit_amount"))
            month = when.replace(day=1)
            last_seen[key] = max(last_seen.get(key, when), when)
            labels[key] = name.partition(" - ")[2] or name
            if when <= window_end:
                moved[key][month] += amount
                first_month[key[0]] = min(first_month.get(key[0], month), month)
    have = set(PlannedCashFlow.objects.filter(workspace=workspace, campfire_account_id__isnull=False).values_list("campfire_entity_id", "campfire_account_id"))
    out = Suggestions([], window_start, window_end, partial=partial)
    whole = lambda v: abs(v).quantize(Decimal("1"), rounding=ROUND_HALF_UP).quantize(Decimal("0.01"))  # noqa: E731
    for key, by_month in moved.items():
        entity, account = key
        active = sorted(m for m, v in by_month.items() if v)
        total = sum(by_month.values(), ZERO)
        if entity not in ids or (ids[entity], account) in have or not active or not total:
            continue
        if len(active) == 1:
            out.one_offs += 1
            continue
        span = (window_end.year - first_month[entity].year) * 12 + window_end.month - first_month[entity].month + 1
        gaps = {(b.year - a.year) * 12 + b.month - a.month for a, b in zip(active, active[1:])}
        each = total / len(active)  # the average time: a large month (a tax instalment) is spread, never dropped
        times = f"{len(active)} times in the last {span} months"
        if gaps == {1} and len(active) >= 3 or len(active) * 4 >= span * 3:
            frequency, amount, basis = F.MONTHLY, each, f"{len(active)} of the last {span} months"
        elif gaps <= {2, 3, 4} and (len(active) >= 3 or gaps == {3}):
            frequency, amount, basis = F.QUARTERLY, each, f"about every quarter: {times}"
        elif gaps == {6}:
            frequency, amount, basis = F.SEMIANNUAL, each, f"twice a year: {times}"
        else:
            frequency, amount, basis = F.MONTHLY, total / span, f"irregular: {times}, averaged per month"
        when, n = last_seen[key], 0
        while when < today:  # the next time it falls due, counted from the last time it happened
            n += 1
            when = add_months(last_seen[key], n * PlannedCashFlow.STEP[frequency])
        if not whole(amount):
            continue
        not_cash = any(w in labels[key].lower() for w in NOT_CASH)
        out.items.append(Suggestion(ids[entity], entity, account, labels[key], D.IN if amount > 0 else D.OUT, whole(amount), frequency, when,
                                    basis + (" · usually not cash" if not_cash else ""), checked=not not_cash))
    out.items.sort(key=lambda s: (s.entity, s.direction != D.IN, -s.amount))
    return out
