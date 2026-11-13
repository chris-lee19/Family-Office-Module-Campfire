"""Family Office inside Campfire's own records: panels and drawer buttons on bills and journal entries.

- A bill: its capital call (fund, commitment, called / unfunded, status), with "check payment". A bill someone keyed
  in Campfire for a fund's GP can be recorded as a capital call from here, so paying it books the contribution.
- The bill list: capital-call bills awaiting approval or payment.
- A journal entry: where a Family Office entry came from: its lines, the transactions behind them, and the file and
  line each came from. (Campfire shows journal-entry panels and buttons once it adds them to that page.)
"""

from datetime import date
from decimal import Decimal
from typing import Any

import requests
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.client import CampfireClient, CampfireError, NotConnected
from campfire_integration.models import Workspace
from gl.models import SOURCE, JournalPosting
from gl.services import calls as call_svc
from gl.services.posting import JE_PATH, mark_posted, posting_for_source_id
from investments.models import CapitalCall, Commitment
from investments.services.privates import summarize
from records.models import AuditEntry

CAMPFIRE_ERRORS = (CampfireError, NotConnected, requests.RequestException)


def _ctx(request: HttpRequest, workspace: Workspace, **kw: Any) -> dict[str, Any]:
    return {"workspace": workspace, "framed": True, "frame_token": getattr(request, "fo_frame_token", ""), "panel": True, **kw}


def _calls(workspace: Workspace):  # type: ignore[no-untyped-def]
    return CapitalCall.objects.filter(commitment__account__family__workspace=workspace).select_related(
        "commitment__account__entity", "commitment__account__family", "commitment__instrument")


# --------------------------------------------------------------------------------------------------- bills


def bill_page(request: HttpRequest, workspace: Workspace, framed: bool, record_id: int) -> HttpResponse:
    message = error = ""
    call = _calls(workspace).filter(campfire_bill_id=record_id).first()
    bill: dict[str, Any] | None = None
    candidates: list[Commitment] = []
    client = CampfireClient(workspace)
    try:
        if call is None or request.method == "POST":
            r = client.request("GET", f"{call_svc.BILL_PATH}/{record_id}")
            bill = r.json() if r.status_code == 200 else None
        if call is None and bill is not None:
            call = call_svc.call_for_bill(workspace, bill)  # our draft, approved since: link it
            if call is not None:
                call = call_svc.apply_bill(call, bill)
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "sync" and call is not None:
                call = call_svc.apply_bill(call, bill)
                message = f"Status: {call.get_status_display()}."
            elif action == "link" and call is None and bill is not None:
                commitment = Commitment.objects.filter(account__family__workspace=workspace, pk=int(request.POST.get("commitment") or 0),
                                                       campfire_vendor_id=bill.get("vendor")).first()
                if commitment is None:
                    error = "Choose one of this vendor's funds."
                else:
                    call = link_bill(workspace, commitment, bill, getattr(request, "fo_actor", ""))
                    message = f"Recorded as a capital call to {commitment.instrument}." + (" It's paid: the contribution is booked." if call.status == call.Status.PAID else "")
    except CAMPFIRE_ERRORS as e:
        error = f"Couldn't reach Campfire ({type(e).__name__})."
    if call is None and bill is not None:
        candidates = list(Commitment.objects.filter(account__family__workspace=workspace, campfire_vendor_id=bill.get("vendor")).select_related("instrument", "account"))
    summary = summarize(call.commitment, timezone.localdate()) if call is not None else None
    return render(request, "gl/panel_bill.html", _ctx(request, workspace, call=call, bill=bill, candidates=candidates, summary=summary,
                                                       message=message, error=error,
                                                       action=pages.page_url(request, True, "gl:bill_panel", record_id),
                                                       calls_url=pages.page_url(request, True, "gl:calls")))


@transaction.atomic
def link_bill(workspace: Workspace, commitment: Commitment, bill: dict[str, Any], actor: str) -> CapitalCall:
    """A GP bill keyed in Campfire becomes the capital call it is, linked to that bill (once)."""
    existing = _calls(workspace).filter(campfire_bill_id=int(bill["id"])).first()
    if existing is not None:
        return existing
    bill_date = date.fromisoformat(bill.get("bill_date") or timezone.localdate().isoformat())
    call = CapitalCall.objects.create(
        commitment=commitment, notice_date=bill_date, due_date=date.fromisoformat(bill.get("due_date") or bill_date.isoformat()),
        amount=Decimal(str(bill.get("total_amount") or bill.get("amount_due") or 0)).quantize(Decimal("0.01")),
        status=CapitalCall.Status.BILLED, campfire_bill_id=int(bill["id"]), source_ref=(bill.get("bill_number") or "")[:255],
        bill_note="Recorded from a bill entered in Campfire.", bill_sent_by=actor,
    )
    AuditEntry.objects.create(workspace=workspace, actor=actor, action=AuditEntry.Action.CREATED, kind="capital-calls", object_id=str(call.pk),
                              object_repr=str(call)[:255], changes={"from Campfire bill": ["", str(bill["id"])]})
    return call_svc.apply_bill(call, bill)


def bills_list_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    with_campfire = list(_calls(workspace).filter(status__in=[CapitalCall.Status.SUBMITTED, CapitalCall.Status.BILLED]).order_by("due_date"))
    not_sent = list(_calls(workspace).filter(status__in=[CapitalCall.Status.RECEIVED, CapitalCall.Status.APPROVED]).order_by("due_date"))
    return render(request, "gl/panel_bills.html", _ctx(request, workspace, with_campfire=with_campfire, not_sent=not_sent,
                                                        today=timezone.localdate(), calls_url=pages.page_url(request, True, "gl:calls")))


# ---------------------------------------------------------------------------------------- journal entries


def journal_page(request: HttpRequest, workspace: Workspace, framed: bool, record_id: int) -> HttpResponse:
    posting = JournalPosting.objects.filter(workspace=workspace, campfire_journal_id=record_id).first()
    error = ""
    if posting is None:
        try:
            je = CampfireClient(workspace).get_json(f"{JE_PATH}/{record_id}")
            if je.get("source") == SOURCE:
                posting = posting_for_source_id(workspace, je.get("source_id"))
                if posting is not None and posting.campfire_journal_id != record_id:
                    posting = mark_posted(posting, record_id)
        except CAMPFIRE_ERRORS as e:
            error = f"Couldn't reach Campfire ({type(e).__name__})."
    items = []
    if posting is not None:
        items = list(posting.items.select_related("transaction__account", "transaction__instrument", "transaction__batch").order_by(
            "transaction__trade_date", "transaction__id"))
    return render(request, "gl/panel_journal.html", _ctx(request, workspace, posting=posting, items=items, error=error,
                                                          gl_url=pages.page_url(request, True, "gl:gl")))


bill_panel = pages.record_framed(bill_page, object_type="bill", surfaces=("panel:detail", "panel:edit", "detail_header"))
bills_list_panel = pages.framed(bills_list_page, surfaces=("panel:list",))
journal_panel = pages.record_framed(journal_page, object_type="journal_entry", surfaces=("panel:detail", "panel:edit", "detail_header"))
