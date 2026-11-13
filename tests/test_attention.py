from datetime import date, timedelta
from decimal import Decimal

import pytest

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Entity, Family
from gl.models import AccountMapping
from gl.services.setup import SUGGESTED_ACCOUNTS
from investments.demo import seed_alder_investments
from investments.models import CapitalCall, Commitment, Valuation
from records.attention import INFO, TODO, URGENT, collect, headline

TODAY = date(2026, 6, 30)
CF = {"Robert Alder Revocable Trust": 9001, "Alder 2012 Dynasty Trust": 9002, "Alder Family Foundation": 9003,
      "Alder Family Investments LP": 9004, "Alder Real Estate Holdings LLC": 9005}


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    for name, cf in CF.items():
        Entity.objects.filter(family=family, name=name).update(campfire_entity_id=cf)
    for s in SUGGESTED_ACCOUNTS:
        AccountMapping.objects.create(workspace=workspace, role=s.role, campfire_account_id=int(s.number), campfire_account_label=s.name)
    return family


def find(items, text: str):  # type: ignore[no-untyped-def]
    return next((i for i in items if text in i.title), None)


@pytest.mark.django_db
def test_the_demo_familys_open_items(alder: Family) -> None:
    items = collect(alder.workspace, TODAY)
    held = find(items, "held for review")
    assert held.severity == URGENT and held.target == "records:review" and "Crescent" in held.detail
    call = find(items, "due within 14 days not sent")
    assert call.severity == URGENT and "Campfire vendor" in call.detail  # blocked: no GP vendor yet
    assert find(items, "ready to send to Campfire").severity == TODO
    assert find(items, "isn't closed for 5 entities").severity == TODO
    old_marks = find(items, "older than 120 days")
    assert old_marks.severity == INFO and "Harbor Street" in old_marks.detail
    # Crescent (last accepted NAV 140 days back; the newer one is held) and Harbor Street, each listed once.
    assert old_marks.title.startswith("2 holdings") and old_marks.detail.count("Crescent") == 1
    assert find(items, "without a Campfire vendor").severity == TODO
    assert [i.severity for i in items] == sorted((i.severity for i in items), key=lambda s: {URGENT: 0, TODO: 1, INFO: 2}[s])

    # Fixing things makes them go away.
    Valuation.objects.filter(status=Valuation.Status.HELD).update(status=Valuation.Status.REJECTED)
    Commitment.objects.update(campfire_vendor_id=1, campfire_vendor_name="GP")
    items = collect(alder.workspace, TODAY)
    assert find(items, "held for review") is None and find(items, "without a Campfire vendor") is None
    assert find(items, "due within 14 days not sent").severity == TODO  # no longer blocked


@pytest.mark.django_db
def test_overdue_calls_and_unmapped_gl(alder: Family) -> None:
    c = Commitment.objects.first()
    CapitalCall.objects.create(commitment=c, notice_date=TODAY - timedelta(days=20), due_date=TODAY - timedelta(days=1), amount=Decimal("250000"))
    AccountMapping.objects.filter(role="interest_income").delete()
    items = collect(alder.workspace, TODAY)
    assert find(items, "overdue ($250,000)").severity == URGENT
    unmapped = find(items, "not mapped to a GL account")
    assert unmapped.severity == URGENT and "Interest income" in unmapped.detail
    assert find(items, "held back").severity == URGENT


@pytest.mark.django_db
def test_headline_and_empty_workspace(alder: Family) -> None:
    head = headline(alder.workspace, TODAY)
    assert (head.families, head.total, head.unfunded) == (1, Decimal("23874900.00"), Decimal("2100000.00"))
    assert head.calls_due_30 == Decimal("500000")
    empty = Workspace.objects.create(customer_id=55, name="Empty")
    assert collect(empty, TODAY) == []


@pytest.mark.django_db
def test_home_page_in_campfire_and_standalone(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    from tests.conftest import context_token

    ws = alder.workspace
    r = client.get(f"/fo/frame/home?cf_ctx={context_token(customer_id=ws.customer_id, surface='nav', user_id=2)}")
    page = r.content.decode()
    assert r.status_code == 200 and "need attention" in page and "held for review" in page
    assert '/fo/frame/review?fo_t=' in page and '/fo/frame/calls?fo_t=' in page  # buttons stay inside the frame

    assert "Log in with Campfire" in client.get("/").content.decode()
    session = client.session
    session["workspace_id"] = ws.pk
    session["campfire_user"] = {"email": "ops@example.test", "campfire_user_id": 2}
    session.save()
    home = client.get("/").content.decode()
    assert "need attention" in home and 'href="/fo/entities"' in home
    assert client.get("/fo/entities").status_code == 200
