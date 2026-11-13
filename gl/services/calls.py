"""Capital calls as Campfire bills: send → approved in Campfire → paid in Campfire → contribution booked.

    send(call, client=CampfireClient(workspace), actor="...")   # a draft bill to the fund's GP
    sync(call, client)                                         # look the bill up; book the contribution once paid

The bill goes to the commitment's Campfire vendor (the GP), in the Campfire entity that owns the fund interest, on
that entity's "Investments at cost" account (the GL bridge's mapping) with the same FO tags. Its `source` /
`source_id` ("Family Office" / fo-call-<pk>) let the app find it whatever happens to the request. With the draft scope
the app requests, Campfire answers 202 and the bill waits for approval; Bill events (or `refresh_calls`) then move the
call on. Payment books the contribution (dated the bill's paid date), which lowers the unfunded commitment; the GL
bridge skips that contribution because the bill already put it in the GL.
"""

from datetime import date
from typing import Any

import requests
from django.utils import timezone

from campfire_integration.client import CampfireClient, NotConnected
from campfire_integration.models import Workspace
from gl.models import SOURCE, Role
from gl.services.posting import Mappings, Tags
from gl.services.rules import Leg
from gl.services.setup import _all
from investments.models import CapitalCall, Transaction
from investments.services.privates import mark_call_paid

BILL_PATH = "/coa/api/v1/bill"
VENDOR_PATH = "/coa/api/vendor"
TERMS = (7, 10, 15, 20, 30, 40, 45, 60, 90, 105, 120)
S = CapitalCall.Status


class CallError(Exception):
    pass


def _terms(notice: date, due: date) -> str:
    days = (due - notice).days
    if days <= 0:
        return "due_on_receipt"
    return f"net_{next((t for t in TERMS if t >= days), TERMS[-1])}"


def problems(call: CapitalCall) -> list[str]:
    """What stops this call from going to Campfire as a bill."""
    c = call.commitment
    out = []
    if call.status not in (S.RECEIVED, S.APPROVED):
        out.append(f"The call is {call.get_status_display().lower()}.")
    entity = c.account.entity
    if entity is None or entity.campfire_entity_id is None:
        out.append(f"{c.account.owner} isn't linked to a Campfire entity (General ledger → Entities).")
    if not c.campfire_vendor_id:
        out.append(f"Choose the Campfire vendor (the GP) for {c.instrument}.")
    if entity is not None and entity.campfire_entity_id is not None:
        mapping = Mappings(c.account.family.workspace).resolve(Role.INVESTMENTS, account=c.account, campfire_entity_id=entity.campfire_entity_id,
                                                               asset_class_id=c.instrument.asset_class_id)
        if mapping is None:
            out.append("Map 'Investments at cost' to a GL account (General ledger → GL accounts by role).")
    if call.amount <= 0:
        out.append("The amount must be positive.")
    return out


def payload(call: CapitalCall) -> dict[str, Any]:
    c = call.commitment
    ws = c.account.family.workspace
    cf_entity = c.account.entity.campfire_entity_id  # type: ignore[union-attr]
    mapping = Mappings(ws).resolve(Role.INVESTMENTS, account=c.account, campfire_entity_id=cf_entity, asset_class_id=c.instrument.asset_class_id)
    probe = Transaction(account=c.account, instrument=c.instrument, txn_type=Transaction.Type.CONTRIBUTION, amount=call.amount)
    tags = Tags(ws).for_leg(Leg(Role.INVESTMENTS, call.amount, probe), set())
    return {
        "bill_number": (call.source_ref or f"FO-CALL-{call.pk}")[:120],
        "bill_date": call.notice_date.isoformat(),
        "item_date": call.notice_date.isoformat(),
        "due_date": call.due_date.isoformat(),
        "terms": _terms(call.notice_date, call.due_date),
        "entity": str(cf_entity),
        "vendor": str(c.campfire_vendor_id),
        "currency": c.currency,
        "message_on_bill": f"Capital call: {c.instrument}, notice of {call.notice_date:%B} {call.notice_date.day}, {call.notice_date.year}",
        "source": SOURCE,
        "source_id": call.bill_source_id,
        "lines": [{
            "account": str(mapping.campfire_account_id),  # type: ignore[union-attr]
            "amount": float(call.amount),
            "description": f"Capital contribution to {c.instrument} ({c.account})"[:255],
            "tags": [{"id": str(t)} for t in tags],
        }],
    }


