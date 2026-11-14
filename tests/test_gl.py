from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
import requests

from campfire_integration import events
from campfire_integration.client import connect_scopes
from campfire_integration.models import CampfireEvent, Workspace
from entities.demo import seed_alder
from entities.models import Entity, Family
from gl.models import SOURCE, AccountMapping, DimensionTag, JournalPosting, PostedTransaction, Role
from gl.services.posting import build_preview, month_end, refresh_statuses, send
from gl.services.setup import SUGGESTED_ACCOUNTS, setup_accounts, setup_dimensions
from gl.services.tieout import tie_out
from investments.demo import seed_alder_investments
from investments.models import CapitalCall, Transaction
from investments.services.privates import mark_call_paid

TODAY = date(2026, 6, 30)
S = JournalPosting.Status
CF_ENTITY = {"Robert Alder Revocable Trust": 9001, "Alder 2012 Dynasty Trust": 9002, "Alder Family Foundation": 9003,
             "Alder Family Investments LP": 9004, "Alder Real Estate Holdings LLC": 9005}
GL = {s.role: (int(s.number), f"{s.number} {s.name}") for s in SUGGESTED_ACCOUNTS}


def _response(status: int, payload: Any = None) -> requests.Response:
    import json

    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(payload if payload is not None else {}).encode()
    r.request = requests.Request("POST", "https://api.campfire.test/x").prepare()
    r.url = "https://api.campfire.test/x"
    return r


