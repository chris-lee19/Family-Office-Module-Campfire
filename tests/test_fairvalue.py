"""Market value in the GL: month-end entries that bring unrealized appreciation to market value less cost."""

from datetime import date
from decimal import Decimal

import pytest

from entities.models import Family
from gl.models import AccountMapping, FairValueItem, GLSettings, JournalPosting, Role
from gl.services import fairvalue
from gl.services.posting import build_preview, release, send
from gl.services.tieout import tie_out
from investments.models import Account, Transaction
from tests.test_gl import CF_ENTITY, GL, TODAY, FakeClient, _response, alder  # noqa: F401  (alder is a fixture)

APRIL, MAY, JUNE, JULY = date(2026, 4, 30), date(2026, 5, 31), date(2026, 6, 30), date(2026, 7, 31)
LP = CF_ENTITY["Alder Family Investments LP"]
DYNASTY = CF_ENTITY["Alder 2012 Dynasty Trust"]
UNREALIZED, GAIN = GL[Role.UNREALIZED][0], GL[Role.UNREALIZED_GAIN][0]


def market(preview, entity: int | None = None):  # type: ignore[no-untyped-def]
    return [j for j in preview.journals if j.is_fair_value and entity in (None, j.campfire_entity_id)]


def unrealized(entity: int, as_of: date) -> Decimal:
    total = Decimal(0)
    for account in Account.objects.filter(entity__campfire_entity_id=entity):
        total += sum((value - cost for _, cost, value in fairvalue.unrealized_at(account, as_of).values()), Decimal(0))
    return total


def book_at_market(family: Family, start: date = date(2026, 4, 15)) -> None:
    gl = GLSettings.for_workspace(family.workspace)
    gl.fair_value_from = start
    gl.save()


def amount(j, account: int) -> Decimal:  # type: ignore[no-untyped-def]
    return sum((ln.amount for ln in j.lines if ln.campfire_account_id == account), Decimal(0))


@pytest.mark.django_db
def test_books_stay_at_cost_until_switched_on(alder: Family) -> None:  # noqa: F811
    assert market(build_preview(alder.workspace, through=TODAY)) == []
    assert fairvalue.balances(alder.workspace, TODAY) == []


@pytest.mark.django_db
def test_first_entry_brings_the_books_to_market_then_each_month_books_the_change(alder: Family) -> None:  # noqa: F811
    ws = alder.workspace
    book_at_market(alder)
    preview = build_preview(ws, through=TODAY)
    april, may = market(preview, LP)  # the funds' marks don't move in June: no entry for a month with no change
    assert (april.period_end, april.je_date, may.period_end) == (APRIL, APRIL, MAY) and april.ok and may.ok
    at_april, at_may = unrealized(LP, APRIL), unrealized(LP, MAY)
    assert at_april != 0 and at_may != at_april and unrealized(LP, JUNE) == at_may
    # April: the whole unrealized amount to date. Debit the balance sheet account, credit the P&L account.
    assert amount(april, UNREALIZED) == at_april and amount(april, GAIN) == -at_april
    assert sum(v.adjustment for v in april.fair_values) == at_april and all(v.unrealized == v.market_value - v.cost_basis for v in april.fair_values)
    assert "market value adjustment, April 2026" in april.memo
    # May: only what changed since April.
    assert amount(may, UNREALIZED) == at_may - at_april
    # Within a month the market value entry comes after the activity entries.
    months = [(j.period_end, j.is_fair_value) for j in preview.journals]
    assert months == sorted(months)

    client = FakeClient()
    sent = send(ws, through=TODAY, client=client, created_by="Pat")  # type: ignore[arg-type]
    fair = [p for p in sent if p.kind == JournalPosting.Kind.FAIR_VALUE]
    assert len(fair) == len(market(preview)) and all(p.status == JournalPosting.Status.SUBMITTED and not p.transaction_ids for p in fair)
    body = next(c["json"] for c in client.sent if c["json"]["source_id"] == next(p for p in fair if p.campfire_entity_id == LP and p.period_end == APRIL).source_id)
    assert body["date"] == APRIL.isoformat() and {t["account"] for t in body["transactions"]} == {str(UNREALIZED), str(GAIN)}
    assert sum(i.adjustment for i in FairValueItem.objects.filter(posting__campfire_entity_id=LP)) == at_may
    assert market(build_preview(ws, through=TODAY)) == []  # nothing left to book


@pytest.mark.django_db
def test_released_entry_is_booked_again(alder: Family) -> None:  # noqa: F811
    ws = alder.workspace
    book_at_market(alder)
    send(ws, through=TODAY, client=FakeClient())  # type: ignore[arg-type]
    may = JournalPosting.objects.get(kind=JournalPosting.Kind.FAIR_VALUE, campfire_entity_id=LP, period_end=MAY)
    release(may, "Rejected in Campfire")
    again = market(build_preview(ws, through=TODAY), LP)
    assert [j.period_end for j in again] == [MAY] and amount(again[0], UNREALIZED) == unrealized(LP, MAY) - unrealized(LP, APRIL)


