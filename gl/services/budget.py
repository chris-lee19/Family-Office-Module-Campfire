"""Budgets, and the Budget vs. Actual report.

A budget is kept in the app (Campfire's budgets aren't reachable with an app connection: there is no budgets
permission): for a year, an amount per Campfire entity, GL account and month. Versions: an approved budget doesn't
change; revising it starts a new draft version, approved in its turn by someone other than who made it (when the
workspace asks for a second person). The budget a report uses is the year's latest approved version, or the latest
draft while none is approved yet.

Actuals are Campfire's: its income statement by month for one entity or a consolidated group. Income and expenses
are both positive, as on the income statement; "better (worse)" is how far the actual is on the good side of the
budget: more income, or less spending.
"""

import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.utils import timezone

from campfire_integration.client import CampfireClient
from campfire_integration.models import Workspace
from gl.models import Budget, BudgetLine
from gl.services import statements as st
from records.models import AuditEntry, ControlSettings

ZERO = Decimal(0)
INCOME_TYPES = ("REVENUE", "OTHER_INCOME")
EXPENSE_TYPES = ("COGS", "OPERATING_EXPENSES", "OTHER_EXPENSE")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
SPREADS = {"even": "Evenly over the year", "quarterly": "At each quarter end", "once": "All in one month"}
FLAG_SHARE, FLAG_FLOOR = Decimal("0.10"), Decimal(1000)  # a variance worth flagging: 10% of the budget and at least $1,000
NOT_BUDGETED = "Not budgeted"
S = Budget.Status


class BudgetError(Exception):
    pass


def _dec(v: Any) -> Decimal:
    try:
        return Decimal(str(v if v not in (None, "") else 0).replace(",", "").replace("$", "").strip()).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise BudgetError(f"'{v}' isn't an amount.") from None


