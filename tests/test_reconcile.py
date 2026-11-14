"""Bank reconciliation: a statement started, ticked, finished and approved, with Campfire keeping the report."""

import json
from datetime import date
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
import requests

from campfire_integration import access
from campfire_integration.models import Workspace
from gl.models import BankReconciliation
from gl.services import cash, statements
from gl.services import reconcile as rc
from records.models import AuditEntry, ControlSettings

TODAY = date(2026, 11, 13)
MGMT, ARE = "Alder Management LLC", "Alder Real Estate Holdings LLC"
ENTITIES = [{"id": 1, "name": "Alder Family (Consolidated)", "parent": None, "active": True},
            {"id": 2, "name": MGMT, "parent": 1, "active": True}, {"id": 3, "name": ARE, "parent": 1, "active": True}]
BS = {"headers": {"2026-11-01": [MGMT, ARE, "Total"]}, "balance_sheet": [
    {"account_number": "1040", "account_name": "Operating cash", "account_subtype": "BANK", "hierarchy": ["ASSET", "11"],
     f"2026-11-01_{MGMT}": 1150.0, f"2026-11-01_{ARE}": 649050.0},
    {"account_number": "1501", "account_name": "Investments at cost", "account_subtype": "OTHER_ASSET", "hierarchy": ["ASSET", "13"], f"2026-11-01_{ARE}": 8620000.0}]}
# The management company's bank account on the books: (id, date, money in (+) / out (-), description, from the bank feed)
GL = [(101, "2025-12-31", 1000.0, "Opening balance", False), (102, "2026-09-30", -100.0, "Operating account", False),
      (103, "2026-10-20", 500.0, "Operating account", False), (104, "2026-10-31", -200.0, "Operating account", False),
      (105, "2026-11-03", -50.0, None, True), (106, "2026-11-20", -1.0, "Dated after today's statement", False)]
BOOK = {"2026-10-31": 1200.0, "2026-11-13": 1150.0}


class FakeCampfire:
    """Campfire's reconciliation API, kept in memory: reports, and which report cleared each ledger line."""

    def __init__(self, status: int = 0) -> None:
        self.reports: dict[int, dict[str, Any]] = {}
        self.cleared: dict[int, int] = {}  # ledger line -> report
        self.sent: list[tuple[str, Any]] = []
        self.status = status  # answer every reconciliation call with this (a missing permission)

    def _resp(self, code: int, body: Any = None) -> requests.Response:
        r = requests.Response()
        r.status_code = code
        r._content = json.dumps(body if body is not None else {}).encode()
        return r

    def _view(self, rep: dict[str, Any]) -> dict[str, Any]:
        return {**rep, "entity_name": MGMT, "account_name": "Operating cash", "account_number": "1040"}

    def get_json(self, path: str, *, params: Any = None) -> Any:
        params = params or {}
        if path == rc.REPORT_PATH:
            if self.status:
                raise rc.CampfireError(mock.Mock(status_code=self.status, text="no", url=path, request=mock.Mock(method="GET")))
            return {"count": len(self.reports), "results": [self._view(r) for r in self.reports.values()], "next": None}
        if path == cash.LEDGER_PATH:
            if params.get("has_parent_bank_transaction"):
                return {"results": [{"id": i, "merchant_name": "MONTHLY SERVICE FEE"} for i, when, *_, feed in GL if feed and when <= params.get("end_date", "9999")], "next": None}
            return {"results": [{"id": GL[0][0], "posted_at": GL[0][1]}], "next": None}  # the account's first line
        if path == statements.TRIAL_BALANCE_PATH:
            return {"trial_balance": {"accounts": [{"id": "11", "ending_balances": {"debits": BOOK[params["end_date"]], "credits": 0}}]}}
        return {statements.ENTITY_PATH: ENTITIES, statements.BALANCE_SHEET_PATH: BS}[path]

    def request(self, method: str, path: str, *, json: Any = None, params: Any = None, timeout: float = 15) -> requests.Response:
        if self.status:
            return self._resp(self.status)
        tail = path[len(rc.REPORT_PATH):].strip("/").split("/")
        if method == "POST" and tail == [""]:
            self.sent.append(("create", json))
            for r in self.reports.values():
                if r["statement_starting_date"] <= json["statement_ending_date"] and json["statement_starting_date"] <= r["statement_ending_date"]:
                    return self._resp(400, {"error": "Reconciliation report already exists for this date range"})
            rid = 9000 + len(self.sent)
            self.reports[rid] = {"id": rid, "status": "DRAFT", **json}
            return self._resp(201, self._view(self.reports[rid]))
        rid = int(tail[0])
        if rid not in self.reports:
            return self._resp(404)
        rep = self.reports[rid]
        if tail[1:] == ["source-transactions"]:  # this report's cleared lines, and every line no report has cleared
            rows = [{"id": i, "posted_at": when, "debit_amount_in_currency": max(amount, 0) or None, "credit_amount_in_currency": max(-amount, 0) or None,
                     "bank_description": text, "is_cleared": self.cleared.get(i) == rid, "journal_order": f"00{i}"}
                    for i, when, amount, text, _ in GL if self.cleared.get(i) in (None, rid)]
            return self._resp(200, {"count": len(rows), "results": rows, "next": None})
        if tail[1:] == ["transactions", "confirm"]:
            self.sent.append(("confirm", json))
            self.cleared = {k: v for k, v in self.cleared.items() if v != rid}
            self.cleared.update({i: rid for i in json["selected"]})
            return self._resp(204)
        if method == "GET":
            return self._resp(200, self._view(rep))
        if method == "PATCH":
            self.sent.append(("patch", json))
            rep.update(json)
            return self._resp(200, self._view(rep))
        if method == "DELETE":
            self.sent.append(("delete", rid))
            del self.reports[rid]
            self.cleared = {k: v for k, v in self.cleared.items() if v != rid}
            return self._resp(204)
        raise AssertionError(f"{method} {path}")


