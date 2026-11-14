"""The Bank reconciliation page: every bank account's standing, and one statement worked line by line."""

from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import requests
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.client import CampfireClient, CampfireError, NotConnected
from campfire_integration.models import Workspace
from gl.services import reconcile as svc

CAMPFIRE_ERRORS = (CampfireError, NotConnected, requests.RequestException)
ZERO = Decimal(0)


def _client(workspace: Workspace) -> CampfireClient:
    return CampfireClient(workspace)


def _int(v: Any) -> int:
    return int(v) if str(v or "").isdigit() else 0


def _with(base: str, params: dict[str, Any]) -> str:
    query = urlencode({k: v for k, v in params.items() if v not in ("", None, 0)})
    return f"{base}{'&' if '?' in base else '?'}{query}" if query else base


def _handle(request: HttpRequest, workspace: Workspace, client: CampfireClient, today: Any) -> tuple[str, int]:
    """Do what was asked. Returns what happened and the statement to show next (0: the overview)."""
    action, report_id = request.POST.get("action", ""), _int(request.POST.get("report"))
    actor, actor_id = getattr(request, "fo_actor", ""), getattr(request, "fo_actor_id", "")
    if action == "start":
        report = svc.start(workspace, client, request.POST, today, actor)
        return f"Statement to {report.end:%b %-d, %Y} started for {report.entity}. Tick what cleared the bank.", report.id
    if action == "save":
        after = svc.save(workspace, client, report_id, [int(v) for v in request.POST.getlist("line") if v.isdigit()], actor)
        return ("Saved: it reconciles. Finish it to send it for approval." if after.reconciles
                else f"Saved: {len(after.cleared)} line{'s' if len(after.cleared) != 1 else ''} cleared, {abs(after.difference):,.2f} still unexplained."), report_id
    if action == "ending":
        svc.set_ending(workspace, client, report_id, request.POST.get("ending"), actor)
        return "Ending balance changed.", report_id
    if action == "finish":
        if request.POST.get("with_lines"):  # the ticks on the page are part of finishing: save them first
            svc.save(workspace, client, report_id, [int(v) for v in request.POST.getlist("line") if v.isdigit()], actor)
        report = svc.finish(workspace, client, report_id, actor, actor_id, today)
        return f"{report.entity}: statement to {report.end:%b %-d, %Y} finished. It now waits for approval.", 0
    if action == "approve":
        report = svc.approve(workspace, client, report_id, actor, actor_id)
        return f"{report.entity}: reconciliation to {report.end:%b %-d, %Y} approved.", 0
    if action == "reopen":
        report = svc.reopen(workspace, client, report_id, actor)
        return f"{report.entity}: statement to {report.end:%b %-d, %Y} reopened.", report_id
    if action == "discard":
        report = svc.discard(workspace, client, report_id, actor)
        return f"{report.entity}: statement to {report.end:%b %-d, %Y} discarded. Its lines are open again.", 0
    raise svc.ReconcileError("That action isn't available here.")


def reconcile_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    today = timezone.localdate()
    base = pages.page_url(request, framed, "gl:reconcile")
    report_id = _int(request.GET.get("report"))
    message = error = ""
    books: list[svc.Book] = []
    detail = None
    entered = ({k: request.POST.get(k, "") for k in ("book", "end", "ending", "start", "starting")} if request.POST.get("action") == "start"
               else {"book": request.GET.get("book", "")} if request.GET.get("book") else {})
    try:
        client = _client(workspace)
        if request.method == "POST":
            try:
                message, report_id = _handle(request, workspace, client, today)
                entered = {}
            except svc.ReconcileError as e:
                error, report_id = str(e), _int(request.POST.get("report"))
        try:
            if report_id:
                detail = svc.detail(client, workspace, report_id)
            else:
                books = svc.books(client, workspace, today)
        except svc.ReconcileError as e:
            error = error or str(e)
    except CAMPFIRE_ERRORS as e:
        error = error or f"Couldn't read the books from Campfire ({type(e).__name__}). Check that the workspace is connected."
    context: dict[str, Any] = {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "today": today, "message": message,
        "error": error, "action": base.split("?")[0], "overview_url": base, "me": getattr(request, "fo_actor_id", ""),
        "cash_url": pages.page_url(request, framed, "gl:cash"), "journals_url": pages.page_url(request, framed, "gl:journals"),
    }
    if detail is not None:
        selected = detail.suggested
        r = detail.report
        signed = " · ".join(x for x in (f"prepared by {r.record.prepared_by}" if r.record and r.record.prepared_by else "",
                                        f"approved by {r.record.approved_by}" if r.record and r.record.approved_by else "") if x)
        context.update({"print_sub": f"{r.entity} · {r.account} · statement {r.start:%B %-d, %Y} to {r.end:%B %-d, %Y} · {r.status_label}" + (f" · {signed}" if signed else ""),
                        "detail": detail, "report": detail.report, "post_action": _with(base, {"report": detail.report.id}), "suggested": selected,
                        "rows": [(ln, ln.cleared or ln.id in selected) for ln in detail.lines], "feed_count": sum(1 for ln in detail.lines if ln.from_feed)})
    else:
        target = today.replace(day=1) - timedelta(days=1)
        reports = sorted((r for b in books for r in b.reports), key=lambda r: (r.end, r.id), reverse=True)
        context.update({
            "books": [(b, _with(base, {"report": b.open.id}) if b.open else _with(base, {"book": b.key}) + "#start") for b in books], "target": target, "post_action": base, "entered": entered,
            "reopen": bool(entered), "start_default": entered.get("end") or target.isoformat(),
            "pending": [(r, _with(base, {"report": r.id})) for r in reports if r.status == svc.PENDING],
            "history": [(r, _with(base, {"report": r.id})) for r in reports],
            "stats": {"total": len(books), "current": sum(1 for b in books if b.state == "current"), "progress": sum(1 for b in books if b.state == "progress"),
                      "pending": sum(1 for b in books if b.state == "pending"), "never": sum(1 for b in books if b.state == "never"),
                      "behind": sum(1 for b in books if b.state == "behind")},
        })
    return render(request, "gl/reconcile.html", context)


frame_reconcile = pages.framed(reconcile_page)
standalone_reconcile = pages.standalone(reconcile_page)
