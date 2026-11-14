"""The Budget vs. actual page: three tabs.

- Budget vs. Actual (`view=compare`): the year through a chosen month, for one entity or a consolidated group.
- Lifestyle Expense Report (`view=lifestyle`): household and family spending by category, for each family member
  or property, from the spending tags in the books.
- The budget (`view=budget`): the year's versions; start one (from the books, empty, or the demo budget), change its
  lines or upload a spreadsheet, approve it, revise it.
"""

import csv
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

import requests
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.client import CampfireClient, CampfireError, NotConnected
from campfire_integration.models import Workspace
from gl.models import Budget, FamilyProperty
from gl.services import budget as svc
from gl.services import budget_demo, lifestyle
from gl.services import statements as st
from records.models import AuditEntry

CAMPFIRE_ERRORS = (CampfireError, NotConnected, requests.RequestException)
VIEWS = {"compare": "Budget vs. Actual", "lifestyle": "Lifestyle Expense Report", "budget": "Budget"}
ZERO = Decimal(0)


def _client(workspace: Workspace) -> CampfireClient:
    return CampfireClient(workspace)


def _int(v: Any) -> int:
    return int(v) if str(v or "").lstrip("-").isdigit() else 0


def _with(base: str, params: dict[str, Any]) -> str:
    query = urlencode({k: v for k, v in params.items() if v not in ("", None)})
    return f"{base}{'&' if '?' in base else '?'}{query}" if query else base


def _choices(q: Any, today: date) -> dict[str, Any]:
    last = st.month_start(today) - timedelta(days=1)  # the last complete month
    year = _int(q.get("year")) or (last.year if last.year == today.year or today.month == 1 else today.year)
    month = _int(q.get("month"))
    if not 1 <= month <= 12:
        month = last.month if year == last.year else (12 if year < today.year else max(today.month - 1, 1))
    return {"view": q.get("view") if q.get("view") in VIEWS else "compare", "entity": q.get("entity", ""), "year": year, "month": month,
            "period": q.get("period") if q.get("period") in st.PERIODS else "ytd", "start": q.get("start", ""), "end": q.get("end", ""),
            "by": q.get("by") if q.get("by") in lifestyle.VIEWS else "person"}


def _handle(request: HttpRequest, workspace: Workspace, client: CampfireClient, c: dict[str, Any]) -> str:
    """The page's actions. Returns what happened; raises svc.BudgetError with what to fix."""
    post, actor, actor_id = request.POST, getattr(request, "fo_actor", ""), getattr(request, "fo_actor_id", "")
    action, year = post.get("action", ""), c["year"]
    budget = Budget.objects.filter(workspace=workspace, pk=_int(post.get("budget"))).first() if post.get("budget") else None
    if action == "start":
        how = post.get("how")
        if how == "demo":
            if not budget_demo.needs_demo_budget(workspace, year):
                raise svc.BudgetError("The demo budget is only for the demo family, for a year without a budget.")
            b = budget_demo.create(workspace, client, year, actor, actor_id)
            return f"Added the demo budget for {year}: {b.lines.count()} lines, waiting for approval."
        if how == "books":
            try:
                uplift = Decimal(str(post.get("uplift") or "0").replace("%", "").strip())
            except InvalidOperation:
                raise svc.BudgetError("Enter the increase as a percentage, such as 3.") from None
            suggested = svc.suggest(client, year, timezone.localdate(), uplift)
            if not suggested.lines:
                raise svc.BudgetError("The books have no income or expenses to start from yet. Start an empty budget instead.")
            b = svc.new_version(workspace, year, actor, actor_id, f"From the books: {suggested.basis}", suggested.lines)
            return f"Started the {year} budget from {suggested.basis}: {b.lines.count()} lines to review."
        if how == "copy":
            source = svc.in_use(workspace, year - 1)
            if source is None:
                raise svc.BudgetError(f"There is no {year - 1} budget to copy.")
            b = svc.new_version(workspace, year, actor, actor_id, f"Copied from the {year - 1} budget", copy_from=source)
            return f"Started the {year} budget as a copy of {year - 1}'s: {b.lines.count()} lines."
        svc.new_version(workspace, year, actor, actor_id, "Started empty")
        return f"Started an empty {year} budget. Add its lines below, or upload a spreadsheet."
    if budget is None:
        raise svc.BudgetError("That budget isn't here any more.")
    if action == "revise":
        b = svc.new_version(workspace, budget.year, actor, actor_id, (post.get("note") or "Revised").strip(), copy_from=budget)
        return f"Version {b.version} of the {b.year} budget is a draft: change it, then have it approved. Version {budget.version} stays in use until then."
    if action == "approve":
        svc.approve(budget, actor, actor_id)
        return f"Approved the {budget.year} budget (version {budget.version}). Reports use it from now on."
    if action == "discard":
        svc.discard(budget, actor)
        return f"Discarded the draft of the {budget.year} budget."
    if action in ("save_line", "delete_line", "upload"):
        names = {s.entity_id: s.label for s in st.scopes(client) if not s.rollup}
        accounts = svc.pl_accounts(client)
        if action == "save_line":
            line = svc.save_line(budget, post, names, {a.id: a for a in accounts}, actor)
            return f"Saved {line.entity_label} · {line.account_label}: {line.total:,.0f} for the year."
        if action == "delete_line":
            line = svc.remove_line(budget, _int(post.get("line")), actor)
            return f"Removed {line.entity_label} · {line.account_label} from the budget."
        upload = request.FILES.get("file")
        if upload is None:
            raise svc.BudgetError("Choose the spreadsheet to upload.")
        n = svc.from_xlsx(budget, upload.read(), names, accounts, actor)
        return f"Replaced the draft's lines with the spreadsheet's: {n} lines."
    raise svc.BudgetError("That action isn't available here.")


