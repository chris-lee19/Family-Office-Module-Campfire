from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

import pytest

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Family
from gl.models import JournalPosting, PostedTransaction
from investments.demo import seed_alder_investments
from investments.models import CapitalCall, Commitment, Transaction
from investments.services.privates import summarize
from tests.conftest import APP_URL, context_token
from tests.test_calls import FakeCampfire
from tests.test_gl import FakeClient

TODAY = date(2026, 6, 30)


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    Commitment.objects.filter(instrument__name__startswith="Summit").update(campfire_vendor_id=501, campfire_vendor_name="Summit Growth Partners")
    return family


def open_panel(client, ws: Workspace, path: str, object_type: str | None, ids: list, surface: str = "panel:edit"):  # type: ignore[no-untyped-def,type-arg]
    return client.get(f"{path}?cf_ctx={context_token(customer_id=ws.customer_id, surface=surface, object_type=object_type, object_ids=ids, user_id=6)}")


def token_of(r) -> str:  # type: ignore[no-untyped-def]
    return r.content.decode().split('name="fo_t" value="')[1].split('"')[0]


def test_manifest_declares_buttons_and_tabs(settings) -> None:  # type: ignore[no-untyped-def]
    from campfire_integration.manifest import build_manifest

    m = build_manifest()
    slots = {s["key"]: s for s in m["slots"]}
    assert slots["fo-bill"]["object_type"] == "bill" and slots["fo-bill"]["action_type"] == "iframe"
    assert slots["fo-bill"]["action_config"] == {"url_template": f"{APP_URL}/fo/frame/bill/{{{{record.id}}}}", "presentation": "drawer", "width": "lg"}
    panels = {p["key"]: p for p in m["panels"]}
    assert panels["fo-bills-tab"]["placements"] == ["list"] and "{{record" not in panels["fo-bills-tab"]["url_template"]
    assert panels["fo-journal-entry-tab"]["object_type"] == "journal_entry"


