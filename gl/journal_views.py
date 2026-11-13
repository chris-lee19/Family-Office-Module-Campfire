"""The Journal entries page: write an entry or a transfer between entities, approve it, and look up the ledger."""

from datetime import timedelta
from typing import Any

import requests
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.client import CampfireClient, CampfireError, NotConnected
from campfire_integration.models import Workspace
from gl.models import JournalRequest
from gl.services import journals as svc
from gl.services import statements
from gl.services.setup import account_label, list_accounts

CAMPFIRE_ERRORS = (CampfireError, NotConnected, requests.RequestException)
LINES = 8
LOOKBACK_DAYS = 120


def _client(workspace: Workspace) -> CampfireClient:
    return CampfireClient(workspace)


def _handle(request: HttpRequest, workspace: Workspace, client: CampfireClient, ref: dict[str, Any]) -> str:
    action = request.POST.get("action", "")
    actor, actor_id = getattr(request, "fo_actor", ""), getattr(request, "fo_actor_id", "")
    if action == "enter":
        req = svc.submit_manual(workspace, request.POST, ref["entities"], ref["accounts"], actor, actor_id)
        return f"Entry {req.ref_number} ({req.total:,.2f}) written: it now waits for approval."
    if action == "transfer":
        req = svc.submit_transfer(workspace, request.POST, ref["entities"], ref["accounts"], actor, actor_id)
        return f"Transfer {req.ref_number} ({req.total / 2:,.2f}) written: it now waits for approval."
    if action in ("approve", "reject"):
        raw = request.POST.get("entry", "")
        req = JournalRequest.objects.filter(workspace=workspace, pk=int(raw) if raw.isdigit() else 0).first()
        if req is None:
            raise svc.JournalError("That entry isn't in this workspace.")
        if action == "reject":
            svc.reject(req, actor, actor_id, request.POST.get("note", ""))
            return f"Entry {req.ref_number} rejected."
        req = svc.approve(req, client, actor, actor_id)
        return (f"Entry {req.ref_number} approved and posted to the ledger." if req.status == req.Status.SENT
                else f"Entry {req.ref_number} approved here; Campfire holds it for its own approval too.")
    raise svc.JournalError("Unknown action.")


def journals_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    today = timezone.localdate()
    message = error = ""
    ref: dict[str, Any] = {"entities": {}, "accounts": {}}
    entries: list[svc.Entry] = []
    try:
        client = _client(workspace)
        ref = {"entities": {s.entity_id: s.label for s in statements.scopes(client) if not s.rollup},
               "accounts": {int(a["id"]): account_label(a) for a in list_accounts(client) if a.get("account_type") != "UNCATEGORIZED"}}
        if request.method == "POST":
            try:
                message = _handle(request, workspace, client, ref)
            except svc.JournalError as e:
                error = str(e)
        entries = svc.recent(client, today - timedelta(days=LOOKBACK_DAYS), today)
    except CAMPFIRE_ERRORS as e:
        error = error or f"Couldn't read the ledger from Campfire ({type(e).__name__}). Check that the workspace is connected."
    retry = request.POST.get("action") if error and request.POST.get("action") in ("enter", "transfer") else ""
    typed = request.POST if retry else {}
    rows = [{"n": n, "account": typed.get(f"account_{n}", ""), "debit": typed.get(f"debit_{n}", ""), "credit": typed.get(f"credit_{n}", ""),
             "description": typed.get(f"description_{n}", "")} for n in range(1, LINES + 1)]
    return render(request, "gl/journals.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "today": today, "message": message, "error": error,
        "entries": entries, "pending": list(JournalRequest.objects.filter(workspace=workspace, status=JournalRequest.Status.PENDING)),
        "recent": JournalRequest.objects.filter(workspace=workspace).exclude(status=JournalRequest.Status.PENDING)[:6], "ref": ref, "rows": rows,
        "typed": {k: typed.get(k, "") for k in ("entity", "date", "memo", "from", "to", "amount")}, "retry": retry, "lookback": LOOKBACK_DAYS,
        "action": pages.page_url(request, framed, "gl:journals"), "me": getattr(request, "fo_actor_id", ""),
        "by_source": sum(1 for e in entries if e.source == "Family Office"),
    })


frame_journals = pages.framed(journals_page)
standalone_journals = pages.standalone(journals_page)
