from datetime import date
from decimal import Decimal
from urllib.parse import unquote

import pytest
from django.utils import timezone

from campfire_integration.models import CampfireUser, Workspace
from entities.demo import seed_alder
from entities.models import Family
from investments.demo import alder_activity_file, seed_alder_investments
from investments.importers.custodian import commit, stage
from investments.importers.formats import PRESETS
from investments.models import Account, ImportBatch, Instrument, Valuation
from records.models import AuditEntry, ControlSettings
from records.review import queue
from tests.conftest import context_token

TODAY = date(2026, 6, 30)


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    CampfireUser.objects.create(workspace=workspace, campfire_user_id=7, name="Pat Preparer")
    CampfireUser.objects.create(workspace=workspace, campfire_user_id=8, name="Rae Reviewer")
    return family


def person(client_class, workspace: Workspace, user_id: int):  # type: ignore[no-untyped-def]
    """A browser session inside the Campfire frame as one Campfire user."""
    c = client_class()
    r = c.get(f"/fo/frame/review?cf_ctx={context_token(customer_id=workspace.customer_id, surface='nav', user_id=user_id)}")
    assert r.status_code == 200
    token = r.content.decode().split('name="fo_t" value="')[1].split('"')[0]

    class P:
        def post(self, path: str, data: dict):  # type: ignore[no-untyped-def,type-arg]
            return c.post(path, {"fo_t": unquote(token), **data})

        def page(self) -> str:
            return c.get("/fo/frame/review", {"fo_t": unquote(token)}).content.decode()

    return P()


def summit(alder: Family) -> tuple[Account, Instrument]:
    return Account.objects.get(name="AFI LP – Summit Growth Partners IV"), Instrument.objects.get(name__startswith="Summit")


@pytest.mark.django_db
def test_preparer_cannot_review_their_own_mark(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    ws = alder.workspace
    pat, rae = person(type(client), ws, 7), person(type(client), ws, 8)
    account, fund = summit(alder)
    r = pat.post("/fo/frame/records/valuations/new", {"account": account.pk, "instrument": fund.pk, "as_of": TODAY.isoformat(),
                                                      "value": "9400000", "method": "nav_statement"})
    assert "Held for review" in r.content.decode()
    mark = Valuation.objects.get(account=account, as_of=TODAY)
    assert (mark.status, mark.created_by, mark.created_by_id) == (Valuation.Status.HELD, "Pat Preparer", "cfu:7")
    page = rae.page()
    assert "Summit Growth Partners IV" in page and "Pat Preparer" in page and "you are Rae Reviewer" in page

    r = pat.post("/fo/frame/review", {"action": "accept", "valuation": mark.pk})
    assert "someone else has to review it" in r.content.decode()
    mark.refresh_from_db()
    assert mark.status == Valuation.Status.HELD

    r = rae.post("/fo/frame/review", {"action": "reject", "valuation": mark.pk})
    assert "Say why" in r.content.decode()
    r = rae.post("/fo/frame/review", {"action": "accept", "valuation": mark.pk, "note": "Confirmed with the GP"})
    assert "Accepted" in r.content.decode()
    mark.refresh_from_db()
    assert (mark.status, mark.reviewed_by, mark.reviewed_by_id, mark.review_note) == (Valuation.Status.ACCEPTED, "Rae Reviewer", "cfu:8", "Confirmed with the GP")
    assert AuditEntry.objects.filter(kind="valuations", object_id=str(mark.pk), actor="Rae Reviewer").exists()
    assert not any(h.valuation.pk == mark.pk for h in queue(ws).held)


@pytest.mark.django_db
def test_correcting_a_misread_statement(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    held = Valuation.objects.get(status=Valuation.Status.HELD)  # the demo's Crescent NAV read as 6,850,000
    rae = person(type(client), alder.workspace, 8)
    r = rae.post("/fo/frame/review", {"action": "correct", "valuation": held.pk, "value": "685,000", "note": "Decimal misread"})
    assert "Recorded 685,000" in r.content.decode()
    held.refresh_from_db()
    assert held.status == Valuation.Status.REJECTED and "Corrected to 685000" in held.review_note
    fixed = Valuation.objects.get(account=held.account, as_of=held.as_of, status=Valuation.Status.ACCEPTED)
    assert fixed.value == Decimal("685000") and fixed.created_by == "Rae Reviewer" and f"mark {held.pk}" in fixed.review_note


@pytest.mark.django_db
def test_imports_with_warnings_are_signed_off_by_someone_else(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    ws = alder.workspace
    batch = stage(ws, fmt=PRESETS["pershing_style_activity"], content=alder_activity_file(timezone.localdate()), file_name="nov.csv",
                  created_by="Pat Preparer", created_by_id="cfu:7", create_securities=True)
    batch = commit(batch)
    assert batch.status == ImportBatch.Status.COMMITTED and batch in queue(ws).warned
    pat, rae = person(type(client), ws, 7), person(type(client), ws, 8)
    assert "someone else" in pat.post("/fo/frame/review", {"action": "sign_off", "batch": batch.pk}).content.decode()
    assert "Signed off nov.csv" in rae.post("/fo/frame/review", {"action": "sign_off", "batch": batch.pk, "note": "Fee is the Q-end advisory fee"}).content.decode()
    batch.refresh_from_db()
    assert batch.reviewed_by == "Rae Reviewer" and batch not in queue(ws).warned


@pytest.mark.django_db
def test_one_person_office_can_switch_the_control_off(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    held = Valuation.objects.get(status=Valuation.Status.HELD)
    Valuation.objects.filter(pk=held.pk).update(created_by_id="cfu:7")
    pat = person(type(client), alder.workspace, 7)
    pat.post("/fo/frame/review", {"action": "policy"})  # unticked box = off
    assert ControlSettings.objects.get(workspace=alder.workspace).require_second_person is False
    assert AuditEntry.objects.filter(kind="settings", actor="Pat Preparer").exists()
    assert "Accepted" in pat.post("/fo/frame/review", {"action": "accept", "valuation": held.pk}).content.decode()


@pytest.mark.django_db
def test_unknown_reviewer_and_other_workspaces(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    held = Valuation.objects.get(status=Valuation.Status.HELD)
    session = client.session
    session["workspace_id"] = alder.workspace.pk
    session["campfire_user"] = {"email": "someone@example.test"}  # no Campfire user id: can't tell who it is
    session.save()
    assert "can&#x27;t tell who you are" in client.post("/fo/review", {"action": "accept", "valuation": held.pk}).content.decode()

    other = Workspace.objects.create(customer_id=4321, name="Other", access_token="a", refresh_token="r",
                                     token_expires_at=alder.workspace.token_expires_at, connected_at=alder.workspace.connected_at)
    seed_alder_investments(seed_alder(other), TODAY)
    theirs = Valuation.objects.get(account__family__workspace=other, status=Valuation.Status.HELD)
    rae = person(type(client), alder.workspace, 8)
    assert "isn&#x27;t in this workspace" in rae.post("/fo/frame/review", {"action": "accept", "valuation": theirs.pk}).content.decode()
    theirs.refresh_from_db()
    assert theirs.status == Valuation.Status.HELD