def _handle_spending(request: HttpRequest, workspace: Workspace, client: CampfireClient) -> str:
    post, actor = request.POST, getattr(request, "fo_actor", "")
    action = post.get("action", "")
    if action == "setup_tags":
        added = lifestyle.set_up(workspace, client)
        return f"Spending tags are set up in Campfire for family members and properties ({added} linked)."
    if action == "add_property":
        name = (post.get("name") or "").strip()[:100]
        kind = post.get("kind") if post.get("kind") in FamilyProperty.Kind.values else FamilyProperty.Kind.RESIDENCE
        if not name:
            raise svc.BudgetError("Give the property a name.")
        prop, made = FamilyProperty.objects.get_or_create(workspace=workspace, name=name, defaults={"kind": kind, "created_by": actor})
        if not made:
            raise svc.BudgetError(f"{name} is already listed.")
        AuditEntry.objects.create(workspace=workspace, actor=actor, action=AuditEntry.Action.CREATED, kind="family-properties", object_id=str(prop.pk),
                                  object_repr=name, changes={"property": ["", f"{name} ({prop.get_kind_display()})"]})
        tagged = lifestyle.set_up(workspace, client) if lifestyle.is_set_up(workspace) else 0
        return f"Added {name}." + (" It can now be chosen as a property when spending is recorded." if tagged else "")
    if action == "remove_property":
        prop = FamilyProperty.objects.filter(workspace=workspace, pk=_int(post.get("property"))).first()
        if prop is None:
            raise svc.BudgetError("That property isn't listed any more.")
        AuditEntry.objects.create(workspace=workspace, actor=actor, action=AuditEntry.Action.DELETED, kind="family-properties", object_id=str(prop.pk),
                                  object_repr=prop.name, changes={"property": [prop.name, ""]})
        prop.delete()
        return f"Removed {prop.name} from the list. Spending already tagged to it keeps its tag in Campfire."
    raise svc.BudgetError("That action isn't available here.")


