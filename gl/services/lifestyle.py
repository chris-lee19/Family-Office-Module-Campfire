"""The Lifestyle Expense Report: household and family spending by category, for each family member or each property.

Spending is tagged in the books with two Campfire tag groups the app sets up: "FO Family Member" (who it was for) and
"FO Property" (which home or property). Campfire's income statement split by a tag group (`group_by=tag`) gives each
expense account's spending per tag, and "Other" for what carries no tag of that group. The report shows the
categories (expense accounts) that have tagged spending; their untagged part is "Not assigned", and spending in
other categories (trustee fees, grants and the like) is stated below, so nothing is hidden.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from campfire_integration.client import CampfireClient
from campfire_integration.models import Workspace
from gl.models import Budget, DimensionTag
from gl.services import budget as budget_svc
from gl.services import statements as st
from gl.services.setup import SPENDING_DIMENSIONS, TAG_GROUP_PATH, TAG_GROUPS, _all, setup_dimensions

ZERO = Decimal(0)
VIEWS = {"person": "By family member", "property": "By property"}
NOT_ASSIGNED = "Not assigned"
OTHER = "Other"  # Campfire's column for what carries no tag of the group


@dataclass
class Category:
    label: str
    number: str
    account_id: int
    values: dict[str, Decimal]  # column -> amount
    budget: Decimal | None = None

    @property
    def total(self) -> Decimal:
        return sum(self.values.values(), ZERO)

    @property
    def better(self) -> Decimal | None:
        return None if self.budget is None else self.budget - self.total


@dataclass
class Lifestyle:
    by: str
    start: date
    end: date
    columns: list[str] = field(default_factory=list)
    categories: list[Category] = field(default_factory=list)
    other_spending: Decimal = ZERO  # expenses in categories nobody tagged
    budgeted: bool = False
    budget_months: tuple[int, int] = (0, 0)  # the whole months the budget column covers (1..12)

    @property
    def column_totals(self) -> list[Decimal]:
        return [sum((c.values.get(col, ZERO) for c in self.categories), ZERO) for col in self.columns]

    @property
    def total(self) -> Decimal:
        return sum((c.total for c in self.categories), ZERO)

    @property
    def budget(self) -> Decimal | None:
        return sum((c.budget or ZERO for c in self.categories), ZERO) if self.budgeted else None

    @property
    def assigned(self) -> Decimal:
        return sum((t for col, t in zip(self.columns, self.column_totals) if col != NOT_ASSIGNED), ZERO)


def group_ids(client: CampfireClient) -> dict[str, int]:
    """The spending tag groups in Campfire, by dimension (only those that exist)."""
    groups = {g.get("name"): int(g["id"]) for g in _all(client, TAG_GROUP_PATH) if not g.get("is_deleted")}
    return {d: groups[TAG_GROUPS[d]] for d in SPENDING_DIMENSIONS if TAG_GROUPS[d] in groups}


def is_set_up(workspace: Workspace) -> bool:
    configured = set(DimensionTag.objects.filter(workspace=workspace, dimension__in=SPENDING_DIMENSIONS).values_list("dimension", flat=True))
    return configured == set(SPENDING_DIMENSIONS)


def set_up(workspace: Workspace, client: CampfireClient) -> int:
    """Create (or find) the two tag groups and a tag per family member and property. Returns how many values were added."""
    steps = setup_dimensions(workspace, client, create=True, only=SPENDING_DIMENSIONS)
    return sum(int(s.label.split()[0]) for s in steps if s.label.split()[0].isdigit())


def choices(workspace: Workspace) -> dict[str, list[tuple[int, str]]]:
    """The tags a bill, an entry or a bank line can carry: {"person": [(tag id, name)], "property": [...]}."""
    out: dict[str, list[tuple[int, str]]] = {"person": [], "property": []}
    for t in DimensionTag.objects.filter(workspace=workspace, dimension__in=SPENDING_DIMENSIONS).order_by("label"):
        out[t.dimension].append((t.campfire_tag_id, t.label))
    return out


def labels(workspace: Workspace) -> dict[int, str]:
    """Spending tag id -> name, to show what a bill or an entry was tagged with."""
    return {t: name for values in choices(workspace).values() for t, name in values}


def picked(workspace: Workspace, data: Any) -> list[int]:
    """The spending tags chosen on a form (fields tag_person, tag_property), checked against the workspace's own."""
    allowed = {tag for values in choices(workspace).values() for tag, _ in values}
    wanted = [data.get("tag_person"), data.get("tag_property")]
    return [int(v) for v in wanted if str(v or "").isdigit() and int(v) in allowed]