@pytest.mark.django_db
def test_sold_holding_comes_back_out(alder: Family) -> None:  # noqa: F811
    ws = alder.workspace
    book_at_market(alder)
    send(ws, through=TODAY, client=FakeClient())  # type: ignore[arg-type]
    item = FairValueItem.objects.filter(posting__campfire_entity_id=DYNASTY, instrument__name="US Total Market Index ETF").order_by("-posting__period_end").first()
    assert item is not None and item.unrealized > 0
    booked = sum(i.adjustment for i in FairValueItem.objects.filter(account=item.account, instrument=item.instrument))
    held = fairvalue.replay(item.account, TODAY).quantity(item.instrument.pk)
    Transaction.objects.create(account=item.account, instrument=item.instrument, txn_type=Transaction.Type.SELL, trade_date=date(2026, 7, 10),
                               quantity=held, amount=item.market_value)
    july = next(j for j in market(build_preview(ws, through=JULY), DYNASTY) if j.period_end == JULY)
    line = next(v for v in july.fair_values if v.instrument == item.instrument)
    # The unrealized amount in the GL is reversed; the sale's own entry books the gain as realized.
    assert (line.unrealized, line.adjustment) == (Decimal(0), -booked)


@pytest.mark.django_db
def test_held_until_the_roles_are_mapped_and_months_go_in_order(alder: Family) -> None:  # noqa: F811
    ws = alder.workspace
    book_at_market(alder)
    AccountMapping.objects.filter(workspace=ws, role=Role.UNREALIZED_GAIN).delete()
    april, may = market(build_preview(ws, through=TODAY), LP)
    assert not april.ok and any("Unrealized gain / loss (P&L)" in e for e in april.errors)
    assert not may.ok and any("Send the April 2026 market value entry first" in e for e in may.errors)
    sent = send(ws, through=TODAY, client=FakeClient())  # type: ignore[arg-type]
    assert sent and all(p.kind == JournalPosting.Kind.ACTIVITY for p in sent)

    # A month Campfire refuses stops that entity's later months: they build on it.
    AccountMapping.objects.create(workspace=ws, role=Role.UNREALIZED_GAIN, campfire_account_id=GAIN, campfire_account_label="7300")
    refused = send(ws, through=TODAY, client=FakeClient(lambda body: _response(400, {"detail": "Period is locked"})))  # type: ignore[arg-type]
    assert {p.period_end for p in refused if p.campfire_entity_id == LP} == {APRIL} and not FairValueItem.objects.exists()


@pytest.mark.django_db
def test_tie_out_covers_unrealized_appreciation(alder: Family) -> None:  # noqa: F811
    ws = alder.workspace
    book_at_market(alder)
    send(ws, through=TODAY, client=FakeClient())  # type: ignore[arg-type]  # everything awaits approval
    client = FakeClient(gets={"/ca/api/get_trial_balance": {"trial_balance": {"accounts": []}}})
    rows = [r for r in tie_out(ws, TODAY, client).rows if r.campfire_account_id == UNREALIZED]  # type: ignore[arg-type]
    lp = next(r for r in rows if r.campfire_entity_id == LP)
    assert lp.fo_balance == unrealized(LP, JUNE) and lp.awaiting_approval == lp.fo_balance and lp.ties
    # Mid-month the GL still holds the last month end's amount, and that is what it is compared with.
    mid = next(r for r in tie_out(ws, date(2026, 7, 15), client).rows if r.campfire_account_id == UNREALIZED and r.campfire_entity_id == LP)  # type: ignore[arg-type]
    assert mid.fo_balance == lp.fo_balance and mid.ties


@pytest.mark.django_db
def test_setup_control_on_the_gl_page(client, alder: Family) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from unittest import mock
    from urllib.parse import unquote

    from tests.conftest import context_token

    ws = alder.workspace
    cf = FakeClient(gets={"/coa/api/account": {"results": [], "next": None}, "/coa/api/entity": {"results": [], "next": None}})
    with mock.patch("gl.views.CampfireClient", return_value=cf):
        page = client.get(f"/fo/frame/gl?cf_ctx={context_token(customer_id=ws.customer_id, surface='nav', user_id=3)}").content.decode()
        assert "Book market value from" in page and "the books stay at cost" in page
        token = unquote(page.split('name="fo_t" value="')[1].split('"')[0])
        out = client.post("/fo/frame/gl", {"fo_t": token, "action": "fair_value_from", "fair_value_from": "2026-04-15"}).content.decode()
    assert "booked at market value from April 2026" in out and "Market value" in out and "Holdings" in out
    assert GLSettings.for_workspace(ws).fair_value_from == date(2026, 4, 15)
