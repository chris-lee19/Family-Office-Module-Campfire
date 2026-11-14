"""Financial statements from Campfire's books, shaped for the app's pages: balance sheet, income statement, cash
flow and trial balance, for one entity or a consolidated group, in one column or by month or quarter, with the ledger
lines behind any account.

Campfire computes the statements (hierarchy, net income, consolidation, eliminations); this module asks for them
(`reports:read`, `transactions:read`) and puts the rows in a family office's reading order.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from campfire_integration.client import CampfireClient

BALANCE_SHEET_PATH = "/ca/api/get_balance_sheet"
INCOME_STATEMENT_PATH = "/ca/api/get_income_statement"
CASH_FLOW_PATH = "/ca/api/get_cash_flow"
TRIAL_BALANCE_PATH = "/ca/api/get_trial_balance"
LEDGER_PATH = "/coa/api/transaction"
ENTITY_PATH = "/coa/api/entity"
ZERO = Decimal(0)
# Statement names follow family-office practice, not corporate labels, and depend on whose books are shown:
#   family     the consolidated view for the principals: personal financial statements (ASC 274), at estimated
#              current value, with an estimate of income taxes on unrealized appreciation
#   investment a family investment vehicle (partnership, pooled LLC): investment company presentation (ASC 946),
#              at fair value
#   entity     any other legal entity: its conventional books, at cost
FAMILY, INVESTMENT, ENTITY = "family", "investment", "entity"
NAMES: dict[str, dict[str, str]] = {
    FAMILY: {"bs": "Statement of Net Worth", "is": "Statement of Changes in Net Worth", "cf": "Statement of Cash Flows", "tb": "Trial Balance",
             "tx": "Tax Basis and Unrealized Gain Report", "equity": "Net worth", "total": "Total liabilities and net worth",
             "result": "Excess of income over expenses", "change": "Change in net worth", "basis": "Estimated current value (ASC 274)", "value": "estimated current value"},
    INVESTMENT: {"bs": "Statement of Assets and Liabilities", "is": "Statement of Operations and Changes in Net Assets", "cf": "Statement of Cash Flows",
                 "tb": "Trial Balance", "tx": "Tax Basis and Unrealized Gain Report", "equity": "Net assets", "total": "Total liabilities and net assets",
                 "result": "Net increase (decrease) from operations", "change": "Change in net assets", "basis": "Fair value (ASC 946)", "value": "fair value"},
    ENTITY: {"bs": "Entity Balance Sheet", "is": "Statement of Income and Expenses", "cf": "Statement of Cash Flows", "tb": "Trial Balance",
             "tx": "Tax Basis and Unrealized Gain Report", "equity": "Equity", "total": "Total liabilities and equity",
             "result": "Excess of income over expenses", "change": "Change in equity", "basis": "The entity's books, investments at cost", "value": "cost"},
}
VIEWS = {"bs": "Net worth", "is": "Changes in net worth", "cf": "Cash flow", "tb": "Trial balance", "tx": "Tax basis"}  # the keys; labels come from NAMES
COLUMNS = {"total": "One column", "monthly": "By month", "quarterly": "By quarter"}
# The family's Statement of Net Worth can also be split across: a column per entity, or per family member (look-through).
SPLITS = {"entity": "By entity", "person": "By family member"}
NOT_HELD = "Not held by a family member"
PERIODS = {"ytd": "This year to date", "month": "Last month", "quarter": "Last quarter", "year": "Last year", "custom": "Custom dates"}


@dataclass
class Row:
    label: str
    values: list[Decimal | None]
    kind: str = "account"  # account | section | total | grand
    number: str = ""
    level: int = 0
    account_id: int | None = None  # set on account rows: the ledger lines can be opened


@dataclass
class Statement:
    title: str
    columns: list[str]
    rows: list[Row] = field(default_factory=list)
    note: str = ""


@dataclass(frozen=True)
class Scope:
    """Whose books: one Campfire entity, or a parent with everything under it (consolidated)."""

    entity_id: int
    rollup: bool
    label: str
    members: tuple[int, ...] = ()  # the Campfire entities whose books are in it
    kind: str = ENTITY  # family | investment | entity: which statements it gets (see NAMES)

    @property
    def names(self) -> dict[str, str]:
        return NAMES[self.kind]

    @property
    def fair_value(self) -> bool:
        return self.kind in (FAMILY, INVESTMENT)

    @property
    def value(self) -> str:
        return f"{self.entity_id}{'r' if self.rollup else ''}"

    def params(self) -> dict[str, Any]:
        return {"entity": self.entity_id, **({"entity_rollup": "true"} if self.rollup else {})}


def scopes(client: CampfireClient) -> list[Scope]:
    """What the statements can be run for: consolidated groups first, then each active entity."""
    data = client.get_json(ENTITY_PATH)
    entities = [e for e in (data if isinstance(data, list) else data.get("results", [])) if not e.get("is_deleted") and e.get("active", True)]
    parents = {e.get("parent") for e in entities if e.get("parent")}
    groups = [Scope(int(e["id"]), True, f"{e['name']}: all entities", tuple(int(c["id"]) for c in entities if c.get("parent") == e["id"]), FAMILY)
              for e in entities if e["id"] in parents]
    singles = [Scope(int(e["id"]), False, e["name"], (int(e["id"]),))
               for e in sorted(entities, key=lambda e: (bool(e.get("is_elimination_entity")), e["name"])) if e["id"] not in parents]
    return groups + singles


def classify(workspace: Any, options: list[Scope]) -> list[Scope]:
    """Mark the investment vehicles (entities taxed as partnerships, limited and general partnerships): they report
    like an investment company. Everything else that isn't a consolidated group keeps conventional entity books."""
    from dataclasses import replace

    from entities.models import Entity

    T, X = Entity.EntityType, Entity.TaxClassification
    vehicles = {e.campfire_entity_id for e in Entity.objects.filter(family__workspace=workspace, campfire_entity_id__isnull=False)
                if e.tax_classification == X.PARTNERSHIP or e.entity_type in (T.LIMITED_PARTNERSHIP, T.GENERAL_PARTNERSHIP)}
    return [s if s.rollup or s.entity_id not in vehicles else replace(s, kind=INVESTMENT) for s in options]


