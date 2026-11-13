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
from gl.services import statements as svc

CAMPFIRE_ERRORS = (CampfireError, NotConnected, requests.RequestException)


def _client(workspace: Workspace) -> CampfireClient:
    return CampfireClient(workspace)


def _choices(request: HttpRequest) -> dict[str, str]:
    g = request.GET
    return {
        "view": g.get("view") if g.get("view") in svc.VIEWS else "bs",
        "cols": g.get("cols") if g.get("cols") in svc.COLUMNS else "total",
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
    for wanted in (("Net worth", "Total assets", "Total liabilities"),  # the answer first
                   ("Change in net worth", "Excess of income over expenses", "Total contributions and distributions")):
        if all(w in by_label for w in wanted[:2]):
            return [(w, by_label[w]) for w in wanted if w in by_label]
    grand = [t[:2] for t in totals if t[2] == "grand"][:2]
    return (grand + [t[:2] for t in totals if t[2] == "total"])[:4]


def statements_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    today = timezone.localdate()
    c = _choices(request)
    start, end = svc.period_dates(c["period"], today, c["start"], c["end"])
    base = pages.page_url(request, framed, "gl:statements")
    error, statement, options, scope = "", None, [], None
    try:
        client = _client(workspace)
        options = svc.scopes(client)
        scope = _scope(options, c["entity"])
        if scope is None:
            error = "No entities in Campfire yet."
        else:
            statement = svc.build(client, c["view"], scope, start, end, c["cols"])
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

    ledger_base = lambda account_id: _with(pages.page_url(request, framed, "gl:ledger", account_id),  # noqa: E731
                                           {"entity": current["entity"], "start": current["start"], "end": current["end"]})
    rows = [(r, ledger_base(r.account_id) if r.account_id else "") for r in statement.rows] if statement else []
    return render(request, "gl/statements.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "action": base.split("?")[0],
        "statement": statement, "rows": rows, "error": error, "options": options, "scope": scope, "choices": current,
        "start": start, "end": end, "views": [(k, label, _with(base, {**current, "view": k}), k == c["view"]) for k, label in svc.VIEWS.items()],
        "columns": svc.COLUMNS, "periods": svc.PERIODS, "headline": _headline(statement) if statement else [],
        "csv_url": _with(base, {**current, "format": "csv"}), "cents": c["view"] == "tb",
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
