"""Journal entries: written in the app, approved by a second person, posted to Campfire's ledger."""

from datetime import date
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest
import requests

from campfire_integration.models import Workspace
from gl.models import JournalRequest
from gl.services import journals, statements
from gl.services.posting import JE_PATH

S = JournalRequest.Status
ENTITIES = {2: "Alder Management LLC", 3: "Alder Real Estate Holdings LLC"}
ACCOUNTS = {11: "1040 Operating cash - family entities", 18: "1180 Due from related entities", 28: "2180 Due to related entities",
            65: "6520 Professional and advisory fees", 26: "2160 Accrued expenses - family entities"}
MANUAL = {"entity": "2", "date": "2026-10-31", "memo": "Accrue October adviser fee", "account_1": "65", "debit_1": "9,500", "description_1": "Granite Peak, October",
          "account_2": "26", "credit_2": "9500"}


class FakeCampfire:
    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses, self.sent = list(statuses or []), []  # type: ignore[var-annotated]

    def get_json(self, path: str, *, params: Any = None) -> Any:
        if path == statements.ENTITY_PATH:
            return [{"id": 1, "name": "Alder Family (Consolidated)", "parent": None, "active": True}] + [{"id": i, "name": n, "parent": 1, "active": True} for i, n in ENTITIES.items()]
        if path == "/coa/api/account":
            return {"results": [{"id": i, "number": label.split(" ", 1)[0], "name": label.split(" ", 1)[1], "account_type": "ASSET"} for i, label in ACCOUNTS.items()], "next": None}
        assert path == JE_PATH
        return {"results": [{"id": 77, "date": "2026-10-31", "entity_name": "Alder Management LLC", "memo": "Operating activity, October 2026", "type": "journal_entry",
                             "source": "Alder demo books", "order": "0000454", "transactions": [
                                 {"account_name": "6520 - Professional and advisory fees", "bank_description": "Staff", "debit_amount": 8500.0},
                                 {"account_name": "1040 - Operating cash", "credit_amount": 8500.0}]},
                            {"id": 78, "is_deleted": True, "date": "2026-10-30"}], "next": None}

    def request(self, method: str, path: str, *, json: Any = None, params: Any = None, timeout: float = 15) -> requests.Response:
        self.sent.append(json)
        r = requests.Response()
        r.status_code = self.statuses.pop(0) if self.statuses else 201
        r._content = b'{"id": 9001}'
        return r


def test_recent_entries_from_the_ledger() -> None:
    (entry,) = journals.recent(FakeCampfire(), date(2026, 8, 1), date(2026, 11, 13))  # type: ignore[arg-type]
    assert (entry.id, entry.kind, entry.total, entry.number, len(entry.lines)) == (77, "journal entry", Decimal("8500.00"), "0000454", 2)


@pytest.mark.django_db
def test_manual_entry_must_balance_and_is_approved_by_someone_else(workspace: Workspace) -> None:
    for bad, message in (({"credit_2": "9000"}, "doesn't balance"), ({"account_2": ""}, "Line 2: choose"), ({"debit_2": "1"}, "not both"),
                         ({"account_2": "", "credit_2": ""}, "at least two lines"), ({"memo": " "}, "what the entry is for"), ({"date": "soon"}, "date")):
        with pytest.raises(journals.JournalError, match=message):
            journals.submit_manual(workspace, {**MANUAL, **bad}, ENTITIES, ACCOUNTS, "Pat", "cfu:1")
    req = journals.submit_manual(workspace, MANUAL, ENTITIES, ACCOUNTS, "Pat", "cfu:1")
    assert (req.status, req.total, req.ref_number, len(req.entries[0]["lines"])) == (S.PENDING, Decimal("9500.00"), f"FO-JE-{req.pk}", 2)
    assert req.entries[0]["lines"][1]["description"] == "Accrue October adviser fee"  # a line without its own text takes the memo

    cf = FakeCampfire()
    with pytest.raises(journals.JournalError, match="someone else has to approve"):
        journals.approve(req, cf, "Pat", "cfu:1")  # type: ignore[arg-type]
    req = journals.approve(req, cf, "Rae", "cfu:2")  # type: ignore[arg-type]
    assert req.status == S.SENT and req.entries[0]["campfire_id"] == 9001 and req.decided_by == "Rae"
    body = cf.sent[0]
    assert (body["entity"], body["date"], body["source"], body["source_id"], body["ref_number"]) == ("2", "2026-10-31", "Family Office", f"fo-journal-{req.pk}-1", req.ref_number)
    assert body["transactions"][0] == {"account": "65", "debit_amount_native": 9500.0, "tags": [], "bank_description": "Granite Peak, October",
                                       "external_id": f"fo-journal-{req.pk}-1-1"}
    assert body["transactions"][1]["credit_amount_native"] == 9500.0
    with pytest.raises(journals.JournalError, match="already been decided"):
        journals.reject(req, "Rae", "cfu:2", "late")