class FakeClient:
    """Stands in for CampfireClient: records requests and answers from a script."""

    def __init__(self, answer: Any = None, gets: dict[str, Any] | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.answer = answer or (lambda body: _response(202, {"draft_id": 77}))
        self.gets = gets or {}
        self.get_calls: list[tuple[str, Any]] = []

    def request(self, method: str, path: str, *, json: Any = None, params: Any = None, timeout: float = 15) -> requests.Response:
        self.sent.append({"method": method, "path": path, "json": json})
        return self.answer(json)

    def get_json(self, path: str, *, params: Any = None) -> Any:
        self.get_calls.append((path, params))
        value = self.gets[path]
        return value(params) if callable(value) else value


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    for name, cf_id in CF_ENTITY.items():
        Entity.objects.filter(family=family, name=name).update(campfire_entity_id=cf_id)
    for role, (account_id, label) in GL.items():
        AccountMapping.objects.create(workspace=workspace, role=role, campfire_account_id=account_id, campfire_account_label=label)
    return family


def journal(preview, entity: str, month: date):  # type: ignore[no-untyped-def]
    return next(j for j in preview.journals if j.campfire_entity_id == CF_ENTITY[entity] and j.period_end == month)


def line_amount(j, role: str) -> Decimal:  # type: ignore[no-untyped-def]
    return sum((ln.amount for ln in j.lines if ln.campfire_account_id == GL[role][0]), Decimal(0))


# ---------------------------------------------------------------------------------------------- preview


@pytest.mark.django_db
def test_preview_balances_and_applies_fifo_cost(alder: Family) -> None:
    preview = build_preview(alder.workspace, through=TODAY)
    assert preview.journals and all(j.ok for j in preview.journals)
    for j in preview.journals:
        assert sum((ln.amount for ln in j.lines), Decimal(0)) == 0
        assert j.je_date == min(j.period_end, TODAY)

    # The sale of 2,000 USTM: FIFO cost 2,000 x $210 = $420,000; net proceeds $529,975; gain $109,975.
    j = journal(preview, "Robert Alder Revocable Trust", month_end(TODAY - timedelta(days=200)))
    assert line_amount(j, Role.CASH) == Decimal("529975.00")
    assert line_amount(j, Role.INVESTMENTS) == Decimal("-420000.00")
    assert line_amount(j, Role.REALIZED_GAIN) == Decimal("-109975.00")

    # Eleanor's IRA is owned by a person, not an entity booked in Campfire.
    assert preview.not_linked == {"Eleanor Alder": 2}


@pytest.mark.django_db
def test_fund_flows_go_through_clearing_and_return_of_capital_relieves_cost(alder: Family) -> None:
    preview = build_preview(alder.workspace, through=TODAY)
    roc_day = TODAY - timedelta(days=300)  # Summit IV return of capital, $200,000, within basis
    j = journal(preview, "Alder Family Investments LP", month_end(roc_day))
    assert line_amount(j, Role.CASH_CLEARING) == Decimal("200000.00")
    assert line_amount(j, Role.INVESTMENTS) == Decimal("-200000.00")
    assert line_amount(j, Role.CASH) == 0  # fund interests hold no custody cash


@pytest.mark.django_db
def test_unmapped_role_holds_back_only_that_group(alder: Family) -> None:
    AccountMapping.objects.filter(role=Role.INTEREST_INCOME).delete()
    preview = build_preview(alder.workspace, through=TODAY)
    blocked = [j for j in preview.journals if not j.ok]
    assert blocked and all(j.campfire_entity_id == CF_ENTITY["Alder 2012 Dynasty Trust"] for j in blocked)
    assert any("Interest income" in e for e in blocked[0].errors)
    assert len(preview.ready) == len(preview.journals) - len(blocked)


@pytest.mark.django_db
def test_narrower_mapping_wins(alder: Family) -> None:
    pe = alder.workspace.asset_classes.get(code="private_equity")
    AccountMapping.objects.create(workspace=alder.workspace, role=Role.INVESTMENTS, asset_class=pe, campfire_account_id=1510, campfire_account_label="1510 Private equity")
    preview = build_preview(alder.workspace, through=TODAY)
    lp = [j for j in preview.journals if j.campfire_entity_id == CF_ENTITY["Alder Family Investments LP"]]
    accounts = {ln.campfire_account_id for j in lp for ln in j.lines if Role.INVESTMENTS in ln.roles}
    assert accounts == {1500, 1510}  # Summit (PE) to 1510, Crescent (VC) still 1500


@pytest.mark.django_db
def test_contribution_paid_by_campfire_bill_is_not_posted_again(alder: Family) -> None:
    call = CapitalCall.objects.get()
    call.campfire_bill_id = 555
    call.save()
    txn = mark_call_paid(call, TODAY)
    preview = build_preview(alder.workspace, through=TODAY)
    assert preview.billed_contributions == 1
    assert all(txn not in j.transactions for j in preview.journals)


@pytest.mark.django_db
def test_dimension_tags(alder: Family) -> None:
    ws = alder.workspace
    DimensionTag.objects.create(workspace=ws, dimension="family", key="alder", campfire_tag_id=11, label="Alder Family")
    DimensionTag.objects.create(workspace=ws, dimension="asset_class", key="public_equity", campfire_tag_id=21, label="Public equity")
    preview = build_preview(ws, through=TODAY)
    j = journal(preview, "Robert Alder Revocable Trust", month_end(TODAY - timedelta(days=200)))
    gain = next(ln for ln in j.lines if Role.REALIZED_GAIN in ln.roles)
    cash = next(ln for ln in j.lines if Role.CASH in ln.roles)
    assert gain.tags == (11, 21) and cash.tags == (11,)  # cash lines carry account-level dimensions only

    # Account-level dimensions (branch, tax treatment, custodian) go on every line, cash included.
    DimensionTag.objects.create(workspace=ws, dimension="tax_treatment", key="taxable", campfire_tag_id=31, label="Taxable")
    DimensionTag.objects.create(workspace=ws, dimension="branch", key=str(Entity.objects.get(name="Robert Alder Revocable Trust").branch_id), campfire_tag_id=41, label="Founders (G1)")
    j = journal(build_preview(ws, through=TODAY), "Robert Alder Revocable Trust", month_end(TODAY - timedelta(days=200)))
    assert next(ln for ln in j.lines if Role.CASH in ln.roles).tags == (11, 31, 41)
    assert not any("Holding" in w for w in j.warnings)  # holding dimension isn't set up at all
    assert any("Asset class 'Fixed income'" in w for jj in preview.journals for w in jj.warnings)


# --------------------------------------------------------------------------------------------- sending


@pytest.mark.django_db
def test_send_submits_drafts_and_locks_transactions(alder: Family) -> None:
    ws = alder.workspace
    client = FakeClient()
    ready = len(build_preview(ws, through=TODAY).ready)
    postings = send(ws, through=TODAY, client=client, created_by="ops")  # type: ignore[arg-type]
    assert len(postings) == ready == len(client.sent)
    assert {p.status for p in postings} == {S.SUBMITTED}

    body = client.sent[0]["json"]
    p = postings[0]
    assert (body["source"], body["source_id"], body["entity"], body["type"]) == (SOURCE, p.source_id, str(p.campfire_entity_id), "journal_entry")
    assert body["transactions"][0]["external_id"] == f"{p.source_id}-1"
    debits = sum(Decimal(str(t.get("debit_amount_native", 0))) for t in body["transactions"])
    credits = sum(Decimal(str(t.get("credit_amount_native", 0))) for t in body["transactions"])
    assert debits == credits == p.total
    assert all(("debit_amount_native" in t) != ("credit_amount_native" in t) for t in body["transactions"])

    # Everything sent is locked: nothing left to post except the person-owned IRA.
    again = build_preview(ws, through=TODAY)
    assert again.journals == [] and again.not_linked == {"Eleanor Alder": 2}
    assert PostedTransaction.objects.count() == sum(len(p.transaction_ids) for p in postings)


@pytest.mark.django_db
def test_send_outcomes(alder: Family) -> None:
    ws = alder.workspace
    through = TODAY - timedelta(days=1250)  # two groups: the property purchase, then Summit's first call
    answers = iter([_response(201, {"id": 4242}), _response(400, {"detail": "Period is locked"})])
    postings = send(ws, through=through, client=FakeClient(lambda body: next(answers)))  # type: ignore[arg-type]
    posted, failed = postings
    assert (posted.status, posted.campfire_journal_id) == (S.POSTED, 4242)
    assert failed.status == S.FAILED and "Period is locked" in failed.error
    assert not failed.items.exists()  # released: the next run posts them again
    assert build_preview(ws, through=through).journals[0].transactions

    def boom(body: Any) -> requests.Response:
        raise requests.ConnectionError("reset")

    (unsure,) = send(ws, through=through, client=FakeClient(boom))  # type: ignore[arg-type]
    assert unsure.status == S.UNCERTAIN and unsure.items.exists()  # held until we know


@pytest.mark.django_db
def test_refresh_and_events_confirm_or_release(alder: Family) -> None:
    ws = alder.workspace
    postings = send(ws, through=TODAY, client=FakeClient())  # type: ignore[arg-type]
    first, second = postings[0], postings[1]

    gl = {"results": [{"id": 900, "source": SOURCE, "source_id": first.source_id}, {"id": 901, "source": "CSV Upload", "source_id": "x.csv"}], "next": None}
    assert refresh_statuses(ws, FakeClient(gets={"/coa/api/journal_entry": gl})) == 1  # type: ignore[arg-type]
    first.refresh_from_db()
    assert (first.status, first.campfire_journal_id) == (S.POSTED, 900)

    # An approval in Campfire arrives as JournalEntry.created; the handler re-reads the entry.
    from unittest import mock

    with mock.patch("gl.handlers.CampfireClient") as cc:
        cc.return_value = FakeClient(gets={"/coa/api/journal_entry/901": {"id": 901, "source": SOURCE, "source_id": second.source_id}})
        events.dispatch(CampfireEvent.objects.create(workspace=ws, customer_id=ws.customer_id, topic="JournalEntry.created", object_id="901", payload={}))
    second.refresh_from_db()
    assert (second.status, second.campfire_journal_id) == (S.POSTED, 901)

    events.dispatch(CampfireEvent.objects.create(workspace=ws, customer_id=ws.customer_id, topic="JournalEntry.deleted", object_id="901", payload={}))
    second.refresh_from_db()
    assert second.status == S.RELEASED and not second.items.exists()
    assert second.transaction_ids  # the audit trail keeps what it carried
    assert any(j.period_end == second.period_end and j.campfire_entity_id == second.campfire_entity_id for j in build_preview(ws, through=TODAY).journals)


# --------------------------------------------------------------------------------------------- tie-out


@pytest.mark.django_db
def test_tie_out(alder: Family) -> None:
    ws = alder.workspace
    through = TODAY - timedelta(days=880)
    # The revocable trust's entry posts; the others wait for approval in Campfire.
    send(ws, through=through, client=FakeClient(lambda body: _response(201, {"id": 1}) if body["entity"] == "9001" else _response(202, {})))  # type: ignore[arg-type]
    # Campfire's trial balance for the revocable trust after that entry: cash and cost as the FO book has them.
    tb = {"trial_balance": {"accounts": [
        {"id": "1150", "ending_balances": {"debits": 300000.0, "credits": 0.0}},
        {"id": "1500", "ending_balances": {"debits": 1700000.0, "credits": 0.0}},
    ]}}
    client = FakeClient(gets={"/ca/api/get_trial_balance": lambda params: tb if params["entity"] == 9001 else {"trial_balance": {"accounts": []}}})
    result = tie_out(ws, through, client)  # type: ignore[arg-type]
    rrt = {r.campfire_account_id: r for r in result.rows if r.campfire_entity_id == 9001}
    assert rrt[1150].fo_balance == Decimal("300000.00") and rrt[1150].ties
    assert rrt[1500].ties
    others = [r for r in result.rows if r.campfire_entity_id != 9001]
    assert others and all(r.awaiting_approval == r.fo_balance and r.gl_balance == 0 and r.ties for r in others)

    tb["trial_balance"]["accounts"][0]["ending_balances"]["debits"] = 300100.0  # an entry booked by hand
    off = {r.campfire_account_id: r for r in tie_out(ws, through, client).rows if r.campfire_entity_id == 9001}  # type: ignore[arg-type]
    assert off[1150].difference == Decimal("100.00") and not off[1150].ties


# ----------------------------------------------------------------------------------------------- setup


@pytest.mark.django_db
def test_setup_accounts_and_dimensions(workspace: Workspace) -> None:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    created: list[dict[str, Any]] = []

    def answer(body: Any) -> requests.Response:
        created.append(body)
        return _response(201, {"id": 7000 + len(created), **body})

    chart = [{"id": 31, "number": "1170", "name": "Transfers clearing"},  # same name: reused
             {"id": 32, "number": "1500", "name": "Intercompany Receivable"}]  # same number, other account: not reused
    client = FakeClient(answer, gets={"/coa/api/account": {"results": chart, "next": None},
                                      "/coa/api/tag-group": {"results": [], "next": None}, "/coa/api/tag": {"results": [], "next": None}})
    preview = setup_accounts(workspace, client, create=False)  # type: ignore[arg-type]
    assert [s.action for s in preview].count("reuse") == 1 and not AccountMapping.objects.exists() and not created

    steps = setup_accounts(workspace, client, create=True)  # type: ignore[arg-type]
    assert AccountMapping.objects.get(role=Role.TRANSFERS).campfire_account_id == 31
    investments = next(b for b in created if b["name"] == "Investments at cost")
    assert investments["number"] == "1501"  # 1500 is taken by intercompany receivable
    assert AccountMapping.objects.get(role=Role.INVESTMENTS).campfire_account_id != 32
    assert len(created) == len(SUGGESTED_ACCOUNTS) - 1 and steps[2].action == "mapped" and steps[0].action == "created"
    assert setup_accounts(workspace, client, create=True)[0].action == "mapped"  # idempotent
    assert len(created) == len(SUGGESTED_ACCOUNTS) - 1

    created.clear()
    setup_dimensions(workspace, client, create=True)  # type: ignore[arg-type]
    groups = [b for b in created if "group" not in b]
    assert [g["name"] for g in groups] == ["FO Family", "FO Family Branch", "FO Tax Treatment", "FO Custodian", "FO Asset Class", "FO Holding",
                                           "FO Family Member", "FO Property"]
    assert DimensionTag.objects.filter(dimension="person").count() == 5  # the demo family's people
    assert list(DimensionTag.objects.filter(dimension="property").values_list("label", flat=True)) == ["Harbor Street Mixed-Use Property"]  # real estate held
    assert set(DimensionTag.objects.filter(dimension="tax_treatment").values_list("label", flat=True)) == {"Taxable", "Tax-deferred", "Tax-exempt"}
    assert DimensionTag.objects.filter(dimension="custodian").count() == 1  # the demo's one custodian
    assert DimensionTag.objects.filter(dimension="holding").count() == workspace.instruments.count()


# ----------------------------------------------------------------------------------------------- scopes


def test_connect_scopes_request_draft_tier(settings) -> None:  # type: ignore[no-untyped-def]
    settings.CAMPFIRE = {**settings.CAMPFIRE, "SCOPES": ["openid", "journal_entries:write", "bills:write", "accounts:write"], "POSTING_MODE": "draft"}
    assert connect_scopes() == ["openid", "journal_entries:draft", "bills:draft", "accounts:write"]
    settings.CAMPFIRE = {**settings.CAMPFIRE, "POSTING_MODE": "direct"}
    assert connect_scopes() == ["openid", "journal_entries:write", "bills:write", "accounts:write"]


def test_default_scopes_are_the_p1_set() -> None:
    from config.settings import DEFAULT_CAMPFIRE_SCOPES

    assert "journal_entries:write" in DEFAULT_CAMPFIRE_SCOPES and not any(s.endswith(":approve") for s in DEFAULT_CAMPFIRE_SCOPES)


# ------------------------------------------------------------------------------------------------- page


@pytest.mark.django_db
def test_gl_page_send_and_link(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    from unittest import mock

    from tests.conftest import context_token

    ws = alder.workspace
    fake = FakeClient(gets={
        "/coa/api/entity": [{"id": 9001, "name": "Campfire: RRT"}, {"id": 9002, "name": "Campfire: Dynasty"}],
        "/coa/api/account": {"results": [{"id": 1150, "number": "1150", "name": "Cash held at custodians"}], "next": None},
    })
    with mock.patch("gl.views.CampfireClient", return_value=fake):
        r = client.get(f"/fo/frame/gl?cf_ctx={context_token(customer_id=ws.customer_id, surface='nav', user_id=5)}")
        page = r.content.decode()
        assert r.status_code == 200 and "Ready to post" in page and "Campfire: Dynasty" in page
        token = page.split('name="fo_t" value="')[1].split('"')[0]

        ready = len(build_preview(ws, through=__import__("django.utils.timezone", fromlist=["x"]).localdate()).ready)
        r = client.post("/fo/frame/gl", {"fo_t": token, "action": "send"})
        assert r.status_code == 200 and f"Sent {ready} journal entries" in r.content.decode()
        assert JournalPosting.objects.filter(status=S.SUBMITTED, created_by="Campfire user 5").count() == ready

        posting = JournalPosting.objects.first()
        r = client.post("/fo/frame/gl", {"fo_t": token, "action": "release", "posting": posting.pk})
        posting.refresh_from_db()
        assert posting.status == S.RELEASED

        # Linking ignores ids that aren't Campfire entities in this workspace.
        dyn = Entity.objects.get(family=alder, name="Alder 2012 Dynasty Trust")
        client.post("/fo/frame/gl", {"fo_t": token, "action": "link", f"entity_{dyn.pk}": "424242"})
        dyn.refresh_from_db()
        assert dyn.campfire_entity_id == 9002
        client.post("/fo/frame/gl", {"fo_t": token, "action": "link", f"entity_{dyn.pk}": ""})
        dyn.refresh_from_db()
        assert dyn.campfire_entity_id is None

    # Link by matching name: only unlinked entities, only exact (case/space-insensitive) unique matches.
    fake.gets["/coa/api/entity"] = [{"id": 7001, "name": "alder 2012  dynasty trust"}, {"id": 7002, "name": "Alder Family Foundation"},
                                    {"id": 7003, "name": "Alder Family Foundation"}]
    with mock.patch("gl.views.CampfireClient", return_value=fake):
        r = client.post("/fo/frame/gl", {"fo_t": token, "action": "auto_link"})
    assert "Linked 1 entity" in r.content.decode()
    dyn.refresh_from_db()
    assert dyn.campfire_entity_id == 7001
    assert Entity.objects.get(family=alder, name="Alder Family Foundation").campfire_entity_id is None  # two matches: left alone


@pytest.mark.django_db
def test_gl_page_without_campfire_connection(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    from unittest import mock

    from campfire_integration.client import NotConnected

    session = client.session
    session["workspace_id"] = alder.workspace.pk
    session.save()
    with mock.patch("gl.views.list_entities", side_effect=NotConnected("x")):
        r = client.get("/fo/gl")
    assert r.status_code == 200 and "load entities and accounts from Campfire" in r.content.decode()


@pytest.mark.django_db
def test_post_from_skips_history_already_in_opening_balances(alder: Family) -> None:
    from gl.models import GLSettings

    ws = alder.workspace
    everything = build_preview(ws, through=TODAY)
    GLSettings.objects.update_or_create(workspace=ws, defaults={"post_from": TODAY - timedelta(days=100)})
    recent = build_preview(ws, through=TODAY)
    assert recent.journals and all(j.period_end >= TODAY - timedelta(days=131) for j in recent.journals)
    posted = sum(len(j.transactions) for j in recent.journals)
    assert recent.before_start + posted == sum(len(j.transactions) for j in everything.journals) + sum(everything.not_linked.values()) - sum(recent.not_linked.values())


@pytest.mark.django_db
def test_demo_setup_entity_names_match_the_demo_seed(workspace: Workspace) -> None:
    from gl.management.commands.campfire_demo_setup import ALDER_ENTITIES

    seed_alder(workspace)
    assert sorted(ALDER_ENTITIES) == sorted(Entity.objects.filter(family__workspace=workspace).values_list("name", flat=True))
