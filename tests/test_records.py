from datetime import date, timedelta
from decimal import Decimal

import pytest
from django import forms as djforms

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Entity, Family, OwnershipInterest
from gl.models import JournalPosting, PostedTransaction
from investments.demo import seed_alder_investments
from investments.models import Account, ImportBatch, Instrument, Transaction, Valuation
from records.forms import scoped
from records.models import AuditEntry
from records.registry import KINDS
from tests.conftest import context_token

TODAY = date(2026, 6, 30)


@pytest.fixture
def alder(workspace: Workspace) -> Family:
    family = seed_alder(workspace)
    seed_alder_investments(family, TODAY)
    return family


@pytest.fixture
def framed(client, alder: Family):  # type: ignore[no-untyped-def]
    """A client inside the Campfire frame, plus a helper that adds the frame token."""
    r = client.get(f"/fo/frame/records?cf_ctx={context_token(customer_id=alder.workspace.customer_id, surface='nav', user_id=9)}")
    assert r.status_code == 200
    from urllib.parse import unquote

    token = unquote(r.content.decode().split("fo_t=")[1].split('"')[0].split("&")[0])

    class F:
        def get(self, path: str):  # type: ignore[no-untyped-def]
            return client.get(path, {"fo_t": token})

        def post(self, path: str, data: dict):  # type: ignore[no-untyped-def,type-arg]
            return client.post(path, {"fo_t": token, **data})

    return F()


def errors(r) -> str:  # type: ignore[no-untyped-def]
    return r.content.decode()


@pytest.mark.django_db
def test_every_record_type_lists_and_opens(framed, alder: Family) -> None:  # type: ignore[no-untyped-def]
    for slug, kind in KINDS.items():
        assert framed.get(f"/fo/frame/records/{slug}").status_code == 200, slug
        assert framed.get(f"/fo/frame/records/{slug}/new").status_code == 200, slug
        first = kind.queryset(alder.workspace).first()
        if first is not None:
            assert framed.get(f"/fo/frame/records/{slug}/{first.pk}").status_code == 200, slug


@pytest.mark.django_db
def test_forms_never_offer_another_workspaces_records(alder: Family) -> None:
    other = Workspace.objects.create(customer_id=999, name="Other")
    seed_alder_investments(seed_alder(other), TODAY)
    for slug, kind in KINDS.items():
        form = kind.form(workspace=alder.workspace)
        for name, field in form.fields.items():
            if isinstance(field, djforms.ModelChoiceField):
                theirs = set(scoped(field.queryset.model, other).values_list("pk", flat=True))
                offered = set(field.queryset.values_list("pk", flat=True))
                assert offered and not offered & theirs, f"{slug}.{name}"
    # And another workspace's record is simply not found.
    their_account = Account.objects.filter(family__workspace=other).first()
    assert their_account is not None


@pytest.mark.django_db
def test_other_workspaces_record_is_not_found(framed, alder: Family) -> None:  # type: ignore[no-untyped-def]
    other = Workspace.objects.create(customer_id=999, name="Other")
    theirs = seed_alder(other).entities.first()
    assert framed.get(f"/fo/frame/records/entities/{theirs.pk}").status_code == 404
    assert framed.get("/fo/frame/records/nonsense").status_code == 404


@pytest.mark.django_db
def test_add_and_edit_are_audited(framed, alder: Family) -> None:  # type: ignore[no-untyped-def]
    r = framed.post("/fo/frame/records/entities/new", {
        "family": alder.pk, "name": "Alder 2026 Education Trust", "entity_type": "irrevocable_trust",
        "tax_classification": "non_grantor_trust", "status": "active",
    })
    assert r.status_code == 302 and "/fo/frame/records/entities?fo_t=" in r["Location"]
    trust = Entity.objects.get(name="Alder 2026 Education Trust")
    r = framed.post(f"/fo/frame/records/entities/{trust.pk}", {
        "name": "Alder 2026 Education Trust", "entity_type": "irrevocable_trust", "tax_classification": "grantor_trust",
        "status": "active", "jurisdiction": "US-SD",
    })
    assert r.status_code == 302
    created, updated = AuditEntry.objects.filter(object_id=str(trust.pk)).order_by("at", "id")
    assert (created.action, updated.action, updated.actor) == ("created", "updated", "Campfire user 9")
    assert updated.changes["tax classification"] == ["Non-grantor trust", "Grantor trust"]
    assert updated.changes["jurisdiction"] == ["", "US-SD"]
    trust.refresh_from_db()
    assert trust.family == alder  # family is locked on edit
    assert "Grantor trust" in errors(framed.get(f"/fo/frame/records/entities/{trust.pk}"))  # history shows