@pytest.mark.django_db
def test_bank_accounts_and_where_each_stands(workspace: Workspace) -> None:
    cf = FakeCampfire()
    books = rc.books(cf, workspace, TODAY)  # type: ignore[arg-type]
    assert [(b.entity, b.account, b.balance, b.state, b.key) for b in books] == [
        (MGMT, "1040 Operating cash", Decimal("1150.00"), "never", "2-11"), (ARE, "1040 Operating cash", Decimal("649050.00"), "never", "3-11")]  # investments aren't bank accounts
    assert books[0].target == date(2026, 10, 31) and books[0].through is None and books[0].open is None


@pytest.mark.django_db
def test_a_statement_from_start_to_approved(workspace: Workspace) -> None:
    cf = FakeCampfire()
    go = {"book": "2-11", "end": "2026-10-31", "ending": "1,400"}
    for bad, why in (({"book": "9-9"}, "Choose the bank account"), ({"end": ""}, "Choose the bank account"), ({"end": "2026-12-31"}, "can't be in the future"),
                     ({"ending": "lots"}, "as a number"), ({"ending": ""}, "as a number")):
        with pytest.raises(rc.ReconcileError, match=why):
            rc.start(workspace, cf, {**go, **bad}, TODAY, "Pat")  # type: ignore[arg-type]
    assert not cf.sent

    # The first statement of an account starts where the books start, from zero: every line is accounted for.
    report = rc.start(workspace, cf, go, TODAY, "Pat")  # type: ignore[arg-type]
    assert cf.sent == [("create", {"entity": 2, "account": 11, "statement_starting_date": "2025-12-31", "statement_ending_date": "2026-10-31",
                                   "starting_balance": 0.0, "ending_balance": 1400.0})]
    assert (report.start, report.starting, report.ending, report.activity, report.status, report.editable) == (
        date(2025, 12, 31), Decimal("0.00"), Decimal("1400.00"), Decimal("1400.00"), rc.DRAFT, True)
    with pytest.raises(rc.ReconcileError, match="already has a statement in progress"):
        rc.start(workspace, cf, {**go, "end": "2026-11-13"}, TODAY, "Pat")  # type: ignore[arg-type]

    d = rc.detail(cf, workspace, report.id)  # type: ignore[arg-type]
    assert [ln.id for ln in d.lines] == [101, 102, 103, 104] and d.later == 2  # November's lines belong to a later statement
    assert (d.cleared_activity, d.difference, d.reconciles, d.book_balance) == (0, Decimal("1400.00"), False, Decimal("1200.00"))
    assert (d.in_transit, d.outstanding, d.adjusted, d.unexplained) == (Decimal("1500.00"), Decimal("300.00"), Decimal("2600.00"), Decimal("1400.00"))
    with pytest.raises(rc.ReconcileError, match="1,400.00 is unexplained"):
        rc.finish(workspace, cf, report.id, "Pat", "cfu:1", TODAY)  # type: ignore[arg-type]
    with pytest.raises(rc.ReconcileError, match="aren't on this statement"):
        rc.save(workspace, cf, report.id, [101, 105], "Pat")  # type: ignore[arg-type]  # a line after the statement's last day

    # The October payment hasn't reached the bank: it stays outstanding, and the statement still reconciles.
    d = rc.save(workspace, cf, report.id, [101, 102, 103], "Pat")  # type: ignore[arg-type]
    assert cf.sent[-1] == ("confirm", {"selected": [101, 102, 103]})
    assert (d.cleared_activity, d.difference, d.reconciles) == (Decimal("1400.00"), 0, True)
    assert (d.in_transit, d.outstanding, d.adjusted, d.unexplained) == (0, Decimal("200.00"), Decimal("1200.00"), 0)  # the classic proof agrees
    rc.set_ending(workspace, cf, report.id, "1,450", "Pat")  # type: ignore[arg-type]
    assert rc.detail(cf, workspace, report.id).difference == Decimal("50.00")  # type: ignore[arg-type]
    rc.set_ending(workspace, cf, report.id, "1400", "Pat")  # type: ignore[arg-type]

    done = rc.finish(workspace, cf, report.id, "Pat", "cfu:1", TODAY)  # type: ignore[arg-type]
    assert cf.sent[-1] == ("patch", {"status": "PENDING_APPROVAL", "reconciled_on": "2026-11-13"}) and done.status == rc.PENDING
    rec = BankReconciliation.objects.get()
    assert (rec.status, rec.prepared_by, rec.prepared_by_id, rec.created_by, rec.campfire_report_id) == ("pending", "Pat", "cfu:1", "Pat", report.id)
    for change in (lambda: rc.save(workspace, cf, report.id, [101], "Pat"), lambda: rc.set_ending(workspace, cf, report.id, "1", "Pat"),  # type: ignore[arg-type]
                   lambda: rc.discard(workspace, cf, report.id, "Pat"), lambda: rc.finish(workspace, cf, report.id, "Pat", "cfu:1", TODAY)):  # type: ignore[arg-type]
        with pytest.raises(rc.ReconcileError, match="finished|Only a statement in progress"):
            change()  # Campfire would allow these: the app doesn't
    assert [b.state for b in rc.books(cf, workspace, TODAY)][0] == "pending"  # type: ignore[arg-type]

    # Approval: someone other than the preparer, and only while it still reconciles.
    with pytest.raises(rc.ReconcileError, match="preparer ≠ approver"):
        rc.approve(workspace, cf, report.id, "Pat", "cfu:1")  # type: ignore[arg-type]
    cf.cleared.pop(103)  # changed behind the app's back, in Campfire
    with pytest.raises(rc.ReconcileError, match="no longer reconciles"):
        rc.approve(workspace, cf, report.id, "Sam", "cfu:2")  # type: ignore[arg-type]
    cf.cleared[103] = report.id
    ok = rc.approve(workspace, cf, report.id, "Sam", "cfu:2")  # type: ignore[arg-type]
    rec.refresh_from_db()
    assert ok.status == rc.APPROVED and cf.sent[-1] == ("patch", {"status": "APPROVED"}) and (rec.status, rec.approved_by, rec.approved_by_id) == ("approved", "Sam", "cfu:2")
    book = rc.books(cf, workspace, TODAY)[-1]  # type: ignore[arg-type]
    assert (book.entity, book.state, book.through, book.last.ending) == (MGMT, "current", date(2026, 10, 31), Decimal("1400.00"))  # type: ignore[union-attr]

    # The next statement continues from it. The payment still outstanding is offered again; bank-feed lines are ticked for the person.
    with pytest.raises(rc.ReconcileError, match="already reconciled to Oct 31, 2026"):
        rc.start(workspace, cf, go, TODAY, "Pat")  # type: ignore[arg-type]
    nxt = rc.start(workspace, cf, {"book": "2-11", "end": "2026-11-13", "ending": "1150", "start": "2020-01-01", "starting": "5"}, TODAY, "Pat")  # type: ignore[arg-type]
    assert (nxt.start, nxt.starting, nxt.activity) == (date(2026, 11, 1), Decimal("1400.00"), Decimal("-250.00"))  # a later statement ignores a typed start
    d = rc.detail(cf, workspace, nxt.id)  # type: ignore[arg-type]
    assert [(ln.id, ln.from_feed, ln.description) for ln in d.lines] == [(104, False, "Operating account"), (105, True, "MONTHLY SERVICE FEE")]
    assert d.later == 1 and d.suggested == {105}
    assert rc.save(workspace, cf, nxt.id, [104, 105], "Pat").reconciles  # type: ignore[arg-type]

    # Only the latest statement can be reopened; a draft can be discarded, and its lines are open again.
    with pytest.raises(rc.ReconcileError, match="A later statement follows"):
        rc.reopen(workspace, cf, report.id, "Sam")  # type: ignore[arg-type]
    rc.discard(workspace, cf, nxt.id, "Pat")  # type: ignore[arg-type]
    assert nxt.id not in cf.reports and 104 not in cf.cleared and BankReconciliation.objects.count() == 1
    again = rc.reopen(workspace, cf, report.id, "Sam")  # type: ignore[arg-type]
    rec.refresh_from_db()
    assert again.editable and (rec.status, rec.prepared_by, rec.approved_by) == ("draft", "", "")
    kinds = [(e.action, e.actor) for e in AuditEntry.objects.filter(kind="bank-reconciliations").order_by("id")]
    assert kinds[0] == (AuditEntry.Action.CREATED, "Pat") and (AuditEntry.Action.DELETED, "Pat") in kinds and len(kinds) == 10

    # Without the second-person rule, the preparer may approve.
    rc.finish(workspace, cf, report.id, "Pat", "cfu:1", TODAY)  # type: ignore[arg-type]
    ControlSettings.objects.update_or_create(workspace=workspace, defaults={"require_second_person": False})
    assert rc.approve(workspace, cf, report.id, "Pat", "cfu:1").status == rc.APPROVED  # type: ignore[arg-type]