def report(workspace: Workspace, client: CampfireClient, scope: st.Scope, start: date, end: date, by: str) -> Lifestyle | None:
    """Spending by category for each family member (or property), for one entity or a consolidated group. None
    when the tag group isn't in Campfire yet."""
    group = group_ids(client).get(by)
    if group is None:
        return None
    data = client.get_json(st.INCOME_STATEMENT_PATH, params={**scope.params(), "start_date": start.isoformat(), "end_date": end.isoformat(),
                                                             "cadence": "yearly", "group_by": "tag", "group": group})
    headers = {k: v for k, v in (data.get("headers") or {}).items() if k != "Total"}
    out = Lifestyle(by, start, end)
    for r in budget_svc.leaf_rows(data.get("income_statement", [])):
        if r["hierarchy"][0] not in budget_svc.EXPENSE_TYPES:
            continue
        values: dict[str, Decimal] = {}
        for key, names in headers.items():
            for name in names:
                if name != "Total" and r.get(f"{key}_{name}"):
                    column = NOT_ASSIGNED if name == OTHER else name
                    values[column] = values.get(column, ZERO) + st._dec(r.get(f"{key}_{name}"))
        if any(v for k, v in values.items() if k != NOT_ASSIGNED):
            out.categories.append(Category(str(r.get("display_name") or r.get("account_name") or ""), str(r.get("account_number") or ""),
                                            int(r["hierarchy"][-1]), values))
        else:
            out.other_spending += sum(values.values(), ZERO)
    totals: dict[str, Decimal] = {}
    for c in out.categories:
        for col, v in c.values.items():
            totals[col] = totals.get(col, ZERO) + v
    out.columns = sorted((c for c in totals if c != NOT_ASSIGNED and totals[c]), key=lambda c: -totals[c])
    if totals.get(NOT_ASSIGNED):
        out.columns.append(NOT_ASSIGNED)
    out.categories.sort(key=lambda c: -c.total)
    _add_budget(workspace, out, scope)
    return out


def _add_budget(workspace: Workspace, out: Lifestyle, scope: st.Scope) -> None:
    """The budget for each category over the same months, when the period falls in one year that has a budget."""
    if out.start.year != out.end.year:
        return
    budget: Budget | None = budget_svc.in_use(workspace, out.start.year)
    planned = budget_svc.budget_by_account(budget, scope.members or (scope.entity_id,))
    if not planned:
        return
    # Whole months only: a month counts when the period takes in all of it (year to date on the 14th: through last month).
    first = out.start.month if out.start.day == 1 else out.start.month + 1
    last = out.end.month if out.end == st.month_end(out.end) else out.end.month - 1
    if first > last:
        return
    out.budgeted, out.budget_months = True, (first, last)
    for c in out.categories:
        _, months = planned.get(c.account_id, (None, [ZERO] * 12))
        c.budget = sum(months[first - 1:last], ZERO)


def chart(lifestyle: Lifestyle) -> dict[str, Any]:
    """Two ranked bar charts: spending by column (family member or property) and by category."""
    from gl.services.charts import ranked

    people = ranked([(col, t) for col, t in zip(lifestyle.columns, lifestyle.column_totals) if col != NOT_ASSIGNED])
    categories = ranked([(c.label, c.total) for c in lifestyle.categories])
    return {"columns": people, "categories": categories}