@pytest.mark.django_db
def test_transfer_books_both_entities_and_a_refusal_keeps_what_was_posted(workspace: Workspace) -> None:
    data = {"from": "3", "to": "2", "amount": "50,000", "date": "2026-11-10", "memo": "Working capital advance"}
    with pytest.raises(journals.JournalError, match="different entity"):
        journals.submit_transfer(workspace, {**data, "to": "3"}, ENTITIES, ACCOUNTS, "Pat", "cfu:1")
    with pytest.raises(journals.JournalError, match="needs these accounts"):
        journals.submit_transfer(workspace, data, ENTITIES, {11: ACCOUNTS[11]}, "Pat", "cfu:1")
    req = journals.submit_transfer(workspace, data, ENTITIES, ACCOUNTS, "Pat", "cfu:1")
    payer, receiver = req.entries
    assert [(ln["account_id"], ln["debit"], ln["credit"]) for ln in payer["lines"]] == [(18, "50000.00", "0"), (11, "0", "50000.00")]
    assert [(ln["account_id"], ln["debit"], ln["credit"]) for ln in receiver["lines"]] == [(11, "50000.00", "0"), (28, "0", "50000.00")]
    assert req.total == Decimal("100000.00") and "Due to Alder Real Estate Holdings LLC" in receiver["lines"][1]["description"]

    cf = FakeCampfire([201, 400])  # the first entity posts, Campfire refuses the second
    with pytest.raises(journals.JournalError, match="Alder Management LLC"):
        journals.approve(req, cf, "Rae", "cfu:2")  # type: ignore[arg-type]
    req.refresh_from_db()
    assert req.status == S.PENDING and req.entries[0].get("campfire_id") == 9001 and "400" in req.error
    with pytest.raises(journals.JournalError, match="already in Campfire"):
        journals.reject(req, "Rae", "cfu:2", "changed my mind")
    again = FakeCampfire([202])  # approving again sends only the entry that is missing
    req = journals.approve(req, again, "Rae", "cfu:2")  # type: ignore[arg-type]
    assert len(again.sent) == 1 and again.sent[0]["entity"] == "2" and req.status == S.AWAITING and not req.error


@pytest.mark.django_db
def test_journals_page(client, workspace: Workspace) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import unquote

    from tests.conftest import context_token

    cf = FakeCampfire()
    with mock.patch("gl.journal_views.CampfireClient", return_value=cf):
        ctx = lambda user: f"/fo/frame/journals?cf_ctx={context_token(customer_id=workspace.customer_id, surface='nav', user_id=user)}"  # noqa: E731
        page = client.get(ctx(3)).content.decode()
        assert "Operating activity, October 2026" in page and "Write an entry" in page and "Transfer between entities" in page and "8,500.00" in page
        token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
        bad = client.post("/fo/frame/journals", {"fo_t": token, "action": "enter", **MANUAL, "credit_2": "1"}).content.decode()
        assert "doesn&#x27;t balance" in bad and 'value="9,500"' in bad and '<details class="settings" id="enter" open>' in bad  # what was typed comes back
        out = client.post("/fo/frame/journals", {"fo_t": token, "action": "enter", **MANUAL}).content.decode()
        assert "now waits for approval" in out and "To approve" in out and "You wrote this entry" in out
        req = JournalRequest.objects.get()
        other = unquote(client.get(ctx(4)).content.decode().split('name="fo_t" value="')[1].split('"')[0])
        assert "approved and posted to the ledger" in client.post("/fo/frame/journals", {"fo_t": other, "action": "approve", "entry": req.pk}).content.decode()
    assert len(cf.sent) == 1