def _csv(comparison: svc.Comparison) -> HttpResponse:
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="budget-vs-actual-{comparison.year}-{comparison.month:02d}.csv"'
    out = csv.writer(response)
    out.writerow(["Budget vs. Actual", comparison.scope.label, comparison.month_label])
    out.writerow(["Number", "Account", "Month actual", "Month budget", "Month better (worse)", "YTD actual", "YTD budget", "YTD better (worse)",
                  "Year budget", "Outlook", "Flag"])
    money = lambda v: f"{v:.2f}"  # noqa: E731
    for rows, total in ((comparison.income, comparison.total_income), (comparison.expenses, comparison.total_expenses), ([], comparison.net)):
        for r in [*rows, total]:
            out.writerow([r.number, r.label, money(r.m_actual), money(r.m_budget), money(r.m_better), money(r.ytd_actual), money(r.ytd_budget),
                          money(r.ytd_better), money(r.year_budget), money(r.outlook), r.flag])
    return response


def _xlsx(budget: Budget) -> HttpResponse:
    response = HttpResponse(svc.to_xlsx(budget), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="budget-{budget.year}-v{budget.version}.xlsx"'
    return response


def budget_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    today = timezone.localdate()
    q = request.POST if request.method == "POST" else request.GET
    c = _choices(q, today)
    base = pages.page_url(request, framed, "gl:budget")
    message = error = ""
    options: list[st.Scope] = []
    scope = comparison = report = None
    names: dict[int, str] = {}
    accounts: list[svc.PLAccount] = []
    try:
        client = _client(workspace)
        if request.method == "POST":
            try:
                if request.POST.get("action") in ("setup_tags", "add_property", "remove_property"):
                    message = _handle_spending(request, workspace, client)
                else:
                    message = _handle(request, workspace, client, c)
            except svc.BudgetError as e:
                error = str(e)
        if request.method == "GET" and request.GET.get("format") == "xlsx":
            b = Budget.objects.filter(workspace=workspace, pk=_int(request.GET.get("budget"))).first()
            if b is not None:
                return _xlsx(b)
        options = st.scopes(client)
        scope = next((s for s in options if s.value == c["entity"]), options[0] if options else None)
        if scope is not None and c["view"] == "compare":
            comparison = svc.compare(workspace, client, scope, c["year"], c["month"])
            if request.method == "GET" and request.GET.get("format") == "csv":
                return _csv(comparison)
        if scope is not None and c["view"] == "lifestyle":
            start, end = _whole_months(*st.period_dates(c["period"], today, c["start"], c["end"]))
            report = lifestyle.report(workspace, client, scope, start, end, c["by"])
        if c["view"] == "budget":
            names = {s.entity_id: s.label for s in options if not s.rollup}
            accounts = svc.pl_accounts(client)
    except CAMPFIRE_ERRORS as e:
        error = error or f"Couldn't read the books from Campfire ({type(e).__name__}). Check that the workspace is connected."
    current = {"view": c["view"], "entity": scope.value if scope else "", "year": c["year"], "month": c["month"]}
    lifestyle_current = {**current, "period": c["period"], "start": c["start"], "end": c["end"], "by": c["by"]}
    versions = svc.budgets(workspace, c["year"])
    working = svc.draft(workspace, c["year"])
    shown = working or svc.in_use(workspace, c["year"])
    ledger = lambda account_id: _with(pages.page_url(request, framed, "gl:ledger", account_id),  # noqa: E731
                                      {"entity": scope.value if scope else "", "start": date(c["year"], 1, 1).isoformat(),
                                       "end": st.month_end(date(c["year"], c["month"], 1)).isoformat()})
    asked = st.period_dates(c["period"], today, c["start"], c["end"])
    period = _whole_months(*asked)
    groups = _lines(shown) if shown else []
    return render(request, "gl/budget.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "today": today,
        "message": message, "error": error, "choices": c, "options": options, "scope": scope, "views": VIEWS,
        "tabs": [(k, label, _with(base, {**current, "view": k}), k == c["view"]) for k, label in VIEWS.items()],
        "action": base.split("?")[0], "post_action": _with(base, current if c["view"] != "lifestyle" else lifestyle_current),
        "years": svc.years(workspace, today), "months": list(enumerate(svc.MONTHS, start=1)), "periods": st.PERIODS, "bys": lifestyle.VIEWS,
        # Budget vs. Actual
        "c": comparison, "bullets": svc.chart(comparison) if comparison else [],
        "rows": _rows(comparison, ledger) if comparison else [],
        "csv_url": _with(base, {**current, "view": "compare", "format": "csv"}),
        # Lifestyle
        "life": report, "life_chart": lifestyle.chart(report) if report else {}, "set_up": lifestyle.is_set_up(workspace),
        "properties": FamilyProperty.objects.filter(workspace=workspace), "property_kinds": FamilyProperty.Kind.choices, "period": period,
        "life_rows": [(cat, [cat.values.get(col, ZERO) for col in report.columns],
                       _with(pages.page_url(request, framed, "gl:ledger", cat.account_id),
                             {"entity": scope.value if scope else "", "start": period[0].isoformat(), "end": period[1].isoformat()}))
                      for cat in report.categories] if report else [],
        "life_gap": (report.budget - report.total) if report and report.budgeted else None,
        "life_months": _months_label(report) if report and report.budgeted else "", "trimmed": period != asked,
        "tag_choices": lifestyle.choices(workspace),
        # The budget
        "versions": versions, "shown": shown, "working": working, "lines": groups,
        "budget_income": sum((g["income"] for g in groups), ZERO), "budget_expenses": sum((g["expenses"] for g in groups), ZERO), "names": sorted(names.items(), key=lambda kv: kv[1]),
        "accounts": accounts, "spreads": svc.SPREADS, "demo": budget_demo.needs_demo_budget(workspace, c["year"]),
        "previous": svc.in_use(workspace, c["year"] - 1), "xlsx_url": _with(base, {"view": "budget", "year": c["year"], "format": "xlsx", "budget": shown.pk}) if shown else "",
        "budget_url": _with(base, {**current, "view": "budget"}), "compare_url": _with(base, {**current, "view": "compare"}),
        "bills_url": pages.page_url(request, framed, "gl:bills"), "journals_url": pages.page_url(request, framed, "gl:journals"),
        "cash_url": pages.page_url(request, framed, "gl:cash"),
        "print_sub": " · ".join(x for x in (scope.label if scope else "", f"Year to {comparison.month_label}" if comparison else
                                            (f"{period[0]:%B %-d, %Y} to {period[1]:%B %-d, %Y}" if report else "")) if x),
    })


