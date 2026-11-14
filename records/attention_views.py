"""Home, the executive dashboard: where the family stands, how liquid it is, and what needs a decision. And "To do":
everything that needs a person, most urgent first, each with a button to the page that fixes it."""

from decimal import Decimal
from typing import Any

import requests
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.client import CampfireClient, CampfireError, NotConnected
from campfire_integration.models import Workspace
from campfire_integration.templatetags.nav import _badges
from entities.models import Family
from gl.services import close, dashboard
from investments.models import CapitalCall
from investments.services.privates import irr
from investments.services.portfolio import build_portfolio
from investments.views import breakdown, demo_url
from records import attention
from records.models import AuditEntry

CAMPFIRE_ERRORS = (CampfireError, NotConnected, requests.RequestException)
TOP_ITEMS = 6


def _client(workspace: Workspace) -> CampfireClient:
    return CampfireClient(workspace)


def _close_rows(workspace: Workspace, month: Any) -> list[dict[str, Any]]:
    rows = []
    for r in close.month_status(workspace, month):
        done = sum(1 for s in r.steps if s.done)
        rows.append({"label": r.label, "done": done, "total": len(r.steps), "pct": round(done * 100 / len(r.steps)) if r.steps else 0,
                     "closed": r.closed, "next": r.next_step.label if r.next_step else "", "problem": any(s.problem for s in r.steps)})
    return rows


def _commitments(workspace: Workspace, today: Any) -> list[dict[str, Any]]:
    """Each private-fund commitment: how much is called, what is left, and how it has done."""
    out = []
    for family in Family.objects.filter(workspace=workspace, is_active=True):
        for c in build_portfolio(family, today).commitments:
            amount = c.commitment.amount
            out.append({"fund": c.commitment.instrument.name, "commitment": amount, "called": c.contributed, "unfunded": c.unfunded, "nav": c.nav,
                        "pct": float(min(c.pct_called or 0, 100)), "tvpi": c.tvpi, "irr": irr(c.commitment, today, c.nav), "stale": c.nav_stale})
    return sorted(out, key=lambda r: -r["commitment"])


def home_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    today = timezone.localdate()
    background = request.headers.get("X-Requested-With") == "fetch"
    error = ""
    snap, fresh = dashboard.current(workspace, today)
    if (request.method == "POST" and request.POST.get("action") == "refresh") or snap is None:
        try:  # the first visit builds it while the person waits; after that it refreshes behind the page
            snap, fresh = dashboard.refresh(workspace, _client(workspace), today), True
        except CAMPFIRE_ERRORS as e:
            error = f"Couldn't read the books from Campfire ({type(e).__name__}). Check that the workspace is connected."
        if background:
            return HttpResponse(status=502 if error else 204)
    items = attention.collect(workspace)
    counts = {s: sum(1 for i in items if i.severity == s) for s in (attention.URGENT, attention.TODO, attention.INFO)}
    head = attention.headline(workspace)
    url = lambda name, *args: pages.page_url(request, framed, name, *args)  # noqa: E731
    badges = _badges(workspace)
    approvals = [{"label": label, "count": badges.get(key, 0), "url": url(page)} for label, key, page in (
        ("Bills", "bills", "gl:bills"), ("Journal entries", "journals", "gl:journals"), ("Bank reconciliations", "reconcile", "gl:reconcile"),
        ("Marks and imports", "review", "records:review"))]
    last_month = close.previous_month(today)
    close_rows = _close_rows(workspace, last_month)
    data = snap.data if snap else {}
    return render(request, "records/home.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "today": today, "error": error,
        "snap": snap, "fresh": fresh, "nw": data.get("net_worth"), "lq": data.get("liquidity"), "bl": data.get("bills"), "rc": data.get("reconciliation"),
        "rows": [(i, url(i.target, *i.args)) for i in items[:TOP_ITEMS]], "more": max(len(items) - TOP_ITEMS, 0), "counts": counts, "head": head,
        "approvals": approvals, "approvals_total": sum(a["count"] for a in approvals), "demo_url": demo_url(request, workspace, framed),
        "allocation": breakdown(head.by_asset_class, head.total), "commitments": _commitments(workspace, today) if head.families else [],
        "close_rows": close_rows, "close_month": last_month, "close_done": bool(close_rows) and all(r["closed"] for r in close_rows),
        "closed_count": sum(1 for r in close_rows if r["closed"]), "action": url("records:attention"),
        "links": {k: url(v) for k, v in {"work": "records:work", "statements": "gl:statements", "investments": "investments:investments", "cash": "gl:cash",
                                           "liquidity": "gl:liquidity", "bills": "gl:bills", "calls": "gl:calls", "close": "gl:close", "reconcile": "gl:reconcile",
                                           "entities": "entities:entities", "imports": "investments:imports"}.items()},
    })


def attention_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    today = timezone.localdate()
    items = attention.collect(workspace)
    rows = [(i, pages.page_url(request, framed, i.target, *i.args)) for i in items]
    counts = {s: sum(1 for i in items if i.severity == s) for s in (attention.URGENT, attention.TODO, attention.INFO)}
    head = attention.headline(workspace)

    last_month = close.previous_month(today)
    close_rows = _close_rows(workspace, last_month)
    calls = [{"call": c, "days": (c.due_date - today).days, "late": (today - c.due_date).days} for c in
             CapitalCall.objects.filter(commitment__account__family__workspace=workspace, status__in=CapitalCall.OPEN_STATUSES)
             .select_related("commitment__instrument", "commitment__account__entity").order_by("due_date")[:5]]
    url = lambda name, *args: pages.page_url(request, framed, name, *args)  # noqa: E731
    return render(request, "records/attention.html", {
        "workspace": workspace, "framed": framed, "rows": rows, "counts": counts, "head": head, "today": today,
        "demo_url": demo_url(request, workspace, framed),
        "allocation": breakdown(head.by_asset_class, head.total), "close_rows": close_rows, "close_month": last_month,
        "close_done": bool(close_rows) and all(r["closed"] for r in close_rows), "calls": calls,
        "calls_total": sum((c["call"].amount for c in calls), Decimal(0)),
        "recent": AuditEntry.objects.filter(workspace=workspace)[:6],
        "links": {"investments": url("investments:investments"), "calls": url("gl:calls"), "close": url("gl:close"), "records": url("records:home"),
                  "imports": url("investments:imports"), "new_call": url("records:new", "capital-calls"), "new_mark": url("records:new", "valuations")},
    })


frame_attention = pages.framed(home_page)
standalone_attention = pages.standalone(home_page)
frame_work = pages.framed(attention_page)
standalone_work = pages.standalone(attention_page)
