"""The family reporting pack: one document for the principals, for a chosen period, made of the reports the app
already produces. Each section is built by the same service as its page, so the pack and the pages agree.

Sections that read Campfire's books fail on their own: if the books can't be read for one, the pack says so in that
section and carries on with the rest.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import requests

from campfire_integration.client import CampfireClient, CampfireError, NotConnected
from campfire_integration.models import Workspace
from entities.models import Family
from entities.services import structure
from gl.models import AccountMapping, GLSettings, Role
from gl.services import budget as budget_svc
from gl.services import dashboard, fairvalue, lifestyle, liquidity, partnership, receipts
from gl.services import statements as st
from investments.models import AssetClass
from investments.services import performance
from investments.services.portfolio import build_portfolio

ZERO = Decimal(0)
CAMPFIRE_ERRORS = (CampfireError, NotConnected, requests.RequestException)
# (key, title, what it is, needs the long side of the paper)
SECTIONS = (
    ("glance", "At a glance", "Net worth, how it moved, and where it sits", False),
    ("net_worth", "Statement of Net Worth", "Assets and liabilities at estimated current value", False),
    ("by_entity", "Statement of Net Worth by entity", "The same statement, a column per entity", True),
    ("by_person", "Statement of Net Worth by family member", "Each person's share, traced through the ownership chain", True),
    ("changes", "Statement of Changes in Net Worth", "From net worth at the start of the period to net worth at the end", False),
    ("receipts", "Statement of Cash Receipts and Disbursements", "Where cash came from and where it went", False),
    ("budget", "Budget vs. Actual", "The year so far against the budget", True),
    ("lifestyle", "Lifestyle Expense Report", "Household and family spending by family member", True),
    ("schedule", "Schedule of Investments", "Every holding with its cost and its value", True),
    ("performance", "Investment Performance Report", "Time-weighted returns, and IRR and multiples for private funds", True),
    ("liquidity", "Liquidity and Cash Flow Forecast", "Cash by month for the next twelve months", True),
    ("capital", "Capital Account Statements", "Partners' capital in the family's partnerships", True),
    ("structure", "Ownership Structure", "Who owns what", True),
)
KEYS = tuple(k for k, *_ in SECTIONS)


@dataclass
class Section:
    key: str
    title: str
    about: str
    wide: bool
    data: Any = None
    error: str = ""
    note: str = ""


@dataclass
class Pack:
    workspace: Workspace
    start: date
    end: date
    today: date
    scope: st.Scope | None = None
    sections: list[Section] = field(default_factory=list)

    @property
    def period(self) -> str:
        return f"{self.start:%B %-d, %Y} to {self.end:%B %-d, %Y}"


@dataclass
class ScheduleLine:
    holding: str
    owner: str
    account: str
    quantity: Decimal | None
    cost: Decimal
    value: Decimal
    valued: str
    stale: bool = False

    @property
    def gain(self) -> Decimal:
        return self.value - self.cost


def schedule(workspace: Workspace, as_of: date) -> dict[str, Any]:
    """Every holding in the investment records on a date, by asset class, with cost and value."""
    classes = {a.pk: a for a in AssetClass.objects.filter(workspace=workspace)}
    groups: dict[str, list[ScheduleLine]] = {}
    for family in Family.objects.filter(workspace=workspace, is_active=True):
        for h in build_portfolio(family, as_of).holdings:
            if h.instrument is None:
                name = "Cash & equivalents"
            else:
                ac = classes[h.instrument.asset_class_id]
                name = (classes.get(ac.parent_id, ac) if ac.parent_id else ac).name
            owner = h.account.entity.name if h.account.entity_id else f"{h.account.person.display_name} (individual)"
            valued = f"{h.method_label}{f', {h.value_date:%b %-d, %Y}' if h.value_date and h.instrument is not None else ''}"
            groups.setdefault(name, []).append(ScheduleLine(h.name, owner, h.account.name, h.quantity, h.cost_basis, h.market_value, valued, h.is_stale and h.instrument is not None))
    total = sum((ln.value for lines in groups.values() for ln in lines), ZERO)
    out = [{"name": name, "lines": sorted(lines, key=lambda ln: -ln.value), "cost": sum((ln.cost for ln in lines), ZERO),
            "value": sum((ln.value for ln in lines), ZERO), "share": (sum((ln.value for ln in lines), ZERO) / total * 100).quantize(Decimal("0.1")) if total else ZERO}
           for name, lines in groups.items()]
    for g in out:
        g["gain"] = g["value"] - g["cost"]
    out.sort(key=lambda g: -g["value"])
    cost = sum((g["cost"] for g in out), ZERO)
    return {"groups": out, "cost": cost, "value": total, "gain": total - cost, "count": sum(len(g["lines"]) for g in out)}


def build(workspace: Workspace, client: CampfireClient, start: date, end: date, today: date, wanted: tuple[str, ...] = KEYS) -> Pack:
    pack = Pack(workspace, start, end, today)
    families = list(Family.objects.filter(workspace=workspace, is_active=True))
    options: list[st.Scope] = []
    books_error = ""
    try:
        options = st.classify(workspace, st.scopes(client))
        pack.scope = next((s for s in options if s.rollup), None)
    except CAMPFIRE_ERRORS as e:
        books_error = f"Couldn't read the books from Campfire ({type(e).__name__}). Check that the workspace is connected."
    scope = pack.scope
    rate = GLSettings.for_workspace(workspace).estimated_tax_rate
    booked = frozenset(AccountMapping.objects.filter(workspace=workspace, role=Role.UNREALIZED).values_list("campfire_account_id", flat=True))
    ids = {s.label: s.entity_id for s in options if not s.rollup}
    clearing = frozenset(AccountMapping.objects.filter(workspace=workspace, role__in=(Role.CASH_CLEARING, Role.TRANSFERS)).values_list("campfire_account_id", flat=True))

    def fair() -> st.FairValues:
        members = scope.members if scope else ()
        return st.FairValues(fairvalue.unrealized_for(workspace, members, start - timedelta(days=1)), fairvalue.unrealized_for(workspace, members, end), booked, rate)

    def glance() -> Any:
        from investments.views import breakdown

        by_class: dict[str, Decimal] = {}
        for f in families:
            for label, value in build_portfolio(f, end).by_asset_class.items():
                by_class[label] = by_class.get(label, ZERO) + value
        return {"nw": dashboard.net_worth(workspace, client, end), "allocation": breakdown(by_class, sum(by_class.values(), ZERO)), "investments": sum(by_class.values(), ZERO)}

    def liquidity_section() -> Any:
        from gl.liquidity_views import _totals, chart

        fc = liquidity.build(workspace, client, today, 12)
        return {"fc": fc, "chart": chart(fc), **_totals(fc)}

    def capital() -> Any:
        return [partnership.capital_accounts(client, e, end.year, end) for e in partnership.partnerships(workspace) if e.campfire_entity_id]

    def performance_section() -> Any:
        from investments.performance_views import growth_chart

        return [{"r": r, "growth": growth_chart(r)} for r in (performance.build(f, end) for f in families)]

    # Budget vs. Actual runs through the last whole month of the period (and the Lifestyle Expense Report with it).
    whole_end = end if end == st.month_end(end) else st.month_start(end) - timedelta(days=1)

    def budget_section() -> Any:
        from gl.budget_views import _rows

        if whole_end < date(end.year, 1, 1) or budget_svc.in_use(workspace, end.year) is None:
            return None
        c = budget_svc.compare(workspace, client, scope, end.year, whole_end.month)  # type: ignore[arg-type]
        return {"c": c, "rows": _rows(c, lambda account_id: ""), "bullets": budget_svc.chart(c)}

    def lifestyle_section() -> Any:
        if whole_end < start or not lifestyle.is_set_up(workspace):
            return None
        life = lifestyle.report(workspace, client, scope, start, whole_end, "person")  # type: ignore[arg-type]
        if life is None or not life.categories:
            return None
        from gl.services.charts import ranked

        return {"life": life, "life_rows": [(cat, [cat.values.get(col, ZERO) for col in life.columns], "") for cat in life.categories],
                "life_gap": (life.budget - life.total) if life.budgeted else None, "end": whole_end,
                "people": ranked([(col, t) for col, t in zip(life.columns, life.column_totals) if col != lifestyle.NOT_ASSIGNED])}

    builders: dict[str, tuple[bool, Any]] = {  # key -> (needs the books, how to build it)
        "glance": (True, glance),
        "net_worth": (True, lambda: st.balance_sheet(client, scope, start, end, "total", fair())),  # type: ignore[arg-type]
        "by_entity": (True, lambda: st.net_worth_split(client, scope, end, "entity", fairvalue.unrealized_by_entity(workspace, end), booked, rate)),  # type: ignore[arg-type]
        "by_person": (True, lambda: st.net_worth_split(client, scope, end, "person", fairvalue.unrealized_by_entity(workspace, end), booked, rate,  # type: ignore[arg-type]
                                                       st.look_through(workspace, end, ids))),
        "changes": (True, lambda: st.income_statement(client, scope, start, end, "total", fair())),  # type: ignore[arg-type]
        "receipts": (True, lambda: receipts.build(client, scope, start, end, "total", clearing)),  # type: ignore[arg-type]
        "budget": (True, budget_section),
        "lifestyle": (True, lifestyle_section),
        "schedule": (False, lambda: schedule(workspace, end)),
        "performance": (False, performance_section),
        "liquidity": (True, liquidity_section),
        "capital": (True, capital),
        "structure": (False, lambda: [(f, structure.build(f, end)) for f in families]),
    }
    for key, title, about, wide in SECTIONS:
        if key not in wanted:
            continue
        section = Section(key, title, about, wide)
        needs_books, make = builders[key]
        if needs_books and (books_error or scope is None):
            section.error = books_error or "No consolidated group of entities in Campfire yet."
        else:
            try:
                section.data = make()
            except CAMPFIRE_ERRORS as e:
                section.error = f"Couldn't read the books from Campfire for this section ({type(e).__name__})."
        if key == "liquidity" and today != end:
            section.note = f"The forecast always starts from today ({today:%B %-d, %Y})."
        if key == "capital" and section.data == []:
            section.error = "No partnership keeps books in Campfire yet."
        if key == "budget" and section.data is None and not section.error:
            section.error = f"There is no budget for {end.year} yet (Budget vs. actual -> Budget)."
        if key == "lifestyle" and section.data is None and not section.error:
            section.error = ("No spending in the period is tagged to a family member yet." if lifestyle.is_set_up(workspace)
                             else "Spending tags aren't set up yet (Budget vs. actual -> Lifestyle Expense Report).")
        if key in ("glance", "net_worth", "by_entity", "by_person", "changes", "receipts") and section.data is None and not section.error:
            section.error = "Nothing in the books for this period."
        pack.sections.append(section)
    return pack
