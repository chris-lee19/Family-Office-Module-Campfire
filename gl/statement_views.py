"""Financial statements pages: the four statements for an entity or a consolidated group, and the ledger lines
behind an account. The numbers are Campfire's; the app is where they are read."""

import csv
from typing import Any
from urllib.parse import urlencode

import requests
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.client import CampfireClient, CampfireError, NotConnected
from campfire_integration.models import Workspace
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from gl.models import AccountMapping, GLSettings, Role
from gl.services import fairvalue, receipts
from gl.services import statements as svc

CAMPFIRE_ERRORS = (CampfireError, NotConnected, requests.RequestException)


def _client(workspace: Workspace) -> CampfireClient:
    return CampfireClient(workspace)


def _choices(request: HttpRequest) -> dict[str, str]:
    g = request.GET
    return {
        "view": g.get("view") if g.get("view") in svc.VIEWS else "bs",
        "cols": g.get("cols") if g.get("cols") in svc.COLUMNS or g.get("cols") in svc.SPLITS else "total",
        "period": g.get("period") if g.get("period") in svc.PERIODS else "ytd",
        "entity": g.get("entity", ""), "start": g.get("start", ""), "end": g.get("end", ""),
    }


def _with(base: str, params: dict[str, Any]) -> str:
    return f"{base}{'&' if '?' in base else '?'}{urlencode({k: v for k, v in params.items() if v not in ('', None)})}"


def _scope(options: list[svc.Scope], value: str) -> svc.Scope | None:
    return next((s for s in options if s.value == value), options[0] if options else None)


def _headline(statement: svc.Statement) -> list[tuple[str, Any]]:
    """The totals of a statement as headline figures (last column: the latest balance, or the period total)."""
    totals = [(r.label, r.values[-1], r.kind) for r in statement.rows if r.kind in ("total", "grand") and r.values and r.values[-1] is not None]
    by_label = {label: value for label, value, _ in totals}
    if statement.title == receipts.TITLE:  # the answer first: cash now, then what came in and what went out
        return [(totals[-1][0], totals[-1][1]), ("Receipts", by_label.get("Total receipts")), ("Disbursements", by_label.get("Total disbursements")),
                ("Net change in cash", by_label.get("Net change in cash", by_label.get("Excess of receipts over disbursements")))]
    if len(statement.columns) == 4 and statement.columns[0].startswith("Tax basis"):  # the tax basis report: its last row
        last = statement.rows[-1].values if statement.rows else []
        return [(c, v) for c, v in zip(statement.columns, last) if v is not None]
    for names in svc.NAMES.values():  # the answer first: net worth (or net assets, equity), then what makes it up
        for wanted in ((names["equity"], "Total assets", "Total liabilities"), (names["change"], names["result"], "Total contributions and distributions")):
            if all(w in by_label for w in wanted[:2]):
                return [(w, by_label[w]) for w in wanted if w in by_label]
    grand = [t[:2] for t in totals if t[2] == "grand"][:2]
    return (grand + [t[:2] for t in totals if t[2] == "total"])[:4]


def _fair_values(workspace: Workspace, scope: svc.Scope, start: Any, end: Any, tax_rate: Decimal) -> svc.FairValues | None:
    """What the investment records add to the books for a fair-value statement (None for conventional entity books)."""
    if not scope.fair_value:
        return None
    booked = frozenset(AccountMapping.objects.filter(workspace=workspace, role=Role.UNREALIZED).values_list("campfire_account_id", flat=True))
    return svc.FairValues(fairvalue.unrealized_for(workspace, scope.members, start - timedelta(days=1)),
                          fairvalue.unrealized_for(workspace, scope.members, end), booked, tax_rate)


def _clearing(workspace: Workspace) -> frozenset[int]:
    """The GL accounts that only pass money between the family's own accounts: what moves through them is a transfer."""
    return frozenset(AccountMapping.objects.filter(workspace=workspace, role__in=(Role.CASH_CLEARING, Role.TRANSFERS)).values_list("campfire_account_id", flat=True))