def _whole_months(start: date, end: date) -> tuple[date, date]:
    """A period that ends part-way through a month ends at the month before instead (year to date on the 14th runs
    through the last month end), so it compares with whole months of budget, as Budget vs. Actual does."""
    if end != st.month_end(end) and st.month_start(end) - timedelta(days=1) >= start:
        return start, st.month_start(end) - timedelta(days=1)
    return start, end


def _months_label(report: lifestyle.Lifestyle) -> str:
    first, last = report.budget_months
    return svc.MONTHS[first - 1] if first == last else f"{svc.MONTHS[first - 1]} to {svc.MONTHS[last - 1]}"


def _rows(comparison: svc.Comparison, ledger: Any) -> list[tuple[str, Any, str]]:
    """The report's rows in reading order: (kind, row, ledger link) with kind section | account | total | grand."""
    out: list[tuple[str, Any, str]] = []
    for title, rows, total in (("Income", comparison.income, comparison.total_income), ("Expenses", comparison.expenses, comparison.total_expenses)):
        if not rows:
            continue
        out.append(("section", title, ""))
        out += [("account", r, ledger(r.account_id) if r.account_id else "") for r in rows]
        out.append(("total", total, ""))
    out.append(("grand", comparison.net, ""))
    return out


def _lines(budget: Budget) -> list[dict[str, Any]]:
    """The budget's lines by entity, with each entity's totals."""
    groups: dict[str, dict[str, Any]] = {}
    for ln in budget.lines.all():
        g = groups.setdefault(ln.entity_label, {"entity": ln.entity_label, "lines": [], "income": ZERO, "expenses": ZERO})
        ln.month_values = list(zip(svc.MONTHS, ln.amounts))
        g["lines"].append(ln)
        g["income" if ln.income else "expenses"] += ln.total
    return list(groups.values())


frame_budget = pages.framed(budget_page)
standalone_budget = pages.standalone(budget_page)
