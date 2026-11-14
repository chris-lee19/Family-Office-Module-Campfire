"""The Bills page: enter a bill, approve it, see what is open in Campfire and mark it paid."""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.client import CampfireClient, CampfireError, NotConnected
from campfire_integration.models import Workspace
from gl.models import BillRequest
from gl.services import bills as svc
from gl.services import charts
from gl.services import statements
from gl.services.calls import list_vendors
from gl.services.setup import account_label, list_accounts

CAMPFIRE_ERRORS = (CampfireError, NotConnected, requests.RequestException)
EXPENSE_TYPES = ("OPERATING_EXPENSES", "OTHER_EXPENSE", "COGS")


def _client(workspace: Workspace) -> CampfireClient:
    return CampfireClient(workspace)


def _int(v: Any) -> int:
    return int(v) if str(v or "").isdigit() else 0


def _handle(request: HttpRequest, workspace: Workspace, client: CampfireClient, ref: dict[str, Any], today: date) -> str:
    action = request.POST.get("action", "")
    actor, actor_id = getattr(request, "fo_actor", ""), getattr(request, "fo_actor_id", "")
    if action == "enter":
        req = svc.submit(workspace, request.POST, ref["entities"], ref["vendors"], ref["accounts"], actor, actor_id)
        return f"Bill {req.bill_number} from {req.vendor_name} entered: it now waits for approval."
    if action in ("approve", "reject"):
        req = BillRequest.objects.filter(workspace=workspace, pk=_int(request.POST.get("bill"))).first()
        if req is None:
            raise svc.BillError("That bill isn't in this workspace.")
        if action == "reject":
            svc.reject(req, actor, actor_id, request.POST.get("note", ""))
            return f"Bill {req.bill_number} from {req.vendor_name} rejected."
        req = svc.approve(req, client, actor, actor_id)
        return (f"Bill {req.bill_number} approved and in Campfire's payables." if req.status == req.Status.SENT
                else f"Bill {req.bill_number} approved here; Campfire holds it for its own approval too.")
    if action == "pay":
        bill = next((b for b in svc.bills(client, today) if b.id == _int(request.POST.get("bill"))), None)
        account = _int(request.POST.get("account"))
        if bill is None or account not in ref["cash"]:
            raise svc.BillError("Choose an open bill and the account it is paid from.")
        try:
            when = date.fromisoformat(request.POST.get("date", ""))
            amount = Decimal(str(request.POST.get("amount") or bill.due).replace(",", "").replace("$", "")).quantize(Decimal("0.01"))
        except (ValueError, InvalidOperation):
            raise svc.BillError("Enter the payment date and amount.") from None
        outcome = svc.pay(client, bill, account, ref["cash"][account], when, amount)
        return f"{bill.vendor} {bill.number}: {amount:,.2f} {outcome}."
    raise svc.BillError("Unknown action.")


def bills_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    today = timezone.localdate()
    message = error = ""
    ref: dict[str, Any] = {"entities": {}, "vendors": {}, "accounts": {}, "cash": {}}
    rows: list[svc.Bill] = []
    try:
        client = _client(workspace)
        accounts = list_accounts(client)
        ref = {
            "entities": {s.entity_id: s.label for s in statements.scopes(client) if not s.rollup},
            "vendors": {int(v["id"]): v.get("name", "") for v in list_vendors(client)},
            "accounts": {int(a["id"]): account_label(a) for a in accounts
                         if a.get("account_type") in EXPENSE_TYPES or a.get("account_subtype") in ("PREPAID", "OTHER_ASSET", "OTHER_CURRENT_ASSET")},
            "cash": {int(a["id"]): account_label(a) for a in accounts if a.get("account_subtype") == "BANK"},
        }
        if request.method == "POST":
            try:
                message = _handle(request, workspace, client, ref, today)
            except svc.BillError as e:
                error = str(e)
        rows = svc.bills(client, today)
    except CAMPFIRE_ERRORS as e:
        error = error or f"Couldn't read bills from Campfire ({type(e).__name__}). Check that the workspace is connected."
    # What was typed comes back when the entry is refused, so nothing has to be typed twice.
    retry = bool(error) and request.POST.get("action") == "enter"
    fields = ("vendor", "entity", "account", "bill_number", "bill_date", "due_date", "amount", "description")
    entered = {f: (request.POST.get(f, "") if retry else "") for f in fields}
    entered["bill_date"] = entered["bill_date"] or today.isoformat()
    open_bills = [b for b in rows if b.is_open]
    pending = list(BillRequest.objects.filter(workspace=workspace, status=BillRequest.Status.PENDING))
    recent = BillRequest.objects.filter(workspace=workspace).exclude(status=BillRequest.Status.PENDING)[:8]
    soon = [b for b in open_bills if b.days is not None and 0 <= b.days <= 7]
    return render(request, "gl/bills.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "today": today,
        "message": message, "error": error, "open_bills": open_bills, "paid": [b for b in rows if b.status == "paid"][:10],
        "pending": pending, "recent": recent, "ref": ref, "action": pages.page_url(request, framed, "gl:bills"),
        "stats": {"open": sum((b.due for b in open_bills), svc.ZERO), "count": len(open_bills),
                  "overdue": sum((b.due for b in open_bills if b.overdue), svc.ZERO), "overdue_count": sum(1 for b in open_bills if b.overdue),
                  "soon": sum((b.due for b in soon), svc.ZERO), "soon_count": len(soon)},
        "me": getattr(request, "fo_actor_id", ""), "entered": entered, "reopen": retry,
        "ageing": [{**bar, "count": count} for bar, (_, _, count) in zip(charts.ranked([(label, amount) for label, amount, _ in svc.ageing(open_bills)],
                                                                                    sort=False, bad=("Overdue",)), svc.ageing(open_bills))],
        "by_entity": charts.ranked(list(_by(open_bills, "entity").items())), "by_payee": charts.ranked(list(_by(open_bills, "vendor").items()))[:6],
    })


def _by(open_bills: list[svc.Bill], attr: str) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for b in open_bills:
        out[getattr(b, attr) or "—"] = out.get(getattr(b, attr) or "—", svc.ZERO) + b.due
    return out


frame_bills = pages.framed(bills_page)
standalone_bills = pages.standalone(bills_page)