def send(call: CapitalCall, client: CampfireClient, actor: str = "") -> CapitalCall:
    issues = problems(call)
    if issues:
        raise CallError(" ".join(issues))
    call.bill_sent_at, call.bill_sent_by = timezone.now(), actor
    try:
        r = client.request("POST", BILL_PATH, json=payload(call))
    except (requests.RequestException, NotConnected) as e:
        # Campfire may have saved it; the next sync looks for it by source_id.
        call.status, call.bill_note = S.SUBMITTED, f"No answer from Campfire ({type(e).__name__}); checking for the bill later."
        call.save()
        return call
    try:
        body: Any = r.json()
    except ValueError:
        body = {"text": r.text[:2000]}
    call.bill_response = body
    if r.status_code in (200, 201) and isinstance(body, dict) and body.get("id"):
        call.status, call.campfire_bill_id, call.bill_note = S.BILLED, int(body["id"]), ""
    elif r.status_code == 202:
        call.status, call.bill_note = S.SUBMITTED, ""
    else:
        call.bill_note = f"Campfire refused the bill ({r.status_code}): {r.text[:500]}"
        call.save()
        raise CallError(call.bill_note)
    call.save()
    return call


def find_bill(call: CapitalCall, client: CampfireClient) -> dict[str, Any] | None:
    if call.campfire_bill_id:
        r = client.request("GET", f"{BILL_PATH}/{call.campfire_bill_id}")
        if r.status_code == 200:
            bill = r.json()
            return None if bill.get("is_deleted") else bill
        if r.status_code != 404:
            r.raise_for_status()
        return None
    params = {"vendor": call.commitment.campfire_vendor_id, "q": call.source_ref or f"FO-CALL-{call.pk}", "limit": 50}
    for bill in client.get_json(BILL_PATH, params=params).get("results", []):
        if bill.get("source_id") == call.bill_source_id and not bill.get("is_deleted"):
            return bill
    return None


def apply_bill(call: CapitalCall, bill: dict[str, Any] | None) -> CapitalCall:
    """Move the call on from what Campfire says about its bill."""
    if call.status in (S.PAID, S.CANCELLED):
        return call
    if bill is None:
        if call.status == S.BILLED and call.campfire_bill_id:
            call.status, call.campfire_bill_id = S.RECEIVED, None
            call.bill_note = "The bill was deleted in Campfire; the call can be sent again."
            call.save()
        return call
    call.campfire_bill_id = int(bill["id"])
    status = (bill.get("payment_status") or bill.get("status") or "").lower()
    if status == "void":
        call.status, call.campfire_bill_id, call.bill_note = S.RECEIVED, None, "The bill was voided in Campfire; the call can be sent again."
        call.save()
        return call
    call.status = S.BILLED
    call.bill_note = ""
    call.save()
    if status == "paid":
        paid_on = date.fromisoformat(bill["paid_date"]) if bill.get("paid_date") else timezone.localdate()
        mark_call_paid(call, paid_on, source_ref=f"campfire-bill:{bill['id']}")
        call.refresh_from_db()
    return call


def sync(call: CapitalCall, client: CampfireClient) -> CapitalCall:
    return apply_bill(call, find_bill(call, client))


def refresh_calls(workspace: Workspace, client: CampfireClient) -> int:
    """Check every call that is with Campfire. Returns how many changed status."""
    changed = 0
    for call in CapitalCall.objects.filter(commitment__account__family__workspace=workspace, status__in=[S.SUBMITTED, S.BILLED]).select_related(
            "commitment__account__entity", "commitment__instrument"):
        before = call.status
        if sync(call, client).status != before:
            changed += 1
    return changed


def list_vendors(client: CampfireClient) -> list[dict[str, Any]]:
    return sorted((v for v in _all(client, VENDOR_PATH) if not v.get("is_deleted")), key=lambda v: (v.get("name") or "").lower())


def call_for_bill(workspace: Workspace, bill: dict[str, Any]) -> CapitalCall | None:
    sid = bill.get("source_id") or ""
    if bill.get("source") != SOURCE or not sid.startswith("fo-call-") or not sid[8:].isdigit():
        return None
    return CapitalCall.objects.filter(commitment__account__family__workspace=workspace, pk=int(sid[8:])).first()