def _round(v: Decimal, to: int = 100) -> Decimal:
    """Budgets are round numbers: to the nearest hundred (or dollar, for small amounts)."""
    step = Decimal(to) if abs(v) >= to * 10 else Decimal(1)
    return ((v / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step).quantize(Decimal("0.01"))


def spread(annual: Decimal, how: str, month: int = 12) -> list[Decimal]:
    """An annual amount over twelve months, to the cent: evenly (what rounding leaves goes to December), at each
    quarter end, or all in one month."""
    if how == "quarterly":
        q = (annual / 4).quantize(Decimal("0.01"))
        return [q if m in (3, 6, 9) else (annual - 3 * q if m == 12 else ZERO) for m in range(1, 13)]
    if how == "once":
        return [annual if m == month else ZERO for m in range(1, 13)]
    each = (annual / 12).quantize(Decimal("0.01"))
    return [each] * 11 + [annual - each * 11]


# ---- which budget ----

def budgets(workspace: Workspace, year: int) -> list[Budget]:
    return list(Budget.objects.filter(workspace=workspace, year=year).order_by("-version"))


def in_use(workspace: Workspace, year: int) -> Budget | None:
    """The budget reports use: the latest approved version, or the latest draft while none is approved."""
    versions = budgets(workspace, year)
    return next((b for b in versions if b.status == S.APPROVED), versions[0] if versions else None)


def draft(workspace: Workspace, year: int) -> Budget | None:
    """The version being worked on: a draft newer than the approved one."""
    versions = budgets(workspace, year)
    return versions[0] if versions and versions[0].is_draft else None


def years(workspace: Workspace, today: date) -> list[int]:
    have = set(Budget.objects.filter(workspace=workspace).values_list("year", flat=True))
    return sorted(have | {today.year, today.year + 1}, reverse=True)


# ---- the accounts a budget can hold ----

@dataclass(frozen=True)
class PLAccount:
    id: int
    number: str
    name: str
    income: bool

    @property
    def label(self) -> str:
        return f"{self.number} {self.name}".strip()


def pl_accounts(client: CampfireClient) -> list[PLAccount]:
    """The chart's income and expense accounts."""
    from gl.services.setup import list_accounts

    return [PLAccount(int(a["id"]), str(a.get("number") or ""), str(a.get("name") or ""), a.get("account_type") in INCOME_TYPES)
            for a in list_accounts(client) if a.get("account_type") in INCOME_TYPES + EXPENSE_TYPES and a.get("is_active", True) is not False]


# ---- making and changing a budget ----

def _audit(budget: Budget, actor: str, action: str, what: str, before: str = "", after: str = "") -> None:
    from gl.services.dashboard import mark_stale

    mark_stale(budget.workspace)  # Home shows spending against the budget
    AuditEntry.objects.create(workspace=budget.workspace, actor=actor, action=action, kind="budgets", object_id=str(budget.pk),
                              object_repr=str(budget)[:255], changes={what: [before, after]})


def new_version(workspace: Workspace, year: int, actor: str, actor_id: str, note: str, lines: list[dict[str, Any]] | None = None,
                copy_from: Budget | None = None) -> Budget:
    """A new draft for the year: empty, from the given lines, or a copy of another version."""
    if draft(workspace, year) is not None:
        raise BudgetError(f"There is already a draft of the {year} budget: finish or discard it first.")
    last = Budget.objects.filter(workspace=workspace, year=year).order_by("-version").first()
    with transaction.atomic():
        budget = Budget.objects.create(workspace=workspace, year=year, version=(last.version + 1) if last else 1, note=note[:255],
                                       created_by=actor, created_by_id=actor_id)
        if copy_from is not None:
            lines = [{"entity_id": ln.campfire_entity_id, "entity": ln.entity_label, "account_id": ln.campfire_account_id, "number": ln.account_number,
                      "account": ln.account_label, "income": ln.income, "months": ln.amounts} for ln in copy_from.lines.all()]
        BudgetLine.objects.bulk_create([BudgetLine(budget=budget, campfire_entity_id=ln["entity_id"], entity_label=ln["entity"][:255],
                                                   campfire_account_id=ln["account_id"], account_number=str(ln.get("number") or "")[:40],
                                                   account_label=ln["account"][:255], income=bool(ln["income"]),
                                                   months=[str(Decimal(v).quantize(Decimal("0.01"))) for v in ln["months"]])
                                        for ln in lines or []])
        _audit(budget, actor, AuditEntry.Action.CREATED, "budget", "", f"version {budget.version}: {note}"[:255])
    return budget


def _editable(budget: Budget) -> None:
    if not budget.is_draft:
        raise BudgetError("An approved budget doesn't change. Revise it to start a new version.")


def save_line(budget: Budget, data: Any, names: dict[int, str], accounts: dict[int, PLAccount], actor: str) -> BudgetLine:
    """Add a line, or change one: twelve monthly amounts (m1..m12), or an annual amount and how to spread it."""
    _editable(budget)
    entity = int(data.get("entity")) if str(data.get("entity") or "").isdigit() else 0
    account = int(data.get("account")) if str(data.get("account") or "").isdigit() else 0
    if entity not in names or account not in accounts:
        raise BudgetError("Choose the entity and the account.")
    if str(data.get("annual") or "").strip():
        how = data.get("spread") if data.get("spread") in SPREADS else "even"
        month = int(data.get("month")) if str(data.get("month") or "").isdigit() and 1 <= int(data.get("month")) <= 12 else 12
        months = spread(_dec(data.get("annual")), how, month)
    else:
        months = [_dec(data.get(f"m{m}")) for m in range(1, 13)]
    a = accounts[account]
    line, made = BudgetLine.objects.get_or_create(budget=budget, campfire_entity_id=entity, campfire_account_id=account,
                                                  defaults={"entity_label": names[entity], "account_label": a.name, "account_number": a.number, "income": a.income})
    before = "" if made else f"{line.total:,.2f}"
    line.entity_label, line.account_label, line.account_number, line.income = names[entity][:255], a.name[:255], a.number[:40], a.income
    line.months = [str(v) for v in months]
    line.save()
    _audit(budget, actor, AuditEntry.Action.CREATED if made else AuditEntry.Action.UPDATED, f"{line.entity_label} · {a.label}"[:120], before, f"{line.total:,.2f}")
    return line


def remove_line(budget: Budget, pk: int, actor: str) -> BudgetLine:
    _editable(budget)
    line = budget.lines.filter(pk=pk).first()
    if line is None:
        raise BudgetError("That line isn't in this budget any more.")
    _audit(budget, actor, AuditEntry.Action.DELETED, f"{line.entity_label} · {line.account_label}"[:120], f"{line.total:,.2f}", "")
    line.delete()
    return line


def approve(budget: Budget, actor: str, actor_id: str) -> Budget:
    _editable(budget)
    if not budget.lines.exists():
        raise BudgetError("The budget has no lines yet.")
    if ControlSettings.for_workspace(budget.workspace).require_second_person and actor_id and actor_id == budget.created_by_id:
        raise BudgetError("You made this budget, so someone else has to approve it (preparer ≠ approver).")
    budget.status, budget.approved_by, budget.approved_by_id, budget.approved_at = S.APPROVED, actor, actor_id, timezone.now()
    budget.save()
    _audit(budget, actor, AuditEntry.Action.UPDATED, "status", "Draft", "Approved")
    return budget


def discard(budget: Budget, actor: str) -> None:
    _editable(budget)
    _audit(budget, actor, AuditEntry.Action.DELETED, "budget", f"version {budget.version}", "")
    budget.delete()


# ---- a starting point from the books ----

@dataclass
class Suggested:
    lines: list[dict[str, Any]]
    basis: str


def _by_entity(client: CampfireClient, start: date, end: date) -> dict[tuple[str, int], dict[int, Decimal]]:
    """Income and expenses by entity, account and month: {(entity name, account id): {month index 0..: amount}}."""
    out: dict[tuple[str, int], dict[int, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for scope in (s for s in st.scopes(client) if s.rollup):
        data = client.get_json(st.INCOME_STATEMENT_PATH, params={**scope.params(), "start_date": start.isoformat(), "end_date": end.isoformat(),
                                                                  "cadence": "monthly", "group_by": "entity"})
        for row in leaf_rows(data.get("income_statement", [])):
            for key, names in (data.get("headers") or {}).items():
                if key == "Total":
                    continue
                n = (date.fromisoformat(key).year - start.year) * 12 + date.fromisoformat(key).month - start.month
                for name in names:
                    if name != "Total" and row.get(f"{key}_{name}"):
                        out[(name, int(row["hierarchy"][-1]))][n] += st._dec(row.get(f"{key}_{name}"))
    return out


def leaf_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The income statement's account rows that have no accounts under them (so nothing is counted twice)."""
    accounts = [r for r in rows if (r.get("hierarchy") or [""])[0] in INCOME_TYPES + EXPENSE_TYPES and str(r["hierarchy"][-1]).isdigit()]
    parents = {tuple(r["hierarchy"][:-1]) for r in accounts}
    return [r for r in accounts if tuple(r["hierarchy"]) not in parents]


def suggest(client: CampfireClient, year: int, today: date, uplift: Decimal = ZERO) -> Suggested:
    """Each entity's income and expenses over the year before, month by month, as a starting budget. If the books
    don't cover that year, the last twelve complete months instead; months before an entity's books began take the
    average of the months since. Raised by `uplift` percent and rounded."""
    names = {s.label: s.entity_id for s in st.scopes(client) if not s.rollup}
    accounts = {a.id: a for a in pl_accounts(client)}
    start, end = date(year - 1, 1, 1), date(year - 1, 12, 31)
    moved = _by_entity(client, start, min(end, today))
    basis = f"{year - 1} as booked"
    if not moved or end >= today:
        end = st.month_start(today) - timedelta(days=1)  # the last complete month, and the eleven before it
        start = date(end.year, 1, 1) if end.month == 12 else date(end.year - 1, end.month + 1, 1)
        moved = _by_entity(client, start, end)
        basis = f"the twelve months {start:%b %Y} to {end:%b %Y}"
    first: dict[str, int] = {}
    for (entity, _), months in moved.items():
        active = [n for n, v in months.items() if v]
        if active:
            first[entity] = min(first.get(entity, 12), min(active))
    factor = 1 + uplift / 100
    lines = []
    for (entity, account), months in sorted(moved.items()):
        if entity not in names or account not in accounts:
            continue
        since = first.get(entity, 0)
        booked = [months.get(n, ZERO) for n in range(12)]
        if since:
            average = sum(booked[since:], ZERO) / (12 - since)
            booked = [average] * since + booked[since:]
        # month index n is (start + n months); a budget is January to December
        by_calendar = [ZERO] * 12
        for n, v in enumerate(booked):
            by_calendar[(start.month - 1 + n) % 12] += v
        values = [_round(v * factor) for v in by_calendar]
        if any(values):
            a = accounts[account]
            lines.append({"entity_id": names[entity], "entity": entity, "account_id": a.id, "number": a.number, "account": a.name, "income": a.income,
                          "months": values})
    if uplift:
        basis += f", plus {uplift.normalize():f}%"
    return Suggested(lines, basis)


# ---- spreadsheet ----

HEADER = ["Entity", "Account number", "Account", *MONTHS, "Total"]


def to_xlsx(budget: Budget) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = f"Budget {budget.year}"
    ws.append(HEADER)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for ln in budget.lines.all():
        ws.append([ln.entity_label, ln.account_number, ln.account_label, *[float(v) for v in ln.amounts], float(ln.total)])
    for col, width in zip("ABC", (34, 14, 36)):
        ws.column_dimensions[col].width = width
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def from_xlsx(budget: Budget, content: bytes, names: dict[int, str], accounts: list[PLAccount], actor: str) -> int:
    """Replace a draft's lines with a spreadsheet laid out like the download: entity, account number, account,
    January to December. Entities and accounts are matched by name (an account also by its number). All or nothing."""
    from openpyxl import load_workbook

    _editable(budget)
    try:
        ws = load_workbook(io.BytesIO(content), read_only=True, data_only=True).worksheets[0]
    except Exception:  # noqa: BLE001  (anything openpyxl can't read)
        raise BudgetError("That file isn't a spreadsheet the app can read (.xlsx).") from None
    by_entity = {name.lower(): pk for pk, name in names.items()}
    by_number = {a.number: a for a in accounts if a.number}
    by_name = {a.name.lower(): a for a in accounts}
    rows, problems = [], []
    for n, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row or not any(row):
            continue
        entity, number, name = (str(row[i] or "").strip() if i < len(row) else "" for i in range(3))
        account = by_number.get(number) or by_name.get(name.lower())
        if entity.lower() not in by_entity:
            problems.append(f"row {n}: no entity named '{entity}'")
            continue
        if account is None:
            problems.append(f"row {n}: no income or expense account '{number} {name}'".replace("  ", " "))
            continue
        try:
            months = [_dec(row[3 + m] if 3 + m < len(row) else 0) for m in range(12)]
        except BudgetError as e:
            problems.append(f"row {n}: {e}")
            continue
        rows.append((by_entity[entity.lower()], account, months))
    if problems:
        raise BudgetError("Nothing was changed. " + "; ".join(problems[:6]) + (f"; and {len(problems) - 6} more" if len(problems) > 6 else "") + ".")
    if not rows:
        raise BudgetError("The spreadsheet has no lines below its heading row.")
    merged: dict[tuple[int, int], tuple[PLAccount, list[Decimal]]] = {}
    for entity, account, months in rows:  # the same entity and account twice: added together
        prior = merged.get((entity, account.id))
        merged[(entity, account.id)] = (account, [a + b for a, b in zip(prior[1], months)] if prior else months)
    with transaction.atomic():
        budget.lines.all().delete()
        BudgetLine.objects.bulk_create([BudgetLine(budget=budget, campfire_entity_id=e, entity_label=names[e][:255], campfire_account_id=a.id,
                                                   account_number=a.number[:40], account_label=a.name[:255], income=a.income, months=[str(v) for v in m])
                                        for (e, _), (a, m) in merged.items()])
        _audit(budget, actor, AuditEntry.Action.UPDATED, "lines", "", f"{len(merged)} lines from a spreadsheet")
    return len(merged)


# ---- Budget vs. Actual ----

@dataclass
class Row:
    label: str
    number: str
    account_id: int | None
    income: bool
    actual: list[Decimal]  # twelve months
    budget: list[Decimal]
    month: int  # the report's month, 1..12

    def _better(self, actual: Decimal, budget: Decimal) -> Decimal:
        return actual - budget if self.income else budget - actual

    @property
    def m_actual(self) -> Decimal:
        return self.actual[self.month - 1]

    @property
    def m_budget(self) -> Decimal:
        return self.budget[self.month - 1]

    @property
    def m_better(self) -> Decimal:
        return self._better(self.m_actual, self.m_budget)

    @property
    def ytd_actual(self) -> Decimal:
        return sum(self.actual[:self.month], ZERO)

    @property
    def ytd_budget(self) -> Decimal:
        return sum(self.budget[:self.month], ZERO)

    @property
    def ytd_better(self) -> Decimal:
        return self._better(self.ytd_actual, self.ytd_budget)

    @property
    def ytd_pct(self) -> Decimal | None:
        """How much of the year-to-date budget the actual is (100 = on budget)."""
        return (self.ytd_actual / self.ytd_budget * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP) if self.ytd_budget else None

    @property
    def year_budget(self) -> Decimal:
        return sum(self.budget, ZERO)

    @property
    def outlook(self) -> Decimal:
        """Where the year is heading: the actual so far, and the budget for the months still to come."""
        return self.ytd_actual + sum(self.budget[self.month:], ZERO)

    @property
    def outlook_better(self) -> Decimal:
        return self._better(self.outlook, self.year_budget)

    @property
    def flag(self) -> str:
        """Over budget (spending), Below budget (income), Not budgeted, or nothing."""
        worse = -self.ytd_better
        if not self.ytd_budget and not self.year_budget:
            return NOT_BUDGETED if abs(self.ytd_actual) >= FLAG_FLOOR else ""
        if worse >= max(FLAG_FLOOR, abs(self.ytd_budget) * FLAG_SHARE):
            return "Below budget" if self.income else "Over budget"
        return ""


@dataclass
class Comparison:
    scope: st.Scope
    year: int
    month: int
    budget: Budget | None
    income: list[Row] = field(default_factory=list)
    expenses: list[Row] = field(default_factory=list)

    @property
    def month_label(self) -> str:
        return f"{date(self.year, self.month, 1):%B %Y}"

    @property
    def flagged(self) -> list[Row]:
        return sorted((r for r in self.income + self.expenses if r.flag), key=lambda r: r.ytd_better)

    def total(self, rows: list[Row], label: str, income: bool) -> Row:
        add = lambda attr: [sum((getattr(r, attr)[m] for r in rows), ZERO) for m in range(12)]  # noqa: E731
        return Row(label, "", None, income, add("actual"), add("budget"), self.month)

    @property
    def total_income(self) -> Row:
        return self.total(self.income, "Total income", True)

    @property
    def total_expenses(self) -> Row:
        return self.total(self.expenses, "Total expenses", False)

    @property
    def net(self) -> Row:
        i, e = self.total_income, self.total_expenses
        return Row("Excess of income over expenses", "", None, True, [a - b for a, b in zip(i.actual, e.actual)],
                   [a - b for a, b in zip(i.budget, e.budget)], self.month)


def budget_by_account(budget: Budget | None, members: tuple[int, ...]) -> dict[int, tuple[BudgetLine, list[Decimal]]]:
    """The budget for a scope, by account: the lines of its entities added together."""
    out: dict[int, tuple[BudgetLine, list[Decimal]]] = {}
    if budget is None:
        return out
    for ln in budget.lines.filter(campfire_entity_id__in=members):
        prior = out.get(ln.campfire_account_id)
        out[ln.campfire_account_id] = (ln, [a + b for a, b in zip(prior[1], ln.amounts)] if prior else ln.amounts)
    return out


def compare(workspace: Workspace, client: CampfireClient, scope: st.Scope, year: int, month: int) -> Comparison:
    """Budget vs. actual for one entity or a consolidated group, for the year through the given month."""
    budget = in_use(workspace, year)
    out = Comparison(scope, year, month, budget)
    end = st.month_end(date(year, month, 1))
    data = client.get_json(st.INCOME_STATEMENT_PATH, params={**scope.params(), "start_date": date(year, 1, 1).isoformat(), "end_date": end.isoformat(),
                                                             "cadence": "monthly"})
    actual: dict[int, tuple[dict[str, Any], list[Decimal]]] = {}
    for r in leaf_rows(data.get("income_statement", [])):
        values = [ZERO] * 12
        for key in data.get("headers") or {}:
            if key != "Total" and key[:4] == str(year):
                values[int(key[5:7]) - 1] = st._dec(r.get(f"{key}_Total"))
        actual[int(r["hierarchy"][-1])] = (r, values)
    planned = budget_by_account(budget, scope.members or (scope.entity_id,))
    for account_id in set(actual) | set(planned):
        r, values = actual.get(account_id, ({}, [ZERO] * 12))
        line, budgeted = planned.get(account_id, (None, [ZERO] * 12))
        income = r.get("hierarchy", [""])[0] in INCOME_TYPES if r else bool(line and line.income)
        label = (r.get("display_name") or r.get("account_name")) if r else line.account_label  # type: ignore[union-attr]
        number = str((r.get("account_number") if r else line.account_number) or "")  # type: ignore[union-attr]
        row = Row(str(label or ""), number, account_id, income, values, budgeted, month)
        if any(row.actual) or any(row.budget):
            (out.income if income else out.expenses).append(row)
    for rows in (out.income, out.expenses):
        rows.sort(key=lambda r: (r.number or "~", r.label))
    return out


def chart(comparison: Comparison, limit: int = 12) -> list[dict[str, Any]]:
    """Spending by category against the budget so far: a bar for the actual, a mark for the budget, on one scale.
    The largest categories (by budget or actual, whichever is more), in the order of the report."""
    rows = sorted(comparison.expenses, key=lambda r: -max(r.ytd_actual, r.ytd_budget))[:limit]
    most = max((max(r.ytd_actual, r.ytd_budget) for r in rows), default=ZERO)
    if not most:
        return []
    width = lambda v: float((max(v, ZERO) / most * 100).quantize(Decimal("0.1")))  # noqa: E731
    return [{"label": r.label, "actual": r.ytd_actual, "budget": r.ytd_budget, "better": r.ytd_better, "flag": r.flag, "pct": r.ytd_pct,
             "w_actual": width(r.ytd_actual), "w_budget": width(r.ytd_budget), "over": r.ytd_actual > r.ytd_budget,
             "w_within": width(min(r.ytd_actual, r.ytd_budget))} for r in sorted(rows, key=lambda r: (r.number or "~", r.label))]


def summary(workspace: Workspace, client: CampfireClient, scope: st.Scope, today: date) -> dict[str, Any] | None:
    """For Home: spending against the budget for the year so far (through the last complete month), and what is
    over. None when there is no budget for the year."""
    month_end = st.month_start(today) - timedelta(days=1)
    if month_end.year != today.year or in_use(workspace, today.year) is None:
        return None
    c = compare(workspace, client, scope, today.year, month_end.month)
    spent = c.total_expenses
    return {"year": c.year, "month": c.month_label, "actual": str(spent.ytd_actual), "budget": str(spent.ytd_budget), "better": str(spent.ytd_better),
            "pct": str(spent.ytd_pct) if spent.ytd_pct is not None else "", "draft": bool(c.budget and c.budget.is_draft),
            "over": [{"label": r.label, "better": str(r.ytd_better), "flag": r.flag} for r in c.flagged[:4]]}
