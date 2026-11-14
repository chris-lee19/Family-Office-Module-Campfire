"""Bank reconciliation: prove that each bank account's balance on the books agrees with the bank's statement.

Campfire keeps the reconciliation report: the statement's dates and balances, and which ledger lines on the bank
account's GL account cleared. The app drives it: start a statement, tick what cleared, finish when the difference is
zero, and have a second person approve. Campfire itself lets a finished report be changed, so the locks (only a
draft can be edited; only the latest statement can be reopened) and the record of who prepared and who approved
live here (`BankReconciliation`).

A statement reconciles when what cleared equals the statement's activity (ending balance less starting balance).
The first statement of an account starts where the books start, from a balance of zero, so that nothing is left
unexplained before it. The classic proof is shown beside it: statement balance, plus deposits in transit, less
payments outstanding, against the book balance on the statement date.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.utils import timezone

from campfire_integration.client import CampfireClient, CampfireError
from campfire_integration.models import Workspace
from gl.models import BankReconciliation
from gl.services import cash as cash_svc
from gl.services import statements
from records.models import AuditEntry, ControlSettings

REPORT_PATH = "/coa/api/v2/reconciliation"
ZERO = Decimal(0)
LINE_CAP = 3000
DRAFT, PENDING, APPROVED = "DRAFT", "PENDING_APPROVAL", "APPROVED"
STATUS_LABELS = {DRAFT: "In progress", PENDING: "Waiting for approval", APPROVED: "Approved"}
LOCAL = {DRAFT: BankReconciliation.Status.DRAFT, PENDING: BankReconciliation.Status.PENDING, APPROVED: BankReconciliation.Status.APPROVED}
NEEDS_PERMISSION = ("The connection doesn't allow bank reconciliation yet. Promote the app's new version in Campfire, then reconnect the "
                    "workspace so the app gets the permission.")


class ReconcileError(Exception):
    pass


def _dec(v: Any) -> Decimal:
    return Decimal(str(v or 0)).quantize(Decimal("0.01"))


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v or "")[:10])
    except ValueError:
        return None


@dataclass
class Report:
    id: int
    entity_id: int
    entity: str
    account_id: int
    account: str
    start: date
    end: date
    starting: Decimal
    ending: Decimal
    status: str
    record: BankReconciliation | None = None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status.title())

    @property
    def editable(self) -> bool:
        return self.status == DRAFT

    @property
    def activity(self) -> Decimal:
        """What the statement says moved: ending balance less starting balance."""
        return self.ending - self.starting

    @property
    def key(self) -> str:
        return f"{self.entity_id}-{self.account_id}"


def _report(d: dict[str, Any]) -> Report | None:
    start, end = _date(d.get("statement_starting_date")), _date(d.get("statement_ending_date"))
    if start is None or end is None or d.get("status") == "DELETED" or not d.get("entity") or not d.get("account"):
        return None
    label = " ".join(x for x in (str(d.get("account_number") or ""), str(d.get("account_name") or "")) if x)
    return Report(int(d["id"]), int(d["entity"]), d.get("entity_name") or "", int(d["account"]), label, start, end, _dec(d.get("starting_balance")),
                  _dec(d.get("ending_balance")), d.get("status") or DRAFT)


def _check(r: Any, what: str) -> Any:
    if r.status_code in (401, 403):
        raise ReconcileError(NEEDS_PERMISSION)
    if r.status_code >= 400:
        try:
            reason = r.json().get("error") or r.text
        except (ValueError, AttributeError):
            reason = r.text
        raise ReconcileError(f"Campfire refused to {what} ({r.status_code}): {str(reason)[:300]}")
    return r


def reports(client: CampfireClient, workspace: Workspace) -> list[Report]:
    """Every reconciliation report in Campfire, newest statement first, with the app's record of who did what."""
    try:
        raw = cash_svc._all(client, REPORT_PATH)
    except CampfireError as e:
        if e.status_code in (401, 403):
            raise ReconcileError(NEEDS_PERMISSION) from None
        raise
    out = [r for r in (_report(d) for d in raw) if r is not None]
    records = {b.campfire_report_id: b for b in BankReconciliation.objects.filter(workspace=workspace)}
    for r in out:
        r.record = records.get(r.id)
    return sorted(out, key=lambda r: (r.end, r.id), reverse=True)