@pytest.mark.django_db
def test_account_needs_exactly_one_owner(framed, alder: Family) -> None:  # type: ignore[no-untyped-def]
    trust = Entity.objects.get(name="Alder 2012 Dynasty Trust")
    base = {"family": alder.pk, "name": "Dynasty Trust - Second Custody", "account_type": "brokerage", "tax_treatment": "taxable",
            "currency": "USD", "tracks_cash": "on", "external_id": "4BX-777001"}
    r = framed.post("/fo/frame/records/accounts/new", base)
    assert r.status_code == 200 and "Choose exactly one owner" in errors(r)
    r = framed.post("/fo/frame/records/accounts/new", {**base, "owner_entity": trust.pk})
    assert r.status_code == 302
    assert Account.objects.get(external_id="4BX-777001").entity == trust


@pytest.mark.django_db
def test_manual_transactions_keep_the_ledger_whole(framed, alder: Family) -> None:  # type: ignore[no-untyped-def]
    account = Account.objects.get(external_id="4BX-104417")
    ustm = Instrument.objects.get(ticker="USTM")
    before = Transaction.objects.count()
    r = framed.post("/fo/frame/records/transactions/new", {
        "account": account.pk, "txn_type": "sell", "instrument": ustm.pk, "trade_date": TODAY.isoformat(),
        "quantity": "99999", "amount": "100", "fees": "0",
    })
    assert r.status_code == 200 and "would break the ledger" in errors(r)
    assert Transaction.objects.count() == before  # undone

    # Deleting the buy a later sale depends on is refused too.
    first_buy = account.transactions.filter(txn_type="buy", instrument=ustm).order_by("trade_date").first()
    r = framed.post(f"/fo/frame/records/transactions/{first_buy.pk}", {"action": "delete"})
    assert "would break the ledger" in errors(r) and Transaction.objects.filter(pk=first_buy.pk).exists()

    # An opening position: transfer in with cost basis and the original purchase date.
    r = framed.post("/fo/frame/records/transactions/new", {
        "account": account.pk, "txn_type": "transfer_in", "instrument": ustm.pk, "trade_date": TODAY.isoformat(),
        "quantity": "100", "amount": "21000", "fees": "0", "acquired_on": (TODAY - timedelta(days=2000)).isoformat(),
    })
    assert r.status_code == 302
    assert Transaction.objects.filter(account=account, txn_type="transfer_in", amount=Decimal("21000")).exists()


@pytest.mark.django_db
def test_posted_and_imported_transactions_are_locked(framed, alder: Family) -> None:  # type: ignore[no-untyped-def]
    t1, t2 = Transaction.objects.filter(account__external_id="4BX-301290", txn_type="interest")[:2]
    posting = JournalPosting.objects.create(workspace=alder.workspace, campfire_entity_id=1, period_end=TODAY, je_date=TODAY,
                                            memo="m", total=1, status=JournalPosting.Status.SUBMITTED)
    PostedTransaction.objects.create(posting=posting, transaction=t1)
    batch = ImportBatch.objects.create(workspace=alder.workspace, source="custodian_file", file_name="nov.csv")
    Transaction.objects.filter(pk=t2.pk).update(batch=batch)

    page = errors(framed.get(f"/fo/frame/records/transactions/{t1.pk}"))
    assert f"Sent to the GL in FO-{posting.pk}" in page and 'value="delete"' not in page
    r = framed.post(f"/fo/frame/records/transactions/{t1.pk}", {"amount": "1", "action": "delete"})
    assert Transaction.objects.filter(pk=t1.pk, amount=t1.amount).exists() and "Sent to the GL" in errors(r)
    assert "Imported from nov.csv" in errors(framed.get(f"/fo/frame/records/transactions/{t2.pk}"))


