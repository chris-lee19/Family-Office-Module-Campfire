"""Partnership accounting: capital accounts per partner from the books, K-1 figures to issue, K-1s to receive."""

from datetime import date
from decimal import Decimal
from typing import Any
from unittest import mock

import pytest

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Entity, Family
from gl.services import partnership as svc
from investments.demo import seed_alder_investments
from investments.models import K1

TODAY = date(2026, 11, 13)
TB = {"trial_balance": {"accounts": [
    {"id": "11", "name": "Operating cash", "account_type": "ASSET", "beginning_balances": {"debits": 780000, "credits": 0}, "ending_balances": {"debits": 830300.01, "credits": 0}},
    {"id": "15", "name": "Investments at cost", "account_type": "ASSET", "beginning_balances": {"debits": 4800000, "credits": 0}, "ending_balances": {"debits": 4800000, "credits": 0}},
    {"id": "28", "name": "Due to related entities", "account_type": "LIABILITY", "beginning_balances": {"debits": 0, "credits": 30000}, "ending_balances": {"debits": 0, "credits": 30000}},
    {"id": "39", "name": "Partners' capital", "account_type": "EQUITY", "activity": {"debits": 0, "credits": 100000}},
    {"id": "394", "name": "Distributions to beneficiaries and owners", "account_type": "EQUITY", "activity": {"debits": 40000, "credits": 0}},
    {"id": "712", "name": "Fund income distributions", "account_type": "OTHER_INCOME", "activity": {"debits": 0, "credits": 50000}},
    {"id": "711", "name": "Interest income", "account_type": "OTHER_INCOME", "activity": {"debits": 0, "credits": 10000.01}},
    {"id": "720", "name": "Realized gain / loss on investments", "account_type": "OTHER_INCOME", "activity": {"debits": 0, "credits": 0}},
    {"id": "654", "name": "Tax preparation and accounting fees", "account_type": "OPERATING_EXPENSES", "activity": {"debits": 69700, "credits": 0}},
    {"id": "total", "name": "Total"}]}}


class FakeCampfire:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_json(self, path: str, *, params: Any = None) -> Any:
        self.calls.append(params)
        return TB


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    Entity.objects.filter(family=family, name="Alder Family Investments LP").update(campfire_entity_id=9004)
    return family