@dataclass
class Book:
    """One bank account on the books: an entity's balance on a GL account of the bank kind."""

    entity_id: int
    entity: str
    account_id: int
    account: str
    balance: Decimal = ZERO
    reports: list[Report] = field(default_factory=list)  # newest first
    target: date | None = None

    @property
    def key(self) -> str:
        return f"{self.entity_id}-{self.account_id}"

    @property
    def open(self) -> Report | None:
        return next((r for r in self.reports if r.status != APPROVED), None)

    @property
    def last(self) -> Report | None:
        return next((r for r in self.reports if r.status == APPROVED), None)

    @property
    def through(self) -> date | None:
        return self.last.end if self.last else None

    @property
    def state(self) -> str:
        if self.open is not None:
            return "pending" if self.open.status == PENDING else "progress"
        if self.through is None:
            return "never"
        return "current" if self.target is None or self.through >= self.target else "behind"


def books(client: CampfireClient, workspace: Workspace, today: date) -> list[Book]:
    """The bank accounts on the books (one balance sheet per consolidated group, by entity), with their statements."""
    names = {s.label: s.entity_id for s in statements.scopes(client) if not s.rollup}
    target = today.replace(day=1) - timedelta(days=1)  # the last month end: where every account should be reconciled to
    out: dict[str, Book] = {}
    for scope in [s for s in statements.scopes(client) if s.rollup]:
        data = client.get_json(statements.BALANCE_SHEET_PATH, params={**scope.params(), "start_date": today.replace(day=1).isoformat(),
                                                                         "end_date": today.isoformat(), "group_by": "entity"})
        key = next(iter(sorted(data.get("headers", {}), reverse=True)), None)
        for row in data.get("balance_sheet", []) if key else []:
            account = str((row.get("hierarchy") or [""])[-1])
            if row.get("account_subtype") != "BANK" or not account.isdigit():
                continue
            label = " ".join(x for x in (str(row.get("account_number") or ""), str(row.get("account_name") or "")) if x)
            for entity in data["headers"][key]:
                amount = _dec(row.get(f"{key}_{entity}"))
                if entity == "Total" or entity not in names or not amount:
                    continue
                book = Book(names[entity], entity, int(account), label, amount, target=target)
                out[book.key] = book
    for r in reports(client, workspace):
        book = out.setdefault(r.key, Book(r.entity_id, r.entity, r.account_id, r.account, target=target))  # reconciled, balance now zero
        book.reports.append(r)
    order = {"pending": 0, "progress": 1, "never": 2, "behind": 2, "current": 3}
    return sorted(out.values(), key=lambda b: (order[b.state], b.entity, b.account))


@dataclass
class Line:
    id: int
    posted: date
    description: str
    amount: Decimal  # money in > 0, money out < 0
    cleared: bool
    entry: str = ""
    from_feed: bool = False


@dataclass
class Detail:
    report: Report
    lines: list[Line]  # dated on or before the statement's last day
    later: int = 0  # not cleared and dated after it: they belong to a later statement
    book_balance: Decimal | None = None
    truncated: bool = False

    @property
    def cleared(self) -> list[Line]:
        return [ln for ln in self.lines if ln.cleared]

    @property
    def open(self) -> list[Line]:
        return [ln for ln in self.lines if not ln.cleared]

    @property
    def cleared_activity(self) -> Decimal:
        return sum((ln.amount for ln in self.cleared), ZERO)

    @property
    def difference(self) -> Decimal:
        """What the statement shows moved that the cleared lines don't explain. Zero: it reconciles."""
        return self.report.activity - self.cleared_activity

    @property
    def reconciles(self) -> bool:
        return not self.difference

    @property
    def in_transit(self) -> Decimal:
        return sum((ln.amount for ln in self.open if ln.amount > 0), ZERO)

    @property
    def outstanding(self) -> Decimal:
        return -sum((ln.amount for ln in self.open if ln.amount < 0), ZERO)

    @property
    def adjusted(self) -> Decimal:
        """The bank's balance once what is in transit arrives and what is outstanding is paid."""
        return self.report.ending + self.in_transit - self.outstanding

    @property
    def unexplained(self) -> Decimal | None:
        return None if self.book_balance is None else self.adjusted - self.book_balance

    @property
    def suggested(self) -> set[int]:
        """Lines to tick for the person on a statement nothing has been ticked on: what came through the bank feed."""
        return set() if self.cleared else {ln.id for ln in self.lines if ln.from_feed}