@pytest.mark.django_db
def test_a_first_statement_can_start_part_way_and_a_missing_permission_is_said(workspace: Workspace) -> None:
    cf = FakeCampfire()
    r = rc.start(workspace, cf, {"book": "2-11", "end": "2026-10-31", "ending": "1200", "start": "2026-10-01", "starting": "900"}, TODAY, "Pat")  # type: ignore[arg-type]
    assert (r.start, r.starting, r.activity) == (date(2026, 10, 1), Decimal("900.00"), Decimal("300.00"))
    d = rc.save(workspace, cf, r.id, [103, 104], "Pat")  # type: ignore[arg-type]
    assert d.reconciles and d.unexplained == Decimal("900.00")  # it reconciles, but the lines before October are still open: the page says so
    with pytest.raises(rc.ReconcileError, match="first day must be on or before"):
        rc.start(workspace, FakeCampfire(), {"book": "2-11", "end": "2026-10-31", "ending": "1", "start": "2026-11-01"}, TODAY, "Pat")  # type: ignore[arg-type]
    for code in (401, 403):
        with pytest.raises(rc.ReconcileError, match="Promote the app's new version"):
            rc.reports(FakeCampfire(code), workspace)  # type: ignore[arg-type]
        with pytest.raises(rc.ReconcileError, match="reconnect"):
            rc.detail(FakeCampfire(code), workspace, 1)  # type: ignore[arg-type]
    assert access.required("reconcile", "POST", "approve") == access.APPROVE and access.required("reconcile", "POST", "reopen") == access.APPROVE
    assert access.required("reconcile", "POST", "save") == access.PREPARE and access.required("reconcile", "GET", "") == access.VIEW


