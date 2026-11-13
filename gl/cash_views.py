"""The Cash page: balances by entity, bank lines waiting to be categorized, and recent bank activity."""

import requests
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.client import CampfireClient, CampfireError, NotConnected
from campfire_integration.models import Workspace
from gl.services import bankmatch
from gl.services import cash as svc
from gl.services.calls import list_vendors
from gl.services.setup import account_label, list_accounts
from records.models import AuditEntry

CAMPFIRE_ERRORS = (CampfireError, NotConnected, requests.RequestException)
GROUPS = (("Expenses", ("OPERATING_EXPENSES", "OTHER_EXPENSE", "COGS")), ("Income", ("REVENUE", "OTHER_INCOME")),
          ("Assets", ("ASSET",)), ("Liabilities", ("LIABILITY",)), ("Equity", ("EQUITY",)))


def _client(workspace: Workspace) -> CampfireClient:
    return CampfireClient(workspace)


def _int(v: object) -> int:
    return int(v) if str(v or "").isdigit() else 0


def _categorize(request: HttpRequest, workspace: Workspace, client: CampfireClient) -> str:
    today = timezone.localdate()
    line_id, account_id, vendor_id = _int(request.POST.get("line")), _int(request.POST.get("account")), _int(request.POST.get("vendor"))
    line = next((ln for ln in svc.overview(client, today).waiting if ln.id == line_id), None)
    if line is None or line.gl_line_id is None:
        raise svc.CashError("That bank line isn't waiting to be categorized any more.")
    accounts = {int(a["id"]): a for a in list_accounts(client)}
    if account_id not in accounts or accounts[account_id].get("account_type") == "UNCATEGORIZED":
        raise svc.CashError("Choose the account this line belongs to.")
    outcome = svc.categorize(client, line.gl_line_id, account_id, vendor_id or None)
    label = account_label(accounts[account_id])
    AuditEntry.objects.create(workspace=workspace, actor=getattr(request, "fo_actor", ""), action=AuditEntry.Action.UPDATED, kind="bank-lines",
                              object_id=str(line.id), object_repr=f"{line.merchant} {line.amount:,.2f} ({line.entity})"[:255],
                              changes={"category": ["Uncategorized", label]})
    return f"{line.merchant}: {outcome} as {label}." if outcome == "categorized" else f"{line.merchant}: {label}, {outcome} in Campfire."


def cash_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    today = timezone.localdate()
    message = error = ""
    cash, accounts, vendors, hints = None, [], [], {}
    try:
        client = _client(workspace)
        if request.method == "POST" and request.POST.get("action") == "categorize":
            try:
                message = _categorize(request, workspace, client)
            except svc.CashError as e:
                error = str(e)
        cash = svc.overview(client, today)
        if cash.waiting:
            accounts = [a for a in list_accounts(client) if a.get("account_type") != "UNCATEGORIZED" and a.get("account_subtype") != "BANK"]
            vendors = list_vendors(client)
            for s in bankmatch.suggestions(workspace, client, today):
                hints[int(s.line["id"])] = ("Looks like a fund distribution" if s.kind == "distribution" else "Looks like a capital call payment")
            for line in cash.waiting:
                line.hint = hints.get(line.id, "")
    except CAMPFIRE_ERRORS as e:
        error = error or f"Couldn't read cash from Campfire ({type(e).__name__}). Check that the workspace is connected."
    grouped = [(title, [(int(a["id"]), account_label(a)) for a in accounts if a.get("account_type") in types]) for title, types in GROUPS]
    return render(request, "gl/cash.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "cash": cash, "today": today,
        "message": message, "error": error, "account_groups": [(t, items) for t, items in grouped if items],
        "vendors": [(int(v["id"]), v.get("name", "")) for v in vendors], "months": svc.by_month(cash.lines) if cash else [],
        "action": pages.page_url(request, framed, "gl:cash"), "calls_url": pages.page_url(request, framed, "gl:calls"),
        "statements_url": pages.page_url(request, framed, "gl:statements"), "lookback": svc.LOOKBACK_DAYS,
    })


frame_cash = pages.framed(cash_page)
standalone_cash = pages.standalone(cash_page)