def _get(client: CampfireClient, report_id: int, workspace: Workspace) -> Report:
    r = _check(client.request("GET", f"{REPORT_PATH}/{report_id}"), "read the reconciliation")
    report = _report(r.json())
    if report is None:
        raise ReconcileError("That reconciliation isn't in Campfire any more.")
    report.record = BankReconciliation.objects.filter(workspace=workspace, campfire_report_id=report.id).first()
    return report


def _book_balance(client: CampfireClient, report: Report) -> Decimal | None:
    data = client.get_json(statements.TRIAL_BALANCE_PATH, params={"entity": report.entity_id, "start_date": report.end.replace(day=1).isoformat(),
                                                                    "end_date": report.end.isoformat()})
    for a in data.get("trial_balance", {}).get("accounts", []):
        if str(a.get("id")) == str(report.account_id):
            end = a.get("ending_balances") or {}
            return _dec(end.get("debits")) - _dec(end.get("credits"))
    return ZERO


def detail(client: CampfireClient, workspace: Workspace, report_id: int) -> Detail:
    report = _get(client, report_id, workspace)
    raw: list[dict[str, Any]] = []
    path, query, total = f"{REPORT_PATH}/{report.id}/source-transactions", {"limit": 200}, 0
    while path and len(raw) < LINE_CAP:  # the lines this statement cleared, and every line no statement has cleared yet
        page = _check(client.request("GET", path, params=query), "list the ledger lines").json()
        total = int(page.get("count") or 0)
        raw += page.get("results", [])
        nxt = page.get("next")
        path, query = (nxt.split(".com", 1)[-1], None) if nxt else ("", None)  # type: ignore[assignment]
    # Lines the bank feed posted: the bank has them by definition, and they carry the bank's own description.
    feed = {int(t["id"]): str(t.get("merchant_name") or "") for t in cash_svc._all(client, cash_svc.LEDGER_PATH, {
        "entity": report.entity_id, "account": report.account_id, "has_parent_bank_transaction": "true", "end_date": report.end.isoformat()})}
    lines, later = [], 0
    for t in raw:
        posted = _date(t.get("posted_at"))
        if posted is None:
            continue
        cleared = str(t.get("is_cleared")).lower() == "true"
        if posted > report.end and not cleared:
            later += 1
            continue
        lines.append(Line(int(t["id"]), posted, t.get("bank_description") or feed.get(int(t["id"]), ""), _dec(t.get("debit_amount_in_currency")) - _dec(t.get("credit_amount_in_currency")),
                          cleared, str(t.get("journal_order") or ""), int(t["id"]) in feed))
    lines.sort(key=lambda ln: (ln.posted, ln.id))
    return Detail(report, lines, later, _book_balance(client, report), total > len(raw))


# ---- actions ----

def _money(raw: Any, what: str) -> Decimal:
    try:
        return Decimal(str(raw if raw not in (None, "") else "x").replace(",", "").replace("$", "").strip()).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ReconcileError(f"Enter the {what} as a number.") from None


def _audit(workspace: Workspace, actor: str, report: Report, action: str, changes: dict[str, Any]) -> None:
    AuditEntry.objects.create(workspace=workspace, actor=actor, action=action, kind="bank-reconciliations", object_id=str(report.id),
                              object_repr=f"{report.entity} · {report.account}: statement to {report.end}"[:255], changes=changes)


def _record(workspace: Workspace, report: Report, actor: str = "") -> BankReconciliation:
    rec, _ = BankReconciliation.objects.get_or_create(workspace=workspace, campfire_report_id=report.id, defaults={
        "campfire_entity_id": report.entity_id, "entity_label": report.entity[:255], "campfire_account_id": report.account_id,
        "account_label": report.account[:255], "statement_start": report.start, "statement_end": report.end, "ending_balance": report.ending,
        "status": LOCAL.get(report.status, BankReconciliation.Status.DRAFT), "created_by": actor})
    return rec