@pytest.mark.django_db
def test_reconcile_page(client, workspace: Workspace, settings) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import unquote

    from campfire_integration.client import connect_scopes
    from config.settings import DEFAULT_CAMPFIRE_SCOPES
    from tests.conftest import context_token

    settings.CAMPFIRE = {**settings.CAMPFIRE, "SCOPES": DEFAULT_CAMPFIRE_SCOPES}
    assert "reconciliations:write" in connect_scopes()  # asked for at the write tier, also when entries and bills go as drafts
    cf = FakeCampfire()
    with mock.patch("gl.reconcile_views.CampfireClient", return_value=cf), mock.patch("gl.reconcile_views.timezone.localdate", return_value=TODAY):
        page = client.get(f"/fo/frame/reconcile?cf_ctx={context_token(customer_id=workspace.customer_id, surface='nav', user_id=3)}").content.decode()
        assert "Bank reconciliation" in page and "0 of 2" in page and "Not reconciled" in page and "reconciled through Oct 31, 2026" in page
        assert 'value="2026-10-31"' in page and f"{MGMT} · 1040 Operating cash" in page
        token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
        post = lambda data, q="": client.post(f"/fo/frame/reconcile{q}", {"fo_t": token, **data}).content.decode()  # noqa: E731
        wrong = post({"action": "start", "book": "2-11", "end": "2026-10-31", "ending": ""})
        assert "Enter the statement&#x27;s ending balance as a number" in wrong and "<details class=\"settings\" id=\"start\" open>" in wrong
        started = post({"action": "start", "book": "2-11", "end": "2026-10-31", "ending": "1400"})
        rid = next(iter(cf.reports))
        assert "Statement to Oct 31, 2026 started" in started and "Dec 31, 2025 to Oct 31, 2026" in started and 'data-recon="1400.00"' in started
        assert started.count('name="line"') == 4 and 'data-amount="-200.00"' in started and "2 later lines" in started
        early = post({"action": "finish", "report": rid, "with_lines": "1", "line": ["101"]}, f"?report={rid}")
        assert "400.00 is unexplained" in early and cf.cleared == {101: rid}  # the ticks were saved, the statement stays open
        saved = post({"action": "save", "report": rid, "line": ["101", "102", "103"]}, f"?report={rid}")
        assert "Saved: it reconciles" in saved and "is-zero" in saved
        finished = post({"action": "finish", "report": rid, "with_lines": "1", "line": ["101", "102", "103"]}, f"?report={rid}")
        assert "finished. It now waits for approval" in finished and "To approve" in finished and "You prepared this reconciliation" in finished
        assert "preparer ≠ approver" in post({"action": "approve", "report": rid})
        BankReconciliation.objects.update(prepared_by="Sam", prepared_by_id="cfu:99")
        approved = post({"action": "approve", "report": rid})
        assert "approved." in approved and "1 of 2" in approved and "Up to date" in approved and "Prepared by" not in approved.split("Statements")[0]
        detail = client.get("/fo/frame/reconcile", {"fo_t": token, "report": rid}).content.decode()
        assert "Approved by" in detail and "disabled" in detail and "Finish and send for approval" not in detail and "Reopen to correct it" in detail
        assert "Only a statement in progress can be discarded" in post({"action": "discard", "report": rid}, f"?report={rid}") and rid in cf.reports  # an approved statement can't be discarded
        assert "reopened" in post({"action": "reopen", "report": rid}, f"?report={rid}")
        assert "discarded. Its lines are open again" in post({"action": "discard", "report": rid}, f"?report={rid}") and not cf.reports and not cf.cleared
    no = FakeCampfire(403)
    with mock.patch("gl.reconcile_views.CampfireClient", return_value=no), mock.patch("gl.reconcile_views.timezone.localdate", return_value=TODAY):
        page = client.get("/fo/frame/reconcile", {"fo_t": token}).content.decode()
        assert "Promote the app&#x27;s new version in Campfire" in page