@pytest.mark.django_db
def test_a_tab_opened_for_one_record_cant_open_another(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    ws = alder.workspace
    cf = FakeCampfire()
    with mock.patch("gl.panel_views.CampfireClient", return_value=cf):
        assert open_panel(client, ws, "/fo/frame/bill/7001", "bill", [7002]).status_code == 403
        assert open_panel(client, ws, "/fo/frame/bill/7001", None, [], surface="nav").status_code == 403
        assert open_panel(client, ws, "/fo/frame/bill/7001", "invoice", [7001]).status_code == 403
        assert open_panel(client, ws, "/fo/frame/bill/7001", "bill", [7001]).status_code == 200
        assert open_panel(client, ws, "/fo/frame/bill/7001", "bill", [7001], surface="detail_header").status_code == 200  # the drawer button


@pytest.mark.django_db
def test_bill_tab_shows_its_capital_call(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    call = CapitalCall.objects.get()
    CapitalCall.objects.filter(pk=call.pk).update(status=CapitalCall.Status.BILLED, campfire_bill_id=7001)
    cf = FakeCampfire()
    with mock.patch("gl.panel_views.CampfireClient", return_value=cf):
        r = open_panel(client, alder.workspace, "/fo/frame/bill/7001", "bill", [7001])
    page = r.content.decode()
    assert "Capital call: Summit Growth Partners IV" in page and "Bill approved in Campfire" in page and "Check payment" in page
    assert "frame-ancestors" in r["Content-Security-Policy"]


@pytest.mark.django_db
def test_gp_bill_keyed_in_campfire_becomes_a_capital_call(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    ws = alder.workspace
    summit = Commitment.objects.get(instrument__name__startswith="Summit")
    before = summarize(summit, TODAY + timedelta(days=30))
    paid_on = TODAY + timedelta(days=3)
    cf = FakeCampfire()
    cf.bills[8001] = {"id": 8001, "vendor": 501, "vendor_name": "Summit Growth Partners", "bill_number": "SGP-IV-CALL-14", "total_amount": 250000.0,
                      "amount_due": 0.0, "bill_date": TODAY.isoformat(), "due_date": (TODAY + timedelta(days=10)).isoformat(),
                      "payment_status": "paid", "paid_date": paid_on.isoformat(), "source": "Manual", "source_id": None, "is_deleted": False}
    with mock.patch("gl.panel_views.CampfireClient", return_value=cf):
        r = open_panel(client, ws, "/fo/frame/bill/8001", "bill", [8001])
        page = r.content.decode()
        assert "Record as a capital call" in page and "Summit Growth Partners IV" in page
        token = token_of(r)
        r = client.post(f"/fo/frame/bill/8001", {"fo_t": token, "action": "link", "commitment": summit.pk})
        assert "Recorded as a capital call" in r.content.decode() and "contribution is booked" in r.content.decode()
        client.post(f"/fo/frame/bill/8001", {"fo_t": token, "action": "link", "commitment": summit.pk})  # a second click
    linked = CapitalCall.objects.filter(campfire_bill_id=8001)
    assert linked.count() == 1
    call = linked.get()
    assert (call.status, call.amount, call.paid_on, call.source_ref) == (CapitalCall.Status.PAID, Decimal("250000.00"), paid_on, "SGP-IV-CALL-14")
    assert summarize(summit, TODAY + timedelta(days=30)).contributed == before.contributed + Decimal("250000")

    # A bill from a vendor that isn't a fund's GP.
    cf.bills[8002] = {**cf.bills[8001], "id": 8002, "vendor": 999}
    with mock.patch("gl.panel_views.CampfireClient", return_value=cf):
        assert "isn't a capital call" in open_panel(client, ws, "/fo/frame/bill/8002", "bill", [8002]).content.decode()


@pytest.mark.django_db
def test_bill_list_tab(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    CapitalCall.objects.update(status=CapitalCall.Status.SUBMITTED)
    r = open_panel(client, alder.workspace, "/fo/frame/bills", "bill", [], surface="panel:list")
    page = r.content.decode()
    assert r.status_code == 200 and "Summit Growth Partners IV" in page and "Bill awaiting approval in Campfire" in page


@pytest.mark.django_db
def test_journal_entry_tab_shows_lineage(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    ws = alder.workspace
    posting = JournalPosting.objects.create(workspace=ws, campfire_entity_id=1, period_end=TODAY, je_date=TODAY, memo="Family office investment activity",
                                            total=Decimal("4850"), status=JournalPosting.Status.SUBMITTED, created_by="Pat",
                                            lines=[{"account": 1150, "account_label": "1150 Cash held at custodians", "debit": "4850.00", "credit": "0.00",
                                                    "tags": [], "description": "FO: Cash at custodian", "external_id": "x-1"}])
    t = Transaction.objects.filter(txn_type="dividend").first()
    PostedTransaction.objects.create(posting=posting, transaction=t)

    # Approved in Campfire as entry 5150 (a new id): the tab finds it by source_id and links it.
    fake = FakeClient(gets={"/coa/api/journal_entry/5150": {"id": 5150, "source": "Family Office", "source_id": posting.source_id},
                            "/coa/api/journal_entry/6000": {"id": 6000, "source": "CSV Upload", "source_id": "x.csv"}})
    with mock.patch("gl.panel_views.CampfireClient", return_value=fake):
        page = open_panel(client, ws, "/fo/frame/journal-entry/5150", "journal_entry", [5150], surface="panel:detail").content.decode()
        assert f"From Family Office: {posting.ref_number}" in page and "1150 Cash held at custodians" in page
        assert "Behind it: 1 transaction" in page and str(t.account) in page
        other = open_panel(client, ws, "/fo/frame/journal-entry/6000", "journal_entry", [6000], surface="panel:detail").content.decode()
        assert "wasn't created by Family Office" in other
    posting.refresh_from_db()
    assert (posting.status, posting.campfire_journal_id) == (JournalPosting.Status.POSTED, 5150)