def start(workspace: Workspace, client: CampfireClient, data: Any, today: date, actor: str) -> Report:
    """Start a statement for a bank account. It follows the account's last statement; the first one starts where the
    books start, from zero, unless a starting date and balance are given."""
    book = next((b for b in books(client, workspace, today) if b.key == str(data.get("book") or "")), None)
    end = _date(data.get("end"))
    if book is None or end is None:
        raise ReconcileError("Choose the bank account and the statement's last day.")
    if end > today:
        raise ReconcileError("The statement's last day can't be in the future.")
    if book.open is not None:
        raise ReconcileError(f"{book.entity} already has a statement in progress (to {book.open.end:%b %-d, %Y}). Finish or discard it first.")
    ending = _money(data.get("ending"), "statement's ending balance")
    previous = book.reports[0] if book.reports else None
    if previous is not None:
        if end <= previous.end:
            raise ReconcileError(f"This account is already reconciled to {previous.end:%b %-d, %Y}. Choose a later statement date.")
        begin, starting = previous.end + timedelta(days=1), previous.ending
    elif _date(data.get("start")) is not None:
        begin, starting = _date(data.get("start")), _money(data.get("starting") or "0", "statement's starting balance")
        if begin > end:  # type: ignore[operator]
            raise ReconcileError("The statement's first day must be on or before its last day.")
    else:  # from where the books start
        first = client.get_json(cash_svc.LEDGER_PATH, params={"entity": book.entity_id, "account": book.account_id, "sort": "posted_at", "limit": 1})
        seen = _date((first.get("results") or [{}])[0].get("posted_at"))
        begin, starting = min(seen or end.replace(day=1), end), ZERO
    r = _check(client.request("POST", REPORT_PATH, json={
        "entity": book.entity_id, "account": book.account_id, "statement_starting_date": begin.isoformat(),  # type: ignore[union-attr]
        "statement_ending_date": end.isoformat(), "starting_balance": float(starting), "ending_balance": float(ending)}), "start the reconciliation")
    report = _report(r.json())
    if report is None:
        raise ReconcileError("Campfire answered without a reconciliation. Nothing was started.")
    report.record = _record(workspace, report, actor)
    _audit(workspace, actor, report, AuditEntry.Action.CREATED, {"statement": ["", f"{begin} to {end}, ending balance {ending}"]})
    return report


def save(workspace: Workspace, client: CampfireClient, report_id: int, selected: list[int], actor: str) -> Detail:
    """Say which ledger lines cleared the bank in this statement (the whole selection, not a change to it)."""
    current = detail(client, workspace, report_id)
    if not current.report.editable:
        raise ReconcileError("This statement is finished. Reopen it to change what cleared.")
    offered = {ln.id for ln in current.lines}
    if not set(selected) <= offered:
        raise ReconcileError("Some of those lines aren't on this statement any more. Open it again and retry.")
    before = len(current.cleared)
    _check(client.request("POST", f"{REPORT_PATH}/{report_id}/transactions/confirm", json={"selected": sorted(set(selected))}), "save what cleared")
    after = detail(client, workspace, report_id)
    _record(workspace, after.report, actor)
    _audit(workspace, actor, after.report, AuditEntry.Action.UPDATED, {"cleared lines": [before, len(after.cleared)], "difference": [str(current.difference), str(after.difference)]})
    return after


def set_ending(workspace: Workspace, client: CampfireClient, report_id: int, raw: Any, actor: str) -> Report:
    report = _get(client, report_id, workspace)
    if not report.editable:
        raise ReconcileError("This statement is finished. Reopen it to change its balance.")
    ending = _money(raw, "statement's ending balance")
    _check(client.request("PATCH", f"{REPORT_PATH}/{report_id}", json={"ending_balance": float(ending)}), "change the ending balance")
    BankReconciliation.objects.filter(workspace=workspace, campfire_report_id=report_id).update(ending_balance=ending)
    _audit(workspace, actor, report, AuditEntry.Action.UPDATED, {"ending balance": [str(report.ending), str(ending)]})
    return report


