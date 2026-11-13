from datetime import date
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Entity, Family
from gl.models import AccountMapping
from gl.services.networth import build_net_worth, refresh_balances
from gl.services.setup import SUGGESTED_ACCOUNTS
from investments.demo import seed_alder_investments
from investments.services.portfolio import build_portfolio
from records.attention import collect, headline
from tests.test_gl import FakeClient

TODAY = date(2026, 6, 30)
RRT = 9001
CF = {"Robert Alder Revocable Trust": RRT, "Alder 2012 Dynasty Trust": 9002, "Alder Family Foundation": 9003,
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


def campfire() -> FakeClient:
    chart = [{"id": 1040, "number": "1040", "name": "Operating cash", "account_subtype": "BANK"},
             {"id": 1180, "number": "1180", "name": "Due from related entities", "account_subtype": "OTHER_CURRENT_ASSET"},
             {"id": 2600, "number": "2600", "name": "Mortgage payable", "account_subtype": "LONG_TERM_LIABILITY"},
             {"id": 1150, "number": "1150", "name": "Cash held at custodians", "account_subtype": "BANK"},
             {"id": 1630, "number": "1630", "name": "Unrealized appreciation on investments", "account_subtype": "OTHER_ASSET"}]

    def tb(params: dict[str, Any]) -> dict[str, Any]:
        if params["entity"] != RRT:
            return {"trial_balance": {"accounts": []}}
        row = lambda i, t, d, c: {"id": str(i), "number": str(i), "name": f"acct {i}", "account_type": t, "ending_balances": {"debits": d, "credits": c}}  # noqa: E731
        return {"trial_balance": {"accounts": [
            row(1040, "ASSET", 100000.0, 0.0),          # bank cash: counted
            row(1180, "ASSET", 25000.0, 0.0),           # other asset: counted
            row(2600, "LIABILITY", 0.0, 40000.0),       # liability: subtracted
            row(1150, "ASSET", 611475.0, 0.0),          # custody cash: mirrored by the investment book, not counted again
            row(1630, "ASSET", 90000.0, 0.0),           # unrealized appreciation: mirrored, not counted
            row(3000, "EQUITY", 0.0, 1000000.0),        # equity: not an asset or a liability
            row(7100, "OTHER_INCOME", 0.0, 9500.0),     # income: neither
            {"id": "total", "name": "Total", "number": "", "account_type": None, "ending_balances": {"debits": 1.0, "credits": 1.0}},
        ]}}
    return FakeClient(gets={"/ca/api/get_trial_balance": tb, "/coa/api/account": {"results": chart, "next": None}})


@pytest.mark.django_db
def test_net_worth_adds_campfire_balances_without_double_counting(alder: Family) -> None:
    before = build_net_worth(alder, TODAY)
    assert before.net == before.investments and before.missing  # nothing read yet
    snaps = refresh_balances(alder.workspace, campfire(), TODAY)  # type: ignore[arg-type]
    assert len(snaps) == 5

    nw = build_net_worth(alder, TODAY)
    assert nw.investments == build_portfolio(alder, TODAY).total == Decimal("23874900.00")
    assert (nw.total("cash"), nw.total("other"), nw.total("liability")) == (Decimal("100000"), Decimal("25000"), Decimal("40000"))
    assert nw.net == nw.investments + Decimal("85000")
    assert {ln.account_id for ln in nw.lines} == {1040, 1180, 2600} and not nw.missing
    # The revocable trust is Robert's: his net worth rises by the trust's Campfire balances.
    assert nw.by_person["Robert Alder"] == nw.portfolio.by_person["Robert Alder"] + Decimal("85000")
    assert sum(nw.by_person.values()) == nw.net


@pytest.mark.django_db
def test_home_and_investments_page(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import unquote

    from tests.conftest import context_token

    ws = alder.workspace
    assert any("Campfire balances never read" in i.title for i in collect(ws, TODAY))
    r = client.get(f"/fo/frame/investments?cf_ctx={context_token(customer_id=ws.customer_id, surface='nav')}")
    page = r.content.decode()
    assert "No Campfire balances read yet" in page and "Refresh from Campfire" in page
    token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
    with mock.patch("campfire_integration.client.CampfireClient", return_value=campfire()):
        page = client.post("/fo/frame/investments", {"fo_t": token, "action": "refresh_balances"}).content.decode()
    assert "Read balances for 5 Campfire entities" in page and "Net worth by person" in page and "1040 acct 1040" in page
    assert not any("Campfire balances" in i.title for i in collect(ws, TODAY))
    head = headline(ws)  # the page read balances as of today
    assert head.has_campfire_balances and head.net_worth == head.total + Decimal("85000")
