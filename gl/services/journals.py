"""Journal entries. Campfire holds the ledger; the app is where a manual entry is written, approved and looked up.

An entry written here waits for approval in the app (`JournalRequest`), by someone other than whoever wrote it when
the workspace asks for a second person. Approving posts it to Campfire, one entry per entity. A transfer between two
entities is two entries: the paying entity books a receivable from related entities, the receiving one a payable.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from django.utils import timezone

from campfire_integration.client import CampfireClient
from campfire_integration.models import Workspace
from gl.models import SOURCE, JournalRequest
from gl.services import lifestyle
from gl.services.posting import JE_PATH
from records.models import AuditEntry, ControlSettings

ZERO, CENT = Decimal(0), Decimal("0.01")
S = JournalRequest.Status
CASH_NAME, DUE_FROM_NAME, DUE_TO_NAME = "operating cash", "due from related", "due to related"  # how the transfer finds its accounts


class JournalError(Exception):
    pass


@dataclass
class Entry:
    id: int
    posted: str
    entity: str
    memo: str
    kind: str
    source: str
    number: str
    total: Decimal
    lines: list[dict[str, Any]]


def _money(raw: Any, what: str = "amount") -> Decimal:
    text = str(raw or "").replace(",", "").replace("$", "").strip()
    if not text:
        return ZERO
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise JournalError(f"Enter the {what} as a number.") from None
    if value < 0:
        raise JournalError(f"The {what} can't be negative.")
    return value.quantize(CENT)


def _date(raw: Any) -> date:
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        raise JournalError("Enter the date.") from None


def recent(client: CampfireClient, start: date, end: date, limit: int = 300) -> list[Entry]:
    """Entries in the ledger for the period, newest first."""
    params: dict[str, Any] | None = {"start_date": start.isoformat(), "end_date": end.isoformat(), "limit": 100}
    path, out = JE_PATH, []
    while path and len(out) < limit:
        page = client.get_json(path, params=params)
        for j in page.get("results", []):
            if j.get("is_deleted"):
                continue
            lines = [{"account": t.get("account_name") or "", "description": t.get("bank_description") or t.get("description") or "",
                      "debit": Decimal(str(t.get("debit_amount") or 0)).quantize(CENT), "credit": Decimal(str(t.get("credit_amount") or 0)).quantize(CENT)}
                     for t in (j.get("transactions") or []) if not t.get("is_deleted")]
            memo = j.get("memo") or next((ln["description"] for ln in lines if ln["description"]), "")  # bank lines carry no memo
            source = j.get("source") or ""
            out.append(Entry(int(j["id"]), str(j.get("date") or "")[:10], j.get("entity_name") or "", memo,
                             str(j.get("type") or "").replace("_", " "), "" if source == "manual" else source, str(j.get("order") or ""),
                             sum((ln["debit"] for ln in lines), ZERO), lines))
        nxt = page.get("next")
        path, params = (nxt.split(".com", 1)[-1], None) if nxt else ("", None)
    return sorted(out, key=lambda e: (e.posted, e.id), reverse=True)


def _create(workspace: Workspace, kind: str, when: date, memo: str, entries: list[dict[str, Any]], actor: str, actor_id: str) -> JournalRequest:
    total = sum((Decimal(ln["debit"]) for e in entries for ln in e["lines"]), ZERO)
    req = JournalRequest.objects.create(workspace=workspace, kind=kind, date=when, memo=memo[:255], entries=entries, total=total,
                                        created_by=actor, created_by_id=actor_id)
    AuditEntry.objects.create(workspace=workspace, actor=actor, action=AuditEntry.Action.CREATED, kind="journal-entries", object_id=str(req.pk),
                              object_repr=str(req)[:255], changes={"entered": ["", f"{memo} {total}"]})
    return req


def submit_manual(workspace: Workspace, data: Any, entities: dict[int, str], accounts: dict[int, str], actor: str, actor_id: str,
                  spending_accounts: frozenset[int] = frozenset()) -> JournalRequest:
    """`data`: entity, date, memo and numbered lines (account_N, debit_N, credit_N, description_N); blank lines are skipped.
    The spending tags chosen (family member, property) go on the lines whose account is in `spending_accounts` (the
    income and expense accounts), not on cash or balance-sheet lines."""
    entity = int(data.get("entity")) if str(data.get("entity") or "").isdigit() else 0
    memo = str(data.get("memo") or "").strip()
    if entity not in entities or not memo:
        raise JournalError("Choose the entity and say what the entry is for.")
    when, lines = _date(data.get("date")), []
    for n in range(1, 41):
        account, debit, credit = str(data.get(f"account_{n}") or ""), _money(data.get(f"debit_{n}"), "debit"), _money(data.get(f"credit_{n}"), "credit")
        if not account and not debit and not credit:
            continue
        if not account.isdigit() or int(account) not in accounts:
            raise JournalError(f"Line {n}: choose the account.")
        if bool(debit) == bool(credit):
            raise JournalError(f"Line {n}: enter a debit or a credit, not both.")
        lines.append({"account_id": int(account), "account": accounts[int(account)], "debit": str(debit), "credit": str(credit),
                      "description": str(data.get(f"description_{n}") or "").strip()[:255] or memo})
    if len(lines) < 2:
        raise JournalError("An entry needs at least two lines.")
    debits, credits = sum((Decimal(ln["debit"]) for ln in lines), ZERO), sum((Decimal(ln["credit"]) for ln in lines), ZERO)
    if debits != credits:
        raise JournalError(f"The entry doesn't balance: debits {debits:,.2f}, credits {credits:,.2f}.")
    tags = lifestyle.picked(workspace, data)
    if tags:
        for ln in lines:
            if ln["account_id"] in spending_accounts:
                ln["tags"] = tags
    req = _create(workspace, JournalRequest.Kind.MANUAL, when, memo, [{"entity_id": entity, "entity": entities[entity], "lines": lines}], actor, actor_id)
    if tags:
        req.tag_ids = tags
        req.save(update_fields=["tag_ids"])
    return req


def submit_transfer(workspace: Workspace, data: Any, entities: dict[int, str], accounts: dict[int, str], actor: str, actor_id: str) -> JournalRequest:
    as_int = lambda v: int(v) if str(v or "").isdigit() else 0  # noqa: E731
    payer, receiver, amount = as_int(data.get("from")), as_int(data.get("to")), _money(data.get("amount"))
    memo = str(data.get("memo") or "").strip()
    if payer not in entities or receiver not in entities or payer == receiver:
        raise JournalError("Choose the entity that pays and a different entity that receives.")
    if not amount or not memo:
        raise JournalError("Enter the amount and what the transfer is for.")
    find = lambda needle: next(((i, label) for i, label in accounts.items() if needle in label.lower()), None)  # noqa: E731
    cash, due_from, due_to = find(CASH_NAME), find(DUE_FROM_NAME), find(DUE_TO_NAME)
    if not (cash and due_from and due_to):
        raise JournalError("A transfer needs these accounts in the chart: operating cash, due from related entities, due to related entities.")
    line = lambda account, debit, credit, text: {"account_id": account[0], "account": account[1], "debit": str(debit), "credit": str(credit), "description": text}  # noqa: E731
    entries = [
        {"entity_id": payer, "entity": entities[payer], "lines": [line(due_from, amount, ZERO, f"Due from {entities[receiver]}: {memo}"),
                                                                  line(cash, ZERO, amount, f"Paid to {entities[receiver]}: {memo}")]},
        {"entity_id": receiver, "entity": entities[receiver], "lines": [line(cash, amount, ZERO, f"Received from {entities[payer]}: {memo}"),
                                                                        line(due_to, ZERO, amount, f"Due to {entities[payer]}: {memo}")]},
    ]
    return _create(workspace, JournalRequest.Kind.TRANSFER, _date(data.get("date")), memo, entries, actor, actor_id)


def payload(req: JournalRequest, n: int) -> dict[str, Any]:
    entry = req.entries[n]
    source_id = f"fo-journal-{req.pk}-{n + 1}"
    return {
        "date": req.date.isoformat(), "type": "journal_entry", "memo": req.memo, "entity": str(entry["entity_id"]), "currency": "USD",
        "source": SOURCE, "source_id": source_id, "ref_number": req.ref_number,
        "transactions": [{"account": str(ln["account_id"]),
                          **({"debit_amount_native": float(ln["debit"])} if Decimal(ln["debit"]) else {"credit_amount_native": float(ln["credit"])}),
                          "tags": list(ln.get("tags") or []), "bank_description": ln["description"], "external_id": f"{source_id}-{k}"}
                         for k, ln in enumerate(entry["lines"], start=1)],
    }


def _decide(req: JournalRequest, actor_id: str, what: str) -> None:
    if req.status != S.PENDING:
        raise JournalError("That entry has already been decided.")
    if ControlSettings.for_workspace(req.workspace).require_second_person and actor_id and actor_id == req.created_by_id:
        raise JournalError(f"You wrote this entry, so someone else has to {what} it (preparer ≠ approver).")


def approve(req: JournalRequest, client: CampfireClient, actor: str, actor_id: str) -> JournalRequest:
    _decide(req, actor_id, "approve")
    awaiting = False
    for n, entry in enumerate(req.entries):
        if entry.get("campfire_id") or entry.get("sent"):
            continue  # posted by an earlier attempt
        r = client.request("POST", JE_PATH, json=payload(req, n))
        if r.status_code == 202:
            entry["sent"], awaiting = True, True
        elif r.status_code in (200, 201):
            try:
                entry["campfire_id"] = r.json().get("id")
            except ValueError:
                entry["sent"] = True
            entry["sent"] = True
        else:
            req.error = f"{entry['entity']}: Campfire answered {r.status_code}: {r.text[:400]}"
            req.save()  # keeps what was already posted, so approving again sends only the rest
            raise JournalError(f"Campfire refused the entry for {entry['entity']} ({r.status_code}). It is still waiting here.")
    req.status = S.AWAITING if awaiting or any(e.get("sent") and not e.get("campfire_id") for e in req.entries) else S.SENT
    req.error, req.decided_by, req.decided_by_id, req.decided_at = "", actor, actor_id, timezone.now()
    req.save()
    AuditEntry.objects.create(workspace=req.workspace, actor=actor, action=AuditEntry.Action.UPDATED, kind="journal-entries", object_id=str(req.pk),
                              object_repr=str(req)[:255], changes={"status": ["Waiting for approval", req.get_status_display()]})
    return req


def reject(req: JournalRequest, actor: str, actor_id: str, note: str) -> JournalRequest:
    _decide(req, actor_id, "reject")
    if not note.strip():
        raise JournalError("Say why the entry is rejected.")
    if any(e.get("sent") for e in req.entries):
        raise JournalError("Part of this entry is already in Campfire; approve it again to send the rest.")
    req.status, req.note, req.decided_by, req.decided_by_id, req.decided_at = S.REJECTED, note.strip()[:255], actor, actor_id, timezone.now()
    req.save()
    AuditEntry.objects.create(workspace=req.workspace, actor=actor, action=AuditEntry.Action.UPDATED, kind="journal-entries", object_id=str(req.pk),
                              object_repr=str(req)[:255], changes={"status": ["Waiting for approval", f"Rejected: {req.note}"]})
    return req