@pytest.mark.django_db
def test_nav_statement_goes_through_the_anomaly_hold(framed, alder: Family) -> None:  # type: ignore[no-untyped-def]
    acct = Account.objects.get(name="AFI LP – Summit Growth Partners IV")
    summit = Instrument.objects.get(name__startswith="Summit")
    r = framed.post("/fo/frame/records/valuations/new", {
        "account": acct.pk, "instrument": summit.pk, "as_of": TODAY.isoformat(), "value": "47000000", "method": "nav_statement",
    })
    assert r.status_code == 200 and "Held for review" in errors(r)
    assert Valuation.objects.filter(account=acct, as_of=TODAY).get().status == Valuation.Status.HELD
    v = Valuation.objects.get(account=acct, as_of=TODAY)
    assert "aren&#x27;t edited" in errors(framed.get(f"/fo/frame/records/valuations/{v.pk}"))


@pytest.mark.django_db
def test_ownership_loops_and_protected_deletes_are_refused(framed, alder: Family) -> None:  # type: ignore[no-untyped-def]
    existing = OwnershipInterest.objects.filter(owned__family=alder, owner_entity__isnull=False).first()
    a, b = existing.owner_entity, existing.owned  # a owns b; now try b owning a
    before = OwnershipInterest.objects.count()
    r = framed.post("/fo/frame/records/ownership/new", {
        "owned": a.pk, "owner_entity": b.pk, "interest_type": "member", "percentage": "1", "effective_from": "2000-01-01",
    })
    assert r.status_code == 200 and "loop" in errors(r)
    assert OwnershipInterest.objects.count() == before

    ustm = Instrument.objects.get(ticker="USTM")
    r = framed.post(f"/fo/frame/records/securities/{ustm.pk}", {"action": "delete"})
    assert "Can&#x27;t delete" in errors(r) and Instrument.objects.filter(pk=ustm.pk).exists()


@pytest.mark.django_db
def test_records_standalone_with_session(client, alder: Family) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/fo/records").status_code == 302
    session = client.session
    session["workspace_id"] = alder.workspace.pk
    session["campfire_user"] = {"email": "ops@example.test"}
    session.save()
    r = client.post("/fo/records/people/new", {"family": alder.pk, "first_name": "Nora", "last_name": "Alder", "generation": "3"})
    assert r.status_code == 302 and r["Location"] == "/fo/records/people"
    assert AuditEntry.objects.get(kind="people").actor == "ops@example.test"


@pytest.mark.django_db
def test_deletes_never_cascade_through_the_books(framed, alder: Family) -> None:  # type: ignore[no-untyped-def]
    account = Account.objects.get(external_id="4BX-104417")
    count = account.transactions.count()
    page = errors(framed.get(f"/fo/frame/records/accounts/{account.pk}"))
    assert "Not deletable" in page and "Set &#x27;Closed on&#x27; instead" in page and 'value="delete"' not in page
    r = framed.post(f"/fo/frame/records/accounts/{account.pk}", {"action": "delete"})
    assert "Can&#x27;t delete" in errors(r) and Account.objects.get(pk=account.pk).transactions.count() == count
    r = framed.post(f"/fo/frame/records/families/{alder.pk}", {"action": "delete"})
    assert Family.objects.filter(pk=alder.pk).exists() and "inactive" in errors(r)

    empty = Account.objects.create(family=alder, name="Empty", account_type="brokerage", entity=account.entity)
    assert framed.post(f"/fo/frame/records/accounts/{empty.pk}", {"action": "delete"}).status_code == 302
    assert not Account.objects.filter(pk=empty.pk).exists()
    assert AuditEntry.objects.filter(kind="accounts", action="deleted", object_repr="Empty").exists()