@dataclass
class FairValues:
    """What the app's investment records add to the books for a fair-value statement: unrealized appreciation
    (market value less cost) of the scope's holdings at the start and end, the GL accounts that already carry
    unrealized appreciation, and the rate for the estimate of income taxes on it (family statements only)."""

    start: Decimal = ZERO
    end: Decimal = ZERO
    booked_accounts: frozenset[int] = frozenset()
    tax_rate: Decimal = ZERO  # percent

    def tax(self, unrealized: Decimal) -> Decimal:
        return (max(unrealized, ZERO) * self.tax_rate / 100).quantize(Decimal("0.01"))


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def period_dates(period: str, today: date, start: str = "", end: str = "") -> tuple[date, date]:
    if period == "month":
        last = month_start(today) - timedelta(days=1)
        return month_start(last), last
    if period == "quarter":
        first_of_this = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        last = first_of_this - timedelta(days=1)
        return date(last.year, 3 * ((last.month - 1) // 3) + 1, 1), last
    if period == "year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if period == "custom":
        try:
            a, b = date.fromisoformat(start), date.fromisoformat(end)
            if a <= b:
                return a, b
        except ValueError:
            pass
    return date(today.year, 1, 1), today


def _dec(v: Any) -> Decimal:
    return Decimal(str(v or 0)).quantize(Decimal("0.01"))


def _label(key: str, cadence: str, as_of: bool, end: date) -> str:
    start = date.fromisoformat(key)
    if cadence == "quarterly":
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    if as_of:  # a balance: say the date it is at
        return f"{min(month_end(start), end):%b %-d, %Y}"
    return f"{start:%b %Y}"


def _fetch(client: CampfireClient, path: str, scope: Scope, start: date, end: date, columns: str) -> tuple[dict[str, Any], list[str], str]:
    cadence = "quarterly" if columns == "quarterly" else "monthly"
    data = client.get_json(path, params={**scope.params(), "start_date": start.isoformat(), "end_date": end.isoformat(), "cadence": cadence})
    keys = sorted(k for k in data.get("headers", {}) if k != "Total")
    return data, keys, cadence


def _account_rows(rows: list[dict[str, Any]], prefix: list[str], value: Any) -> list[Row]:
    """The account rows under a hierarchy prefix, by account number, indented by their depth under it."""
    found = [r for r in rows if r.get("hierarchy", [])[:len(prefix)] == prefix and len(r.get("hierarchy", [])) > len(prefix)
             and str(r["hierarchy"][-1]).isdigit()]
    found.sort(key=lambda r: (str(r.get("account_number") or "~"), str(r.get("account_name"))))
    return [Row(r.get("display_name") or r.get("account_name") or "", value(r), "account", str(r.get("account_number") or ""),
                len(r["hierarchy"]) - len(prefix), int(r["hierarchy"][-1])) for r in found]


def _group(rows: list[dict[str, Any]], hierarchy: list[str]) -> dict[str, Any]:
    return next((r for r in rows if r.get("hierarchy") == hierarchy), {})


def _nonzero(row: Row) -> bool:
    return any(v for v in row.values)


def balance_sheet(client: CampfireClient, scope: Scope, start: date, end: date, columns: str, fair: FairValues | None = None) -> Statement:
    if columns == "total":
        start = month_start(end)  # one column: the balances on the end date
    data, keys, cadence = _fetch(client, BALANCE_SHEET_PATH, scope, start, end, columns)
    rows = data.get("balance_sheet", [])
    value = lambda r: [_dec(r.get(f"{k}_Total")) for k in keys]  # noqa: E731
    le, names = "LIABILITIES_AND_EQUITY", scope.names
    out = Statement(names["bs"], [_label(k, cadence, True, end) for k in keys])
    add = out.rows.append
    # At fair value, the books' cost (plus any unrealized appreciation already booked) is brought to estimated current
    # value from the investment records. One column only: the adjustment is as of the statement date.
    adjust = tax = ZERO
    if fair is not None and scope.fair_value and len(keys) == 1:
        booked = sum((v[0] or ZERO for r in rows if str((r.get("hierarchy") or [""])[-1]).isdigit() and int(r["hierarchy"][-1]) in fair.booked_accounts
                      for v in [value(r)]), ZERO)
        adjust = fair.end - booked
        tax = fair.tax(fair.end) if scope.kind == FAMILY else ZERO
    plus = lambda r, extra: Row(r.label, [(r.values[0] or ZERO) + extra], r.kind)  # noqa: E731
    add(Row("Assets", [None] * len(keys), "section"))
    out.rows += _account_rows(rows, ["ASSET"], value)
    if adjust:
        add(Row(f"Investments: adjustment to {names['value']}", [adjust], "account", level=1))
    total_assets = Row("Total assets", value(_group(rows, ["ASSET"])), "total")
    add(plus(total_assets, adjust) if adjust else total_assets)
    add(Row("Liabilities", [None] * len(keys), "section"))
    out.rows += _account_rows(rows, [le, "LIABILITY"], value)
    if tax:
        add(Row("Estimated income taxes on unrealized appreciation", [tax], "account", level=1))
    total_liabilities = Row("Total liabilities", value(_group(rows, [le, "LIABILITY"])), "total")
    add(plus(total_liabilities, tax) if tax else total_liabilities)
    add(Row(names["equity"], [None] * len(keys), "section"))
    out.rows += _account_rows(rows, [le, "EQUITY"], value)
    for key, label in (("RETAINED_EARNINGS", "Accumulated from prior years"), ("NET_INCOME", "Income less expenses, year to date")):
        extra = Row(label, value(_group(rows, [le, "EQUITY", key])), "account", level=1)
        if _nonzero(extra):
            add(extra)
    if adjust:
        add(Row("Unrealized appreciation not in the books", [adjust], "account", level=1))
    if tax:
        add(Row("Less estimated income taxes", [-tax], "account", level=1))
    # Campfire leaves bank lines that nobody has categorized out of equity, so its statement doesn't balance while
    # any are open. Here they are a line of their own, and assets less liabilities is what is shown.
    assets, booked_total = value(_group(rows, ["ASSET"])), value(_group(rows, [le]))
    gap = [(a or ZERO) - (b or ZERO) for a, b in zip(assets, booked_total)]
    if any(gap):
        add(Row("Bank lines not categorized yet", list(gap), "account", level=1))
    extra = [g + (adjust - tax if n == 0 else ZERO) for n, g in enumerate(gap)]
    add(Row(names["equity"], [(v or ZERO) + x for v, x in zip(value(_group(rows, [le, "EQUITY"])), extra)], "total"))
    add(Row(names["total"], [(v or ZERO) + g + (adjust if n == 0 else ZERO) for n, (v, g) in enumerate(zip(booked_total, gap))], "grand"))
    if any(gap):
        out.note += "Bank lines not categorized yet are included as their own line; categorize them on the Cash page. "
    if scope.fair_value and fair is not None and len(keys) > 1:
        out.note = f"Columns over time show the books (investments at cost). Choose one column for {names['value']}. "
    return out


def _spread(amount: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """An amount split in proportion to weights, to the cent; what rounding leaves goes to the largest share."""
    total = sum(weights, ZERO)
    if not amount or not total:
        return [ZERO] * len(weights)
    parts = [(amount * w / total).quantize(Decimal("0.01")) for w in weights]
    parts[max(range(len(parts)), key=lambda i: weights[i])] += amount - sum(parts, ZERO)
    return parts


def net_worth_split(client: CampfireClient, scope: Scope, end: date, split: str, unrealized: dict[int, Decimal], booked: frozenset[int],
                    tax_rate: Decimal, shares: dict[int, dict[str, Decimal]] | None = None) -> Statement:
    """The family's Statement of Net Worth with a column per entity, or per family member.

    Each entity's column is its own books at the statement date, with its investments brought to estimated current
    value (`unrealized`: market less cost of what each Campfire entity holds; `booked`: the GL accounts that already
    carry unrealized appreciation). The family's estimated income taxes are spread over the entities that have
    appreciation. A family member's column is every entity's column times the share of it that traces to them
    (`shares`: Campfire entity id -> {person: fraction}); what traces to nobody (a foundation, or an entity that isn't
    linked) is its own column. The Total column is the consolidated statement, so the columns always add up to it."""
    names = scope.names
    data = client.get_json(BALANCE_SHEET_PATH, params={**scope.params(), "start_date": month_start(end).isoformat(), "end_date": end.isoformat(), "group_by": "entity"})
    key = next(iter(sorted(data.get("headers", {}), reverse=True)), None)
    rows = data.get("balance_sheet", [])
    ids = {s.label: s.entity_id for s in scopes(client) if not s.rollup}
    entities = [name for name in (data["headers"][key] if key else []) if name != "Total"]
    vec = lambda r: [_dec(r.get(f"{key}_{name}")) for name in entities]  # noqa: E731
    add_vec = lambda a, b: [x + y for x, y in zip(a, b)]  # noqa: E731
    zero = [ZERO] * len(entities)
    le = "LIABILITIES_AND_EQUITY"

    carried = zero
    for r in rows:
        if str((r.get("hierarchy") or [""])[-1]).isdigit() and int(r["hierarchy"][-1]) in booked:
            carried = add_vec(carried, vec(r))
    gains = [unrealized.get(ids.get(name, 0), ZERO) for name in entities]
    adjust = [g - c for g, c in zip(gains, carried)]
    total_gain = sum(gains, ZERO)
    tax_total = (max(total_gain, ZERO) * tax_rate / 100).quantize(Decimal("0.01")) if scope.kind == FAMILY else ZERO
    tax = _spread(tax_total, [max(g, ZERO) for g in gains])

    lines: list[tuple[Row, list[Decimal]]] = []  # each row with its figures per entity
    put = lambda label, values, kind="account", **kw: lines.append((Row(label, [], kind, **kw), values))  # noqa: E731
    put("Assets", None, "section")
    for r in _account_rows(rows, ["ASSET"], lambda r: []):
        source = next(x for x in rows if str((x.get("hierarchy") or [""])[-1]) == str(r.account_id) and x.get("hierarchy", [])[:1] == ["ASSET"])
        put(r.label, vec(source), number=r.number, level=r.level, account_id=r.account_id)
    if any(adjust):
        put(f"Investments: adjustment to {names['value']}", adjust, level=1)
    assets = add_vec(vec(_group(rows, ["ASSET"])), adjust)
    put("Total assets", assets, "total")
    put("Liabilities", None, "section")
    for r in _account_rows(rows, [le, "LIABILITY"], lambda r: []):
        source = next(x for x in rows if str((x.get("hierarchy") or [""])[-1]) == str(r.account_id) and x.get("hierarchy", [])[:2] == [le, "LIABILITY"])
        put(r.label, vec(source), number=r.number, level=r.level, account_id=r.account_id)
    if tax_total:
        put("Estimated income taxes on unrealized appreciation", tax, level=1)
    liabilities = add_vec(vec(_group(rows, [le, "LIABILITY"])), tax)
    put("Total liabilities", liabilities, "total")
    put(names["equity"], [a - b for a, b in zip(assets, liabilities)], "grand")

    if split == "person":
        shares = shares or {}
        of = [shares.get(ids.get(name, 0), {NOT_HELD: Decimal(1)}) for name in entities]
        net = lines[-1][1]
        held = sorted({p for s in of for p in s if p != NOT_HELD},
                      key=lambda p: -sum((net[i] * of[i].get(p, ZERO) for i in range(len(entities))), ZERO))
        columns = held + ([NOT_HELD] if any(NOT_HELD in s and s[NOT_HELD] for s in of) else [])

        def project(values: list[Decimal]) -> list[Decimal]:
            parts = [sum((values[i] * of[i].get(p, ZERO) for i in range(len(entities))), ZERO).quantize(Decimal("0.01")) for p in columns]
            if parts:  # shares of a cent: what rounding leaves goes to the largest column, so the row adds up
                parts[max(range(len(parts)), key=lambda i: abs(parts[i]))] += sum(values, ZERO) - sum(parts, ZERO)
            return parts
    else:
        keep = [i for i in range(len(entities)) if any(values and values[i] for _, values in lines)]
        columns = [entities[i] for i in keep]
        project = lambda values: [values[i] for i in keep]  # noqa: E731

    out = Statement(f"{names['bs']} {SPLITS[split].lower()}", columns + ["Total"])
    for row, values in lines:
        row.values = [None] * (len(columns) + 1) if values is None else [*project(values), sum(values, ZERO)]
        if row.kind != "account" or _nonzero(row):
            out.rows.append(row)
    out.note = ("Each family member's column is their share of every entity, traced through the ownership chain. " if split == "person"
                else "Each entity's own books, with its investments at estimated current value. ")
    out.note += "The family's estimated income taxes are spread over the entities with unrealized appreciation. " if tax_total else ""
    return out


def look_through(workspace: Any, as_of: date, ids: dict[str, int]) -> dict[int, dict[str, Decimal]]:
    """For each Campfire entity, the share of it that traces to each family member through the ownership chain
    (`{campfire entity id: {person: fraction}}`; what traces to nobody is under NOT_HELD). An app entity is matched to
    its Campfire entity by the link set on General ledger, or else by having the same name (`ids`: name -> id)."""
    from entities.models import Entity, Family, Person
    from entities.services.ownership import OwnershipCycleError, effective_ownership

    out: dict[int, dict[str, Decimal]] = {}
    by_name = {name.lower(): pk for name, pk in ids.items()}
    for family in Family.objects.filter(workspace=workspace, is_active=True):
        try:
            effective = effective_ownership(family, as_of)
        except OwnershipCycleError:
            continue
        people = {p.pk: p.display_name for p in Person.objects.filter(family=family)}
        for entity in Entity.objects.filter(family=family):
            cf = entity.campfire_entity_id or by_name.get(entity.name.lower())
            if not cf:
                continue
            shares: dict[str, Decimal] = {}
            for person_id, fraction in effective.get(entity.pk, {None: Decimal(1)}).items():
                name = people.get(person_id, NOT_HELD) if person_id else NOT_HELD
                shares[name] = shares.get(name, ZERO) + fraction
            out[cf] = shares
    return out


def income_statement(client: CampfireClient, scope: Scope, start: date, end: date, columns: str, fair: FairValues | None = None) -> Statement:
    data, keys, cadence = _fetch(client, INCOME_STATEMENT_PATH, scope, start, end, columns)
    rows = data.get("income_statement", [])
    show = [] if columns == "total" else keys
    value = lambda r: [_dec(r.get(f"{k}_Total")) for k in show] + [_dec(r.get("Total"))]  # noqa: E731
    out = Statement(scope.names["is"], [_label(k, cadence, False, end) for k in show] + ["Total"])
    blank: list[Decimal | None] = [None] * (len(show) + 1)
    sections = (("REVENUE", "Fee and operating income", "Total fee and operating income"), ("COGS", "Direct costs", "Total direct costs"),
                ("OTHER_INCOME", "Investment and other income", "Total investment and other income"),
                ("OPERATING_EXPENSES", "Expenses", "Total expenses"), ("OTHER_EXPENSE", "Interest, taxes and other", "Total interest, taxes and other"))
    for key, title, total in sections:
        accounts = [r for r in _account_rows(rows, [key], value) if _nonzero(r)]
        if not accounts:
            continue
        out.rows.append(Row(title, blank, "section"))
        out.rows += accounts
        out.rows.append(Row(total, value(_group(rows, [key])), "total"))
    net_income = value(_group(rows, ["NET_INCOME"]))
    out.rows.append(Row(scope.names["result"], net_income, "total"))
    out.rows += _net_worth_bridge(client, scope, start, end, net_income[-1] or ZERO, len(show), fair)
    return out


def _net_worth_bridge(client: CampfireClient, scope: Scope, start: date, end: date, net_income: Decimal, blanks: int,
                      fair: FairValues | None = None) -> list[Row]:
    """From income less expenses to the change in net worth: contributions and distributions (movements on the equity
    accounts), bank lines not categorized yet, then net worth at the start and the end. For the whole period only."""
    data = client.get_json(TRIAL_BALANCE_PATH, params={**scope.params(), "start_date": start.isoformat(), "end_date": end.isoformat()})
    accounts = [a for a in data.get("trial_balance", {}).get("accounts", []) if a.get("id") != "total"]
    net = lambda b: _dec((b or {}).get("debits")) - _dec((b or {}).get("credits"))  # noqa: E731  (debit positive)
    pad: list[Decimal | None] = [None] * blanks
    opening = closing = uncategorized = booked_start = booked_end = ZERO
    movements: list[Row] = []
    for a in sorted(accounts, key=lambda a: str(a.get("number") or "~")):
        kind = a.get("account_type")
        if kind in ("ASSET", "LIABILITY"):
            opening += net(a.get("beginning_balances"))
            closing += net(a.get("ending_balances"))
            if fair is not None and str(a.get("id")).isdigit() and int(a["id"]) in fair.booked_accounts:
                booked_start += net(a.get("beginning_balances"))
                booked_end += net(a.get("ending_balances"))
        elif kind == "EQUITY":
            change = -net(a.get("activity"))  # a credit to an equity account adds to net worth
            if change:
                movements.append(Row(a.get("name") or "", [*pad, change], "account", str(a.get("number") or ""), 1,
                                     int(a["id"]) if str(a.get("id")).isdigit() else None))
        elif kind and kind.startswith("UNCATEGORIZED"):
            uncategorized += -net(a.get("activity"))
    out: list[Row] = []
    if movements:
        out.append(Row("Contributions and distributions", [*pad, None], "section"))
        out += movements
        out.append(Row("Total contributions and distributions", [*pad, sum((m.values[-1] or ZERO for m in movements), ZERO)], "total"))
    if uncategorized:
        out.append(Row("Bank lines not categorized yet", [*pad, uncategorized], "account"))
    other = closing - opening - net_income - sum((m.values[-1] or ZERO for m in movements), ZERO) - uncategorized
    if abs(other) >= Decimal("0.01"):
        out.append(Row("Other changes", [*pad, other], "account"))
    names = scope.names
    if fair is not None and scope.fair_value:  # at fair value: what the investment records add to the books
        adjust_start, adjust_end = fair.start - booked_start, fair.end - booked_end
        tax_start, tax_end = (fair.tax(fair.start), fair.tax(fair.end)) if scope.kind == FAMILY else (ZERO, ZERO)
        if adjust_end != adjust_start:
            out.append(Row("Change in unrealized appreciation not in the books", [*pad, adjust_end - adjust_start], "account"))
        if tax_end != tax_start:
            out.append(Row("Change in estimated income taxes", [*pad, tax_start - tax_end], "account"))
        opening, closing = opening + adjust_start - tax_start, closing + adjust_end - tax_end
    out.append(Row(names["change"], [*pad, closing - opening], "total"))
    out.append(Row(f"{names['equity']}, {start:%b %-d, %Y}", [*pad, opening], "account"))
    out.append(Row(f"{names['equity']}, {end:%b %-d, %Y}", [*pad, closing], "grand"))
    return out


def cash_flow(client: CampfireClient, scope: Scope, start: date, end: date, columns: str) -> Statement:
    data, keys, cadence = _fetch(client, CASH_FLOW_PATH, scope, start, end, columns)
    show = [] if columns == "total" else keys
    out = Statement(scope.names["cf"], [_label(k, cadence, False, end) for k in show] + ["Total"])
    labels = {"OPERATING": "Cash from operations", "INVESTING": "Cash from investing", "FINANCING": "Cash from financing",
              "TOTAL_NET_CASH_FLOW": "Net change in cash", "CASH_BALANCE_BEGINNING_PERIOD": "Cash at the start",
              "CASH_BALANCE_ENDING_PERIOD": "Cash at the end"}
    for r in data.get("cash_flow", []):
        hierarchy = r.get("hierarchy") or []
        activity = r.get("transaction_activity") or {}
        values: list[Decimal | None] = [_dec(activity.get(k)) for k in show] + [_dec(r.get("Total"))]
        if len(hierarchy) == 1:
            kind = "grand" if hierarchy[0].startswith(("TOTAL", "CASH_BALANCE_ENDING")) else "total"
            out.rows.append(Row(labels.get(hierarchy[0], r.get("account_name") or ""), values, kind))
        elif hierarchy[-1] != "OPERATING_CASH_FLOW" or any(values):  # Campfire's empty "adjustments" line
            account_id = int(hierarchy[-1]) if str(hierarchy[-1]).isdigit() else None
            out.rows.append(Row(r.get("account_name") or "", values, "account", str(r.get("account_number") or ""), len(hierarchy) - 1, account_id))
    out.note = "Indirect method, as Campfire classifies each account's cash flow."
    return out


def trial_balance(client: CampfireClient, scope: Scope, start: date, end: date) -> Statement:
    data = client.get_json(TRIAL_BALANCE_PATH, params={**scope.params(), "start_date": start.isoformat(), "end_date": end.isoformat()})
    out = Statement(scope.names["tb"], ["Opening", "Debits", "Credits", "Closing debit", "Closing credit"])

    def values(a: dict[str, Any]) -> list[Decimal | None]:
        b, act, e = a.get("beginning_balances") or {}, a.get("activity") or {}, a.get("ending_balances") or {}
        return [_dec(b.get("debits")) - _dec(b.get("credits")), _dec(act.get("debits")), _dec(act.get("credits")), _dec(e.get("debits")), _dec(e.get("credits"))]

    accounts = [a for a in data.get("trial_balance", {}).get("accounts", []) if a.get("id") != "total"]
    for a in sorted(accounts, key=lambda a: (str(a.get("number") or "~"), str(a.get("name")))):
        row = Row(a.get("name") or "", values(a), "account", str(a.get("number") or ""), 0, int(a["id"]) if str(a.get("id")).isdigit() else None)
        if _nonzero(row):
            out.rows.append(row)
    total = next((a for a in data.get("trial_balance", {}).get("accounts", []) if a.get("id") == "total"), None)
    if total:
        out.rows.append(Row("Total", [None, *values(total)[1:]], "grand"))
    return out


def build(client: CampfireClient, view: str, scope: Scope, start: date, end: date, columns: str, fair: FairValues | None = None) -> Statement:
    if view == "is":
        return income_statement(client, scope, start, end, columns, fair)
    if view == "cf":
        return cash_flow(client, scope, start, end, columns)
    if view == "tb":
        return trial_balance(client, scope, start, end)
    return balance_sheet(client, scope, start, end, columns, fair)


def tax_basis_report(scope: Scope, holdings: list[tuple[str, str, Decimal, Decimal]], tax_rate: Decimal) -> Statement:
    """Tax basis against fair value by holding: (entity, holding, cost, fair value) rows, grouped by entity."""
    out = Statement(scope.names["tx"], ["Tax basis (cost)", "Fair value", "Unrealized gain (loss)", "Estimated tax"])
    rate = FairValues(tax_rate=tax_rate)
    totals = [ZERO, ZERO, ZERO]
    for entity in sorted({h[0] for h in holdings}):
        out.rows.append(Row(entity, [None, None, None, None], "section"))
        sub = [ZERO, ZERO, ZERO]
        for _, name, cost, value in sorted((h for h in holdings if h[0] == entity), key=lambda h: h[1]):
            out.rows.append(Row(name, [cost, value, value - cost, None], "account", level=1))
            sub = [sub[0] + cost, sub[1] + value, sub[2] + value - cost]
        out.rows.append(Row(f"Total {entity}", [*sub, None], "total"))
        totals = [t + x for t, x in zip(totals, sub)]
    out.rows.append(Row("Total", [*totals, rate.tax(totals[2]) if tax_rate else None], "grand"))
    out.note = ("Cost is the tax basis the investment records carry; fair value uses the latest accepted marks and prices. "
                + (f"Estimated tax is {tax_rate.normalize():f}% of the net unrealized gain." if tax_rate else "Set an estimated tax rate to see the tax on the unrealized gain."))
    return out


@dataclass
class LedgerLine:
    posted: str
    entity: str
    memo: str
    description: str
    journal: str
    kind: str
    debit: Decimal
    credit: Decimal
    balance: Decimal | None


def ledger(client: CampfireClient, account_id: int, scope: Scope, start: date, end: date, limit: int = 500) -> tuple[str, list[LedgerLine], int]:
    """The ledger lines of one account in the period: (account label, lines oldest first, how many there are in all)."""
    params: dict[str, Any] | None = {**scope.params(), "account": account_id, "start_date": start.isoformat(), "end_date": end.isoformat(),
                                     "limit": 200, "ordering": "posted_at"}
    path, lines, total, label = LEDGER_PATH, [], 0, ""
    while path and len(lines) < limit:
        page = client.get_json(path, params=params)
        total = page.get("count", total)
        for t in page.get("results", []):
            if t.get("is_deleted"):
                continue
            label = label or str(t.get("account_name") or "")
            lines.append(LedgerLine(str(t.get("posted_at") or "")[:10], t.get("entity_name") or "", t.get("journal_memo") or "",
                                    t.get("bank_description") or t.get("description") or "", str(t.get("journal_order") or t.get("journal") or ""),
                                    t.get("journal_type_name") or "", _dec(t.get("debit_amount")), _dec(t.get("credit_amount")),
                                    _dec(t["balance_after_transaction"]) if t.get("balance_after_transaction") is not None and not scope.rollup else None))
        nxt = page.get("next")
        path, params = (nxt.split(".com", 1)[-1], None) if nxt else ("", None)
    lines.sort(key=lambda ln: (ln.posted, ln.journal))
    return label, lines, total