def statements_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    today = timezone.localdate()
    settings = GLSettings.for_workspace(workspace)
    message = ""
    if request.method == "POST":  # the one setting on this page: the rate for estimated income taxes
        if request.POST.get("action") != "tax_rate":
            return HttpResponseNotAllowed(["GET"])
        try:
            rate = Decimal(str(request.POST.get("rate") or "0").replace("%", "").strip())
            if not 0 <= rate <= 60:
                raise InvalidOperation
            settings.estimated_tax_rate = rate.quantize(Decimal("0.01"))
            settings.save(update_fields=["estimated_tax_rate"])
            message = (f"Estimated income taxes use {settings.estimated_tax_rate.normalize():f}% of unrealized appreciation."
                       if settings.estimated_tax_rate else "No estimate of income taxes is shown.")
        except InvalidOperation:
            message = "Enter the rate as a percentage between 0 and 60."
        request.GET = request.POST  # keep the choices that were on the page
    c = _choices(request)
    start, end = svc.period_dates(c["period"], today, c["start"], c["end"])
    base = pages.page_url(request, framed, "gl:statements")
    error, statement, options, scope = "", None, [], None
    try:
        client = _client(workspace)
        options = svc.classify(workspace, svc.scopes(client))
        scope = _scope(options, c["entity"])
        # A split across entities or family members is the family's Statement of Net Worth; anywhere else it is one column.
        split = c["cols"] if c["cols"] in svc.SPLITS and scope is not None and scope.kind == svc.FAMILY and c["view"] == "bs" else ""
        if c["cols"] in svc.SPLITS and not split:
            c["cols"] = "total"
        if scope is None:
            error = "No entities in Campfire yet."
        elif split:
            ids = {s.label: s.entity_id for s in options if not s.rollup}
            booked = frozenset(AccountMapping.objects.filter(workspace=workspace, role=Role.UNREALIZED).values_list("campfire_account_id", flat=True))
            statement = svc.net_worth_split(client, scope, end, split, fairvalue.unrealized_by_entity(workspace, end), booked, settings.estimated_tax_rate,
                                            svc.look_through(workspace, end, ids) if split == "person" else None)
        elif c["view"] == "rd":
            statement = receipts.build(client, scope, start, end, c["cols"], _clearing(workspace))
        elif c["view"] == "tx":
            statement = svc.tax_basis_report(scope, fairvalue.holdings_for(workspace, scope.members, end), settings.estimated_tax_rate)
        else:
            fair = _fair_values(workspace, scope, start, end, settings.estimated_tax_rate) if c["view"] in ("bs", "is") else None
            statement = svc.build(client, c["view"], scope, start, end, c["cols"], fair)
    except CAMPFIRE_ERRORS as e:
        error = f"Couldn't read the books from Campfire ({type(e).__name__}). Check that the workspace is connected."
    current = {**c, "entity": scope.value if scope else "", "start": start.isoformat(), "end": end.isoformat()}

    if statement is not None and request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{statement.title.lower().replace(" ", "-")}-{end.isoformat()}.csv"'
        out = csv.writer(response)
        out.writerow([statement.title, scope.label if scope else "", f"{start} to {end}"])
        out.writerow(["Number", "Account", *statement.columns])
        for r in statement.rows:
            out.writerow([r.number, r.label, *["" if v is None else f"{v:.2f}" for v in r.values]])
        return response

    basis = "" if scope is None else "Cash basis" if c["view"] == "rd" else scope.names["basis"]
    ledger_base = lambda account_id: _with(pages.page_url(request, framed, "gl:ledger", account_id),  # noqa: E731
                                           {"entity": current["entity"], "start": current["start"], "end": current["end"]})
    rows = [(r, ledger_base(r.account_id) if r.account_id else "") for r in statement.rows] if statement else []
    return render(request, "gl/statements.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "action": base.split("?")[0],
        "statement": statement, "rows": rows, "error": error, "options": options, "scope": scope, "choices": current,
        "start": start, "end": end, "message": message, "tax_rate": settings.estimated_tax_rate, "post_action": base,
        "views": [(k, (scope.names if scope else svc.NAMES[svc.ENTITY])[k], _with(base, {**current, "view": k}), k == c["view"]) for k in svc.VIEWS],
        "columns": {**svc.COLUMNS, **(svc.SPLITS if scope is not None and scope.kind == svc.FAMILY and c["view"] == "bs" else {})}, "periods": svc.PERIODS, "headline": _headline(statement) if statement else [],
        "csv_url": _with(base, {**current, "format": "csv"}), "cents": c["view"] == "tb",
        "basis": basis, "fair_note": bool(scope and scope.fair_value and c["view"] in ("bs", "is")),
        "print_sub": " · ".join(x for x in (scope.label if scope else "", basis,
                                            f"As of {end:%B %-d, %Y}" if c["view"] in ("bs", "tx") else f"{start:%B %-d, %Y} to {end:%B %-d, %Y}") if x),
    })


def ledger_page(request: HttpRequest, workspace: Workspace, framed: bool, account_id: int) -> HttpResponse:
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    today = timezone.localdate()
    start, end = svc.period_dates("custom", today, request.GET.get("start", ""), request.GET.get("end", ""))
    error, label, lines, total, scope = "", "", [], 0, None
    try:
        client = _client(workspace)
        scope = _scope(svc.scopes(client), request.GET.get("entity", ""))
        if scope is not None:
            label, lines, total = svc.ledger(client, account_id, scope, start, end)
    except CAMPFIRE_ERRORS as e:
        error = f"Couldn't read the ledger from Campfire ({type(e).__name__})."
    back = _with(pages.page_url(request, framed, "gl:statements"),
                 {"entity": scope.value if scope else "", "period": "custom", "start": start.isoformat(), "end": end.isoformat(),
                  "view": request.GET.get("view", "")})
    return render(request, "gl/ledger.html", {
        "workspace": workspace, "framed": framed, "label": label or f"Account {account_id}", "lines": lines, "total": total, "scope": scope,
        "start": start, "end": end, "error": error, "back": back,
        "debits": sum((ln.debit for ln in lines), svc.ZERO), "credits": sum((ln.credit for ln in lines), svc.ZERO),
    })


frame_statements = pages.framed(statements_page)
standalone_statements = pages.standalone(statements_page)
frame_ledger = pages.framed(ledger_page)
standalone_ledger = pages.standalone(ledger_page)