def _set_status(client: CampfireClient, report: Report, status: str, extra: dict[str, Any] | None = None) -> None:
    _check(client.request("PATCH", f"{REPORT_PATH}/{report.id}", json={"status": status, **(extra or {})}), "update the reconciliation")
    report.status = status


def finish(workspace: Workspace, client: CampfireClient, report_id: int, actor: str, actor_id: str, today: date) -> Report:
    """The preparer's sign-off: only when the statement reconciles. It then waits for approval."""
    current = detail(client, workspace, report_id)
    report = current.report
    if not report.editable:
        raise ReconcileError("This statement is already finished.")
    if not current.reconciles:
        raise ReconcileError(f"It doesn't reconcile yet: {abs(current.difference):,.2f} is unexplained. Tick what cleared, or correct the ending balance.")
    _set_status(client, report, PENDING, {"reconciled_on": today.isoformat()})
    rec = _record(workspace, report, actor)
    rec.status, rec.ending_balance = BankReconciliation.Status.PENDING, report.ending
    rec.prepared_by, rec.prepared_by_id, rec.prepared_at = actor, actor_id, timezone.now()
    rec.save()
    report.record = rec
    _audit(workspace, actor, report, AuditEntry.Action.UPDATED, {"status": ["In progress", "Waiting for approval"]})
    return report


def approve(workspace: Workspace, client: CampfireClient, report_id: int, actor: str, actor_id: str) -> Report:
    current = detail(client, workspace, report_id)
    report = current.report
    if report.status != PENDING:
        raise ReconcileError("Only a statement that is waiting for approval can be approved.")
    rec = _record(workspace, report)
    if ControlSettings.for_workspace(workspace).require_second_person and actor_id and actor_id == rec.prepared_by_id:
        raise ReconcileError("You prepared this reconciliation. Someone else approves it (preparer ≠ approver).")
    if not current.reconciles:  # Campfire doesn't lock a finished report: check again before signing
        raise ReconcileError(f"It no longer reconciles ({abs(current.difference):,.2f} unexplained). Reopen it and correct it first.")
    _set_status(client, report, APPROVED)
    rec.status, rec.approved_by, rec.approved_by_id, rec.approved_at = BankReconciliation.Status.APPROVED, actor, actor_id, timezone.now()
    rec.save()
    report.record = rec
    _audit(workspace, actor, report, AuditEntry.Action.UPDATED, {"status": ["Waiting for approval", "Approved"]})
    return report


def _latest(client: CampfireClient, workspace: Workspace, report: Report) -> bool:
    return not any(r.key == report.key and r.end > report.end for r in reports(client, workspace))


def reopen(workspace: Workspace, client: CampfireClient, report_id: int, actor: str) -> Report:
    report = _get(client, report_id, workspace)
    if report.editable:
        raise ReconcileError("This statement is already open.")
    if not _latest(client, workspace, report):
        raise ReconcileError("A later statement follows this one. Reopen or discard the later one first.")
    was = report.status_label
    _set_status(client, report, DRAFT)
    rec = _record(workspace, report)
    rec.status = BankReconciliation.Status.DRAFT
    rec.prepared_by = rec.prepared_by_id = rec.approved_by = rec.approved_by_id = ""
    rec.prepared_at = rec.approved_at = None
    rec.save()
    report.record = rec
    _audit(workspace, actor, report, AuditEntry.Action.UPDATED, {"status": [was, "In progress"]})
    return report


def discard(workspace: Workspace, client: CampfireClient, report_id: int, actor: str) -> Report:
    """Delete a statement that is still in progress. Its lines go back to not cleared."""
    report = _get(client, report_id, workspace)
    if not report.editable:
        raise ReconcileError("Only a statement in progress can be discarded. Reopen it first.")
    _check(client.request("DELETE", f"{REPORT_PATH}/{report_id}"), "discard the reconciliation")
    BankReconciliation.objects.filter(workspace=workspace, campfire_report_id=report_id).delete()
    _audit(workspace, actor, report, AuditEntry.Action.DELETED, {"statement": [f"{report.start} to {report.end}", ""]})
    return report
