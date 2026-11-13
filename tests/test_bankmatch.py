from datetime import date, timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import unquote

import pytest
from django.utils import timezone

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Family
from gl.services import bankmatch
from investments.demo import seed_alder_investments
from investments.models import CapitalCall, Commitment, Instrument, Transaction
from tests.conftest import context_token
from tests.test_calls import FakeCampfire

TODAY = timezone.localdate()


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    Commitment.objects.filter(instrument__name__startswith="Summit").update(campfire_vendor_id=501, campfire_vendor_name="Summit Growth Partners")
    Commitment.objects.filter(instrument__name__startswith="Crescent").update(campfire_vendor_id=502, campfire_vendor_name="Crescent Ventures")
    return family


def line(i: int, amount: float, merchant: str, days_ago: int = 1, description: str | None = None) -> dict:  # type: ignore[type-arg]
    """A bank-feed line as Campfire returns it (posted_at, signed amount: in > 0, out < 0)."""
    return {"id": i, "posted_at": (TODAY - timedelta(days=days_ago)).isoformat(), "amount": amount, "merchant_name": merchant,
            "bank_description": description, "external_memo": None, "account_name": "AFI LP Operating", "entity_name": "Alder Family Investments LP",
            "is_deleted": False, "excluded": False}


def test_fund_names_on_wires() -> None:
    summit = Commitment(instrument=Instrument(name="Summit Growth Partners IV, L.P.", sponsor="Summit Growth Partners"), campfire_vendor_name="")
    assert bankmatch.mentions(summit, "WIRE OUT SUMMIT GROWTH PARTNERS IV CAPITAL CALL")
    assert bankmatch.mentions(summit, "Summit Growth")
    assert not bankmatch.mentions(summit, "SUMMIT BANK MORTGAGE PAYMENT")
    assert not bankmatch.mentions(summit, "AMAZON")


@pytest.mark.django_db
def test_suggestions_and_actions(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    ws = alder.workspace
    cf = FakeCampfire()
    cf.bank_lines = [
        line(1, -500000.0, "Summit Growth Partners", description="WIRE OUT SUMMIT GROWTH PARTNERS IV CAPITAL CALL"),
        line(2, 125000.0, "Crescent Ventures", description="WIRE IN CRESCENT VENTURE FUND II DISTRIBUTION"),
        line(3, -89.99, "AMAZON"),
        line(4, -500000.0, "Summit Growth Partners", days_ago=400),  # too old
    ]
    found = {s.line["id"]: s for s in bankmatch.suggestions(ws, cf)}  # type: ignore[arg-type]
    assert set(found) == {1, 2}
    assert (found[1].kind, found[1].call.amount) == ("call_paid", Decimal("500000.00"))
    assert (found[2].kind, str(found[2].commitment.instrument)) == ("distribution", "Crescent Venture Fund II, L.P.")

    with mock.patch("gl.call_views.CampfireClient", return_value=cf):
        r = client.get(f"/fo/frame/calls?cf_ctx={context_token(customer_id=ws.customer_id, surface='nav', user_id=4)}")
        page = r.content.decode()
        assert "From the bank feed" in page and "Mark the call paid" in page and "A distribution from" in page
        token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
        post = lambda data: client.post("/fo/frame/calls", {"fo_t": token, **data}).content.decode()  # noqa: E731

        call = CapitalCall.objects.get()
        assert "call marked paid" in post({"action": "bank_paid", "line": 1, "call": call.pk})
        call.refresh_from_db()
        assert call.status == CapitalCall.Status.PAID and call.paid_on == TODAY - timedelta(days=1)
        assert call.contribution.source_ref == "campfire-bank:1"

        crescent = Commitment.objects.get(instrument__name__startswith="Crescent")
        assert "add up to 100,000.00" in post({"action": "bank_distribution", "line": 2, "commitment": crescent.pk, "roc": "100000"})
        assert "Recorded the Crescent" in post({"action": "bank_distribution", "line": 2, "commitment": crescent.pk, "roc": "100000", "income": "25000"})
        made = Transaction.objects.filter(account=crescent.account, source_ref__startswith="campfire-bank:2")
        assert sorted(t.txn_type for t in made) == ["distribution_income", "distribution_roc"]

        # Used lines aren't suggested again; a dismissed one disappears too.
        assert bankmatch.suggestions(ws, cf) == []  # type: ignore[arg-type]
        cf.bank_lines.append(line(5, 7000.0, "Crescent Ventures"))
        assert [s.line["id"] for s in bankmatch.suggestions(ws, cf)] == [5]  # type: ignore[arg-type]
        post({"action": "bank_dismiss", "line": 5})
        assert bankmatch.suggestions(ws, cf) == []  # type: ignore[arg-type]


@pytest.mark.django_db
def test_a_call_with_a_bill_is_paid_in_campfire(alder: Family) -> None:
    CapitalCall.objects.update(status=CapitalCall.Status.BILLED, campfire_bill_id=7001)
    cf = FakeCampfire()
    cf.bank_lines = [line(1, -500000.0, "Summit Growth Partners")]
    (s,) = bankmatch.suggestions(alder.workspace, cf)  # type: ignore[arg-type]
    assert s.kind == "pay_bill_in_campfire"
    with pytest.raises(ValueError, match="Campfire bill"):
        bankmatch.mark_paid_from_line(alder.workspace, s.call.pk, s.line)  # type: ignore[union-attr]
