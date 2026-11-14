"""Bills and payments. Campfire holds the payables; the app is where bills are entered, approved and marked paid.

A bill entered here waits for approval in the app (`BillRequest`): someone other than whoever entered it, when the
workspace asks for a second person. Approving sends it to Campfire. With a draft-tier connection Campfire answers 202
and holds it for its own approval as well; with a write-tier connection it posts straight away.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from django.utils import timezone

from campfire_integration.client import CampfireClient
from campfire_integration.models import Workspace
from gl.models import SOURCE, BillRequest
from gl.services import lifestyle
from gl.services.calls import BILL_PATH, _terms
from records.models import AuditEntry, ControlSettings

ZERO = Decimal(0)
S = BillRequest.Status


class BillError(Exception):
    pass


@dataclass
class Bill:
    id: int
    number: str
    vendor: str
    entity: str
    bill_date: date | None
    due_date: date | None
    total: Decimal
    due: Decimal
    status: str  # open | partial | paid | voided
    description: str
    account: str
    source: str
    days: int | None = None  # until due (negative: overdue)

    @property
    def is_open(self) -> bool:
        return self.status in ("open", "partial") and self.due > 0

    @property
    def overdue(self) -> bool:
        return self.is_open and self.days is not None and self.days < 0


def _dec(v: Any) -> Decimal:
    return Decimal(str(v or 0)).quantize(Decimal("0.01"))


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _all(client: CampfireClient, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    query: dict[str, Any] | None = dict(params or {}, limit=200)
    while path and len(items) < 2000:
        page = client.get_json(path, params=query)
        items += page.get("results", [])
        nxt = page.get("next")
        path, query = (nxt.split(".com", 1)[-1], None) if nxt else ("", None)
    return items


def bills(client: CampfireClient, today: date) -> list[Bill]:
    out = []
    for b in _all(client, BILL_PATH):
        if b.get("is_deleted"):
            continue
        due_date = _date(b.get("due_date"))
        total, due = _dec(b.get("total_amount")), _dec(b.get("amount_due"))
        status = str(b.get("payment_status") or b.get("status") or "open").lower()
        if status not in ("paid", "voided"):
            status = "partial" if ZERO < due < total else ("paid" if not due and total else "open")
        line = (b.get("lines") or [{}])[0]
        out.append(Bill(int(b["id"]), b.get("bill_number") or "", str(b.get("vendor_name") or "").split(" (VEN-")[0], b.get("entity_name") or "",
                        _date(b.get("bill_date")), due_date, total, due, status, b.get("message_on_bill") or line.get("description") or "",
                        line.get("account_name") or "", b.get("source") or "", (due_date - today).days if due_date else None))
    return sorted(out, key=lambda b: (not b.is_open, b.due_date or date.max, b.id))


AGEING = (("Overdue", lambda d: d < 0), ("Due in 7 days", lambda d: 0 <= d <= 7), ("8 to 30 days", lambda d: 8 <= d <= 30), ("Later", lambda d: d > 30))


def ageing(open_bills: list[Bill]) -> list[tuple[str, Decimal, int]]:
    """Open bills by how soon they are due: (label, amount, how many), overdue first."""
    out = []
    for label, test in AGEING:
        chosen = [b for b in open_bills if test(b.days if b.days is not None else 0)]
        out.append((label, sum((b.due for b in chosen), ZERO), len(chosen)))
    return out


def _money(raw: Any) -> Decimal:
    try:
        value = Decimal(str(raw or "").replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        raise BillError("Enter the amount as a number.") from None
    if value <= 0:
        raise BillError("The amount must be more than zero.")
    return value.quantize(Decimal("0.01"))


def submit(workspace: Workspace, data: dict[str, Any], entities: dict[int, str], vendors: dict[int, str], accounts: dict[int, str],
           actor: str, actor_id: str) -> BillRequest:
    """Enter a bill for approval. `entities`, `vendors`, `accounts`: what the workspace has in Campfire (id -> label)."""
    as_int = lambda v: int(v) if str(v or "").isdigit() else 0  # noqa: E731
    entity, vendor, account = as_int(data.get("entity")), as_int(data.get("vendor")), as_int(data.get("account"))
    if entity not in entities or vendor not in vendors or account not in accounts:
        raise BillError("Choose the entity, the payee and the account.")
    bill_date, due_date = _date(data.get("bill_date")), _date(data.get("due_date"))
    if bill_date is None or due_date is None or due_date < bill_date:
        raise BillError("Enter the bill date and a due date on or after it.")
    number, text = str(data.get("bill_number") or "").strip(), str(data.get("description") or "").strip()
    if not number or not text:
        raise BillError("Enter the bill number and what the bill is for.")
    if BillRequest.objects.filter(workspace=workspace, campfire_vendor_id=vendor, bill_number__iexact=number).exclude(status__in=[S.REJECTED, S.FAILED]).exists():
        raise BillError(f"Bill {number} from {vendors[vendor]} was already entered.")
    req = BillRequest.objects.create(
        workspace=workspace, campfire_entity_id=entity, entity_label=entities[entity][:255], campfire_vendor_id=vendor, vendor_name=vendors[vendor][:255],
        bill_number=number[:120], bill_date=bill_date, due_date=due_date, description=text[:255], campfire_account_id=account,
        account_label=accounts[account][:255], amount=_money(data.get("amount")), created_by=actor, created_by_id=actor_id,
        tag_ids=lifestyle.picked(workspace, data))
    AuditEntry.objects.create(workspace=workspace, actor=actor, action=AuditEntry.Action.CREATED, kind="bills", object_id=str(req.pk),
                              object_repr=str(req)[:255], changes={"entered": ["", f"{req.vendor_name} {req.bill_number} {req.amount}"]})
    return req


def payload(req: BillRequest) -> dict[str, Any]:
    return {
        "bill_number": req.bill_number, "bill_date": req.bill_date.isoformat(), "item_date": req.bill_date.isoformat(), "due_date": req.due_date.isoformat(),
        "terms": _terms(req.bill_date, req.due_date), "entity": str(req.campfire_entity_id), "vendor": str(req.campfire_vendor_id), "currency": "USD",
        "message_on_bill": req.description, "source": SOURCE, "source_id": req.source_id,
        "lines": [{"account": str(req.campfire_account_id), "amount": float(req.amount), "description": req.description, "tags": list(req.tag_ids or [])}],
    }


def _decide(req: BillRequest, actor: str, actor_id: str, what: str) -> None:
    if req.status != S.PENDING:
        raise BillError("That bill has already been decided.")
    if ControlSettings.for_workspace(req.workspace).require_second_person and actor_id and actor_id == req.created_by_id:
        raise BillError(f"You entered this bill, so someone else has to {what} it (preparer ≠ approver).")


def approve(req: BillRequest, client: CampfireClient, actor: str, actor_id: str) -> BillRequest:
    _decide(req, actor, actor_id, "approve")
    r = client.request("POST", BILL_PATH, json=payload(req))
    req.decided_by, req.decided_by_id, req.decided_at = actor, actor_id, timezone.now()
    if r.status_code == 202:
        req.status, req.error = S.AWAITING, ""
    elif r.status_code in (200, 201):
        try:
            req.campfire_bill_id = r.json().get("id")
        except ValueError:
            pass
        req.status, req.error = S.SENT, ""
    else:
        req.decided_by = req.decided_by_id = ""  # not decided: it can be approved again once the cause is fixed
        req.decided_at = None
        req.error = f"Campfire answered {r.status_code}: {r.text[:500]}"
        req.save()
        raise BillError(f"Campfire refused the bill ({r.status_code}). It is still waiting here; nothing was sent.")
    req.save()
    AuditEntry.objects.create(workspace=req.workspace, actor=actor, action=AuditEntry.Action.UPDATED, kind="bills", object_id=str(req.pk),
                              object_repr=str(req)[:255], changes={"status": ["Waiting for approval", req.get_status_display()]})
    return req


def reject(req: BillRequest, actor: str, actor_id: str, note: str) -> BillRequest:
    _decide(req, actor, actor_id, "reject")
    if not note.strip():
        raise BillError("Say why the bill is rejected.")
    req.status, req.note, req.decided_by, req.decided_by_id, req.decided_at = S.REJECTED, note.strip()[:255], actor, actor_id, timezone.now()
    req.save()
    AuditEntry.objects.create(workspace=req.workspace, actor=actor, action=AuditEntry.Action.UPDATED, kind="bills", object_id=str(req.pk),
                              object_repr=str(req)[:255], changes={"status": ["Waiting for approval", f"Rejected: {req.note}"]})
    return req


def pay(client: CampfireClient, bill: Bill, account_id: int, account_name: str, when: date, amount: Decimal) -> str:
    """Record a payment of an open bill from a cash account. Returns what happened."""
    if not bill.is_open:
        raise BillError("That bill isn't open.")
    if amount <= 0 or amount > bill.due:
        raise BillError(f"Pay between 0.01 and the {bill.due:,.2f} still due.")
    r = client.request("POST", f"{BILL_PATH}/{bill.id}/pay", json={"empty_transactions": [
        {"account_id": account_id, "account_name": account_name, "amount": float(amount), "payment_date": when.isoformat(),
         "memo": f"Payment of {bill.number}", "external_id": f"fo-pay-{bill.id}-{when.isoformat()}"}]})
    if r.status_code == 202:
        return "sent for approval"
    if r.status_code in (401, 403):
        raise BillError("The connection doesn't allow recording payments yet. Reconnect the workspace so the app gets that permission.")
    if r.status_code >= 400:
        raise BillError(f"Campfire refused the payment ({r.status_code}): {r.text[:300]}")
    return "paid" if amount == bill.due else "part-paid"
