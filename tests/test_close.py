from datetime import date, timedelta
from typing import Any
from unittest import mock

import pytest
from django.utils import timezone

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Entity, Family
from gl.models import AccountMapping, JournalPosting
from gl.services import close
from gl.services.setup import SUGGESTED_ACCOUNTS
from gl.services.tieout import fo_balances
from investments.demo import seed_alder_investments
from tests.test_gl import FakeClient, _response

TODAY = date(2026, 6, 30)
RRT = 9001
MONTH = close.month_start(TODAY - timedelta(days=200))  # the month of Robert's sale
CF = {"Robert Alder Revocable Trust": RRT, "Alder 2012 Dynasty Trust": 9002, "Alder Family Foundation": 9003,
      "Alder Family Investments LP": 9004, "Alder Real Estate Holdings LLC": 9005}


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    for name, cf in CF.items():
        Entity.objects.filter(family=family, name=name).update(campfire_entity_id=cf)
    for s in SUGGESTED_ACCOUNTS:
        AccountMapping.objects.create(workspace=workspace, role=s.role, campfire_account_id=int(s.number), campfire_account_label=f"{s.number} {s.name}")
    return family


def rrt(ws: Workspace) -> close.EntityClose:
    return next(r for r in close.month_status(ws, MONTH) if r.campfire_entity_id == RRT)


def done(r: close.EntityClose) -> dict[str, bool]:
    return {s.key: s.done for s in r.steps}


def trial_balance(ws: Workspace, end: date) -> Any:
    """Campfire's trial balance as if everything sent had posted: FO balances by entity and account."""
    rows, _ = fo_balances(ws, end)

    def answer(params: dict[str, Any]) -> dict[str, Any]:
        accounts = [{"id": str(acct), "ending_balances": {"debits": float(r.fo_balance), "credits": 0.0}}
                    for (cf, acct), r in rows.items() if cf == params["entity"]]
        return {"trial_balance": {"accounts": accounts}}
    return answer


@pytest.mark.django_db
def test_a_month_closes_step_by_step(alder: Family) -> None:
    ws = alder.workspace
    end = close.month_end(MONTH)
    r = rrt(ws)
    assert done(r) == {"data": False, "review": True, "posted": False, "approved": True, "ties": False, "signoff": False}
    assert r.next_step.key == "data"
    assert any("custody transactions in the month" in e for e in r.steps[0].evidence)

    close.confirm_data(ws, RRT, MONTH, "Pat")
    posted = FakeClient(lambda body: _response(201, {"id": 77}))
    from gl.services.posting import send

    sent = send(ws, through=end, client=posted, created_by="Pat", created_by_id="cfu:7", campfire_entity_id=RRT)  # type: ignore[arg-type]
    assert sent and all(p.campfire_entity_id == RRT for p in sent)  # only this entity
    assert done(rrt(ws))["posted"] and done(rrt(ws))["approved"]
    with pytest.raises(close.CloseRefused, match="Ties to GL"):
        close.sign_off(ws, RRT, MONTH, "Rae", "cfu:8")

    close.run_tieout(ws, MONTH, FakeClient(gets={"/ca/api/get_trial_balance": trial_balance(ws, end)}))  # type: ignore[arg-type]
    r = rrt(ws)
    assert done(r)["ties"] and r.next_step.key == "signoff"

    with pytest.raises(close.CloseRefused, match="someone else"):
        close.sign_off(ws, RRT, MONTH, "Pat", "cfu:7")
    close.sign_off(ws, RRT, MONTH, "Rae", "cfu:8", note="Ties; no exceptions")
    r = rrt(ws)
    assert r.closed and r.next_step is None and "Rae" in r.steps[-1].evidence[0]

    # An entry posted after the tie-out makes it stale.
    JournalPosting.objects.filter(campfire_entity_id=RRT).update(posted_at=timezone.now() + timedelta(minutes=5))
    assert not done(rrt(ws))["ties"]

    with pytest.raises(close.CloseRefused, match="why"):
        close.reopen(ws, RRT, MONTH, "Rae", "")
    close.reopen(ws, RRT, MONTH, "Rae", "Late custodian correction")
    assert not rrt(ws).closed and not done(rrt(ws))["data"]


@pytest.mark.django_db
def test_tieout_that_does_not_tie_shows_the_difference(alder: Family) -> None:
    ws = alder.workspace
    end = close.month_end(MONTH)
    from gl.services.posting import send

    send(ws, through=end, client=FakeClient(lambda body: _response(201, {"id": 1})), campfire_entity_id=RRT)  # type: ignore[arg-type]
    good = trial_balance(ws, end)

    def off(params: dict[str, Any]) -> dict[str, Any]:
        tb = good(params)
        if params["entity"] == RRT:
            tb["trial_balance"]["accounts"][0]["ending_balances"]["debits"] += 250.0
        return tb

    close.run_tieout(ws, MONTH, FakeClient(gets={"/ca/api/get_trial_balance": off}))  # type: ignore[arg-type]
    step = next(s for s in rrt(ws).steps if s.key == "ties")
    assert not step.done and step.problem and "differs by 250.00" in step.evidence[0]


@pytest.mark.django_db
def test_close_page(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import unquote

    from tests.conftest import context_token

    ws = alder.workspace
    r = client.get(f"/fo/frame/close?month={MONTH:%Y-%m}&cf_ctx={context_token(customer_id=ws.customer_id, surface='nav', user_id=7)}")
    page = r.content.decode()
    assert r.status_code == 200 and f"Month-end close: {MONTH:%B %Y}" in page and "Robert Alder Revocable Trust" in page
    token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
    r = client.post("/fo/frame/close", {"fo_t": token, "month": f"{MONTH:%Y-%m}", "entity": RRT, "action": "data"})
    assert "Data confirmed complete" in r.content.decode()
    with mock.patch("gl.close_views.CampfireClient", return_value=FakeClient()):
        r = client.post("/fo/frame/close", {"fo_t": token, "month": f"{MONTH:%Y-%m}", "entity": RRT, "action": "post"})
    assert "Sent" in r.content.decode()
    assert JournalPosting.objects.filter(campfire_entity_id=RRT, created_by_id="cfu:7").exists()
    assert JournalPosting.objects.exclude(campfire_entity_id=RRT).count() == 0
    r = client.post("/fo/frame/close", {"fo_t": token, "month": f"{MONTH:%Y-%m}", "entity": 123456, "action": "data"})
    assert "isn&#x27;t booked" in r.content.decode()