@pytest.mark.django_db
def test_capital_accounts_split_the_books_by_ownership(alder: Family) -> None:
    lp = next(e for e in svc.partnerships(alder.workspace) if e.name == "Alder Family Investments LP")
    assert {e.name for e in svc.partnerships(alder.workspace)} == {"Alder Family Investments LP", "Alder Management LLC"}
    assert svc.partners_of(lp, TODAY) == [("Alder 2012 Dynasty Trust", Decimal("50")), ("Eleanor Alder Revocable Trust", Decimal("24.5")),
                                          ("Robert Alder Revocable Trust", Decimal("24.5")), ("Alder Management LLC", Decimal("1"))]
    cf = FakeCampfire()
    st = svc.capital_accounts(cf, lp, 2026, TODAY)  # type: ignore[arg-type]
    assert cf.calls[0] == {"entity": 9004, "start_date": "2026-01-01", "end_date": "2026-11-13"}
    # The entity's totals: opening 5,550,000; contributions 100,000; income 60,000.01 - 69,700 = -9,699.99; distributions 40,000.
    assert (st.total("opening"), st.total("contributions"), st.total("income"), st.total("distributions")) == (
        Decimal("5550000.00"), Decimal("100000.00"), Decimal("-9699.99"), Decimal("40000.00"))
    assert st.total("closing") == Decimal("5600300.01") and st.other_changes == 0  # ties to the books' net worth at the end
    dynasty, eleanor, robert, mgmt = st.partners
    assert (dynasty.opening, eleanor.opening, mgmt.opening) == (Decimal("2775000.00"), Decimal("1359750.00"), Decimal("55500.00"))
    assert dynasty.income + eleanor.income + robert.income + mgmt.income == Decimal("-9699.99")  # to the cent
    assert mgmt.closing == Decimal("55500.00") + Decimal("1000.00") + mgmt.income - Decimal("400.00")
    # K-1 figures by kind of income; an account with nothing in it adds no line.
    assert st.k1_lines == ["Ordinary income", "Interest income", "Deductions"]
    assert (st.k1_total("Ordinary income"), st.k1_total("Interest income"), st.k1_total("Deductions")) == (Decimal("50000.00"), Decimal("10000.01"), Decimal("-69700.00"))
    assert dynasty.k1 == {"Ordinary income": Decimal("25000.00"), "Interest income": Decimal("5000.01"), "Deductions": Decimal("-34850.00")}
    assert svc._split(Decimal("100"), [Decimal(1), Decimal(1), Decimal(1)]) == [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")]


@pytest.mark.django_db
def test_k1s_to_receive_one_per_fund_interest_and_year(alder: Family) -> None:
    ws = alder.workspace
    years = svc.tax_years(ws, TODAY)
    assert years[0] == 2025 and years == sorted(years, reverse=True)
    expected = svc.expected_k1s(ws, 2025)
    assert {k.instrument.name for k in expected} == {"Summit Growth Partners IV, L.P.", "Crescent Venture Fund II, L.P."} and not any(k.received for k in expected)
    summit = next(k for k in expected if k.instrument.name.startswith("Summit"))
    data = {"account": summit.account.pk, "instrument": summit.instrument.pk, "tax_year": "2025", "status": "final", "received_on": "2026-03-20",
            "ordinary_income": "(12,400)", "interest_income": "3,150.50", "capital_gains": "88000", "distributions": "500000"}
    with pytest.raises(ValueError, match="date the K-1 arrived"):
        svc.record_k1(ws, {**data, "received_on": ""}, "Pat")
    with pytest.raises(ValueError, match="numbers"):
        svc.record_k1(ws, {**data, "capital_gains": "lots"}, "Pat")
    k1 = svc.record_k1(ws, data, "Pat")
    assert (k1.status, k1.ordinary_income, k1.taxable_total, k1.distributions) == ("final", Decimal("-12400.00"), Decimal("78750.50"), Decimal("500000.00"))
    assert svc.record_k1(ws, {**data, "status": "estimate"}, "Rae").pk == k1.pk and K1.objects.count() == 1  # updated, not duplicated
    after = svc.expected_k1s(ws, 2025)
    assert [k.received for k in after] == [False, True] and after[1].record.recorded_by == "Rae"  # still-waiting first


@pytest.mark.django_db
def test_partnerships_page(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    from urllib.parse import unquote

    from tests.conftest import context_token

    ws = alder.workspace
    with mock.patch("gl.partnership_views.CampfireClient", return_value=FakeCampfire()), \
            mock.patch("gl.partnership_views.timezone.localdate", return_value=TODAY):
        page = client.get(f"/fo/frame/partnerships?cf_ctx={context_token(customer_id=ws.customer_id, surface='nav', user_id=3)}").content.decode()
        assert "Statement of changes in partners" in page and "Alder 2012 Dynasty Trust" in page and "2,775,000" in page and "$5,600,300" in page
        token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
        issue = client.get("/fo/frame/partnerships", {"fo_t": token, "view": "issue"}).content.decode()
        assert "K-1 figures for 2026" in issue and "Interest income" in issue and "5,000" in issue and "Capital account at year end" in issue
        unlinked = Entity.objects.get(name="Alder Management LLC")
        assert "linked to its books" in client.get("/fo/frame/partnerships", {"fo_t": token, "entity": unlinked.pk}).content.decode()
        receive = client.get("/fo/frame/partnerships", {"fo_t": token, "view": "receive"}).content.decode()
        assert "K-1s expected for 2025" in receive and "Summit Growth Partners IV" in receive and "Not received" in receive
        k = svc.expected_k1s(ws, 2025)[0]
        out = client.post("/fo/frame/partnerships", {"fo_t": token, "action": "k1", "view": "receive", "tax_year": "2025", "account": k.account.pk,
                                                    "instrument": k.instrument.pk, "status": "final", "received_on": "2026-03-20", "capital_gains": "1000"}).content.decode()
        assert "K-1 for 2025: final received." in out and "Final received" in out
