"""The Capital calls page: open calls and their bills in Campfire, the GP vendor per fund, and recording distributions."""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.client import CampfireClient, CampfireError, NotConnected
from campfire_integration.models import Workspace
from gl.services import calls as svc
from investments.models import CapitalCall, Commitment, Transaction
from investments.services.privates import summarize
from records.models import AuditEntry
from records.registry import check_ledger

CAMPFIRE_ERRORS = (CampfireError, NotConnected, requests.RequestException)
T = Transaction.Type


def _client(workspace: Workspace) -> CampfireClient:
    return CampfireClient(workspace)


def _int(v: Any) -> int:
    return int(v) if str(v or "").isdigit() else 0


def _money(v: Any, label: str) -> Decimal:
    text = str(v or "").replace(",", "").replace("$", "").strip()
    if not text:
        return Decimal(0)
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise svc.CallError(f"{label}: enter a number.") from None
    if value < 0:
        raise svc.CallError(f"{label} can't be negative.")
    return value.quantize(Decimal("0.01"))


def _commitments(workspace: Workspace):  # type: ignore[no-untyped-def]
    return Commitment.objects.filter(account__family__workspace=workspace).select_related("account__entity", "account__family", "instrument")


def record_distribution(workspace: Workspace, commitment: Commitment, when: date, roc: Decimal, recallable: Decimal,
                        income: Decimal, gains: Decimal, note: str, actor: str) -> list[Transaction]:
    if recallable > roc:
        raise svc.CallError("The recallable part can't exceed the return of capital.")
    parts = [(T.DISTRIBUTION_ROC, roc - recallable, False, "Return of capital"), (T.DISTRIBUTION_ROC, recallable, True, "Recallable return of capital"),
             (T.DISTRIBUTION_INCOME, income, False, "Income"), (T.DISTRIBUTION_GAIN, gains, False, "Realized gain")]
    if not any(amount for _, amount, _, _ in parts):
        raise svc.CallError("Enter at least one amount.")
    created = []
    with transaction.atomic():
        for t, amount, recall, label in parts:
            if amount:
                txn = Transaction.objects.create(account=commitment.account, instrument=commitment.instrument, txn_type=t, trade_date=when,
                                                 amount=amount, is_recallable=recall, description=(f"{label}: {note}" if note else label)[:255])
                created.append(txn)
                AuditEntry.objects.create(workspace=workspace, actor=actor, action=AuditEntry.Action.CREATED, kind="transactions",
                                          object_id=str(txn.pk), object_repr=str(txn)[:255], changes={"distribution": ["", f"{label} {amount}"]})
        try:
            check_ledger(commitment.account)
        except ValidationError as e:
            raise svc.CallError(" ".join(e.messages)) from e
    return created


def _handle(request: HttpRequest, workspace: Workspace) -> str:
    action = request.POST.get("action", "")
    actor = getattr(request, "fo_actor", "")
    if action == "send":
        call = CapitalCall.objects.filter(commitment__account__family__workspace=workspace, pk=_int(request.POST.get("call"))).first()
        if call is None:
            raise svc.CallError("That call isn't in this workspace.")
        call = svc.send(call, _client(workspace), actor)
        return (f"Bill for {call.commitment.instrument} sent: awaiting approval in Campfire." if call.status == call.Status.SUBMITTED
                else f"Bill for {call.commitment.instrument} created in Campfire (#{call.campfire_bill_id}).")
    if action == "refresh":
        n = svc.refresh_calls(workspace, _client(workspace))
        return f"Checked Campfire: {n} call{'s' if n != 1 else ''} moved on."
    if action == "vendor":
        commitment = _commitments(workspace).filter(pk=_int(request.POST.get("commitment"))).first()
        vendor_id = _int(request.POST.get("vendor"))
        vendors = {int(v["id"]): v for v in svc.list_vendors(_client(workspace))}
        if commitment is None or (vendor_id and vendor_id not in vendors):
            raise svc.CallError("Choose a fund and one of the workspace's Campfire vendors.")
        commitment.campfire_vendor_id = vendor_id or None
        commitment.campfire_vendor_name = vendors[vendor_id]["name"] if vendor_id else ""
        commitment.save(update_fields=["campfire_vendor_id", "campfire_vendor_name"])
        return f"{commitment.instrument}: bills go to {commitment.campfire_vendor_name or 'no vendor'}."
    if action == "match_vendors":
        by_name = {" ".join(str(v.get("name", "")).lower().split()): v for v in svc.list_vendors(_client(workspace))}
        matched = 0
        for c in _commitments(workspace).filter(campfire_vendor_id__isnull=True):
            v = by_name.get(" ".join((c.instrument.sponsor or "").lower().split()))
            if v:
                c.campfire_vendor_id, c.campfire_vendor_name = int(v["id"]), v["name"]
                c.save(update_fields=["campfire_vendor_id", "campfire_vendor_name"])
                matched += 1
        return f"Matched {matched} fund{'s' if matched != 1 else ''} to the Campfire vendor named like its manager / GP."
    if action == "distribution":
        commitment = _commitments(workspace).filter(pk=_int(request.POST.get("commitment"))).first()
        if commitment is None:
            raise svc.CallError("Choose the fund.")
        try:
            when = date.fromisoformat(request.POST.get("date", ""))
        except ValueError:
            raise svc.CallError("Enter the distribution date.") from None
        made = record_distribution(workspace, commitment, when, _money(request.POST.get("roc"), "Return of capital"),
                                   _money(request.POST.get("recallable"), "Recallable"), _money(request.POST.get("income"), "Income"),
                                   _money(request.POST.get("gains"), "Gains"), request.POST.get("note", "").strip(), actor)
        return f"Recorded the {commitment.instrument} distribution ({sum(t.amount for t in made):,})."
    raise svc.CallError("Unknown action.")


def calls_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    message = error = ""
    if request.method == "POST":
        try:
            message = _handle(request, workspace)
        except svc.CallError as e:
            error = str(e)
        except CAMPFIRE_ERRORS as e:
            error = f"Campfire request failed: {e}"
    elif request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])

    today = timezone.localdate()
    open_calls = list(CapitalCall.objects.filter(commitment__account__family__workspace=workspace, status__in=CapitalCall.OPEN_STATUSES)
                      .select_related("commitment__account__entity", "commitment__account__family", "commitment__instrument__asset_class"))
    rows = [(c, svc.problems(c) if c.status in (c.Status.RECEIVED, c.Status.APPROVED) else []) for c in open_calls]
    paid = CapitalCall.objects.filter(commitment__account__family__workspace=workspace, status=CapitalCall.Status.PAID).select_related(
        "commitment__instrument").order_by("-paid_on")[:10]
    funds = [(c, summarize(c, today)) for c in _commitments(workspace)]
    vendors: list[dict[str, Any]] = []
    campfire_error = ""
    try:
        vendors = svc.list_vendors(_client(workspace))
    except CAMPFIRE_ERRORS as e:
        campfire_error = f"Couldn't load vendors from Campfire ({type(e).__name__})."
    return render(request, "gl/calls.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "rows": rows, "paid": paid,
        "funds": funds, "vendors": vendors, "campfire_error": campfire_error, "message": message, "error": error, "today": today,
        "action": pages.page_url(request, framed, "gl:calls"), "new_call_url": pages.page_url(request, framed, "records:new", "capital-calls"),
        "gl_url": pages.page_url(request, framed, "gl:gl"),
    })


frame_calls = pages.framed(calls_page)
standalone_calls = pages.standalone(calls_page)
