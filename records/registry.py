"""The record types the Records screens manage: how each is listed, searched, locked, saved and checked.

Adding a type is one `Kind` entry plus its form. Locks follow the accounting rules: a transaction already sent to
the GL, imported from a custodian file, or booked by a paid capital call is corrected with an adjusting entry
(or at the source), never edited in place.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import Max, Model, QuerySet
from django.utils import timezone

from campfire_integration.models import Workspace
from entities.models import Branch, Entity, EntityRole, Family, OwnershipInterest, Person
from entities.services.ownership import OwnershipCycleError, effective_ownership
from gl.models import PostedTransaction
from investments.models import Account, AssetClass, CapitalCall, Commitment, Instrument, Price, Transaction, Valuation
from investments.services.ledger import LedgerError, replay
from investments.services.valuation import record_valuation
from records import forms as f


@dataclass(frozen=True)
class Kind:
    slug: str
    label: str
    plural: str
    group: str
    model: type[Model]
    form: type[f.WorkspaceForm]
    queryset: Callable[[Workspace], QuerySet]  # type: ignore[type-arg]
    columns: tuple[tuple[str, Callable[[Any], Any]], ...]
    search: tuple[str, ...] = ()
    family_path: str | None = None  # for the family filter
    help: str = ""
    editable: bool = True  # False: add and delete only
    locked: Callable[[Any], str] = lambda obj: ""  # why the record can't be changed or deleted ("" = it can)
    save: Callable[[f.WorkspaceForm], tuple[Model, str]] | None = None  # custom save; returns (object, note)
    check: Callable[[Any], None] | None = None  # after a save or delete, inside the transaction; raise to undo
    # Why it can't be deleted although it can be edited ("" = it can). Guards against database cascades.
    undeletable: Callable[[Any], str] = lambda obj: ""
    defaults: dict[str, Any] = field(default_factory=dict)


def _d(value: date | None) -> str:
    return value.isoformat() if value else ""


# ------------------------------------------------------------------------------------------------- locks


def transaction_locked(t: Transaction) -> str:
    item = PostedTransaction.objects.filter(transaction=t).select_related("posting").first()
    if item is not None:
        p = item.posting
        return (f"Sent to the GL in {p.ref_number} ({p.get_status_display().lower()}). To change it, release that entry on the "
                "General ledger page if it is still awaiting approval, or record an adjusting transaction.")
    if t.batch_id and t.batch.source == "custodian_file":
        return (f"Imported from {t.batch.file_name or 'a custodian file'}. Correct it at the custodian (the next file brings the fix) "
                "or record an adjusting transaction.")
    if CapitalCall.objects.filter(contribution=t).exists():
        return "Booked by a paid capital call."
    return ""


def price_locked(p: Price) -> str:
    return f"Imported from {p.batch.file_name or 'a price file'}." if p.batch_id else ""


def call_locked(c: CapitalCall) -> str:
    if c.status in (CapitalCall.Status.RECEIVED, CapitalCall.Status.APPROVED):
        return ""
    return f"This call is {c.get_status_display().lower()}; its bill and payment live in Campfire."


def family_undeletable(fam: Family) -> str:
    if fam.entities.exists() or fam.people.exists() or fam.accounts.exists():
        return "It still has people, entities or accounts. Mark it inactive instead."
    return ""


def account_undeletable(a: Account) -> str:
    n = a.transactions.count() + a.valuations.count()
    if n or hasattr(a, "commitment"):
        return f"It has {n} transactions and marks{' and a commitment' if hasattr(a, 'commitment') else ''}. Set 'Closed on' instead."
    return ""


def commitment_undeletable(c: Commitment) -> str:
    return "It has capital calls." if c.calls.exists() else ""


# ---------------------------------------------------------------------------------------- after-save checks


def check_ledger(t: Transaction | Account) -> None:
    """Every account's history must still replay: no selling or transferring out more than is held."""
    accounts = [t] if isinstance(t, Account) else [t.account]
    for account in accounts:
        last = account.transactions.aggregate(d=Max("trade_date"))["d"]
        if last is None:
            continue
        try:
            replay(account, last)
        except LedgerError as e:
            raise ValidationError(f"That would break the ledger: {e}") from e


def check_ownership(o: OwnershipInterest) -> None:
    try:
        effective_ownership(o.owned.family, timezone.localdate())
    except OwnershipCycleError as e:
        raise ValidationError(f"That ownership creates a loop: {e}") from e


def save_valuation(form: f.WorkspaceForm) -> tuple[Model, str]:
    d = form.cleaned_data
    v = record_valuation(account=d["account"], instrument=d["instrument"], as_of=d["as_of"], value=d["value"],
                         method=d["method"], source_ref=d.get("source_ref") or "")
    if v.status == Valuation.Status.HELD:
        return v, f"Held for review: {v.change_pct}% from the expected value. It isn't used until someone accepts it."
    return v, ""


# ------------------------------------------------------------------------------------------------- kinds

FAMILY, INVEST = "Family & structure", "Investments"

KINDS: dict[str, Kind] = {k.slug: k for k in [
    Kind("families", "Family", "Families", FAMILY, Family, f.FamilyForm,
         lambda ws: Family.objects.filter(workspace=ws),
         (("Name", str), ("Code", lambda o: o.code), ("Active", lambda o: "Yes" if o.is_active else "No")),
         search=("name", "code"), undeletable=family_undeletable,
         help="A single-family office has one; a multi-family office has one per client family."),
    Kind("branches", "Branch", "Branches", FAMILY, Branch, f.BranchForm,
         lambda ws: Branch.objects.filter(family__workspace=ws).select_related("family", "parent"),
         (("Name", str), ("Family", lambda o: o.family), ("Part of", lambda o: o.parent or "")),
         search=("name",), family_path="family", help="Households and generations, e.g. G1 → Branch A → household."),
    Kind("people", "Person", "People", FAMILY, Person, f.PersonForm,
         lambda ws: Person.objects.filter(family__workspace=ws).select_related("family", "branch"),
         (("Name", lambda o: o.display_name), ("Family", lambda o: o.family), ("Branch", lambda o: o.branch or ""),
          ("Generation", lambda o: o.generation or ""), ("Principal", lambda o: "Yes" if o.is_principal else "")),
         search=("first_name", "last_name", "preferred_name"), family_path="family"),
    Kind("entities", "Entity", "Entities", FAMILY, Entity, f.EntityForm,
         lambda ws: Entity.objects.filter(family__workspace=ws).select_related("family"),
         (("Name", str), ("Family", lambda o: o.family), ("Type", lambda o: o.get_entity_type_display()),
          ("Tax", lambda o: o.get_tax_classification_display()), ("Status", lambda o: o.get_status_display()),
          ("In Campfire", lambda o: "Linked" if o.campfire_entity_id else "")),
         search=("name", "short_name"), family_path="family",
         help="Trusts, partnerships, LLCs, foundations. Link them to Campfire entities on the General ledger page."),
    Kind("ownership", "Ownership interest", "Ownership", FAMILY, OwnershipInterest, f.OwnershipForm,
         lambda ws: OwnershipInterest.objects.filter(owned__family__workspace=ws).select_related("owned", "owner_entity", "owner_person"),
         (("Entity owned", lambda o: o.owned), ("Owner", lambda o: o.owner), ("%", lambda o: o.percentage.normalize()),
          ("Type", lambda o: o.get_interest_type_display()), ("From", lambda o: _d(o.effective_from)), ("To", lambda o: _d(o.effective_to))),
         search=("owned__name",), family_path="owned__family", check=check_ownership,
         help="Who owns what, with dates. Look-through ownership and net worth by person follow from it."),
    Kind("roles", "Role", "Roles", FAMILY, EntityRole, f.RoleForm,
         lambda ws: EntityRole.objects.filter(entity__family__workspace=ws).select_related("entity", "person"),
         (("Entity", lambda o: o.entity), ("Role", lambda o: o.get_role_display()), ("Held by", lambda o: o.holder_name)),
         search=("entity__name", "external_name"), family_path="entity__family", help="Trustees, managers, protectors, signers."),
    Kind("asset-classes", "Asset class", "Asset classes", INVEST, AssetClass, f.AssetClassForm,
         lambda ws: AssetClass.objects.filter(workspace=ws).select_related("parent"),
         (("Name", str), ("Code", lambda o: o.code), ("Part of", lambda o: o.parent or ""), ("Liquidity", lambda o: o.get_liquidity_display())),
         search=("name", "code"), help="Your taxonomy. It becomes the FO Asset Class tag in Campfire."),
    Kind("securities", "Security / fund", "Securities & funds", INVEST, Instrument, f.InstrumentForm,
         lambda ws: Instrument.objects.filter(workspace=ws).select_related("asset_class"),
         (("Name", str), ("Ticker", lambda o: o.ticker), ("Type", lambda o: o.get_kind_display()), ("Asset class", lambda o: o.asset_class)),
         search=("name", "ticker", "cusip"), help="Listed securities, fund interests, property, notes, art."),
    Kind("accounts", "Account", "Accounts", INVEST, Account, f.AccountForm,
         lambda ws: Account.objects.filter(family__workspace=ws).select_related("family", "entity", "person"),
         (("Name", str), ("Owner", lambda o: o.owner), ("Type", lambda o: o.get_account_type_display()),
          ("Tax", lambda o: o.get_tax_treatment_display()), ("Custodian account", lambda o: o.external_id)),
         search=("name", "external_id", "custodian"), family_path="family", undeletable=account_undeletable,
         help="Custody accounts, fund interests, property. Imports match custodian files on the custodian account number."),
    Kind("commitments", "Commitment", "Commitments", INVEST, Commitment, f.CommitmentForm,
         lambda ws: Commitment.objects.filter(account__family__workspace=ws).select_related("account", "instrument"),
         (("Fund", lambda o: o.instrument), ("Account", lambda o: o.account), ("Amount", lambda o: o.amount), ("Date", lambda o: _d(o.commitment_date))),
         search=("instrument__name", "account__name"), family_path="account__family", undeletable=commitment_undeletable),
    Kind("capital-calls", "Capital call", "Capital calls", INVEST, CapitalCall, f.CapitalCallForm,
         lambda ws: CapitalCall.objects.filter(commitment__account__family__workspace=ws).select_related("commitment__instrument"),
         (("Fund", lambda o: o.commitment.instrument), ("Amount", lambda o: o.amount), ("Due", lambda o: _d(o.due_date)),
          ("Status", lambda o: o.get_status_display())),
         search=("commitment__instrument__name", "source_ref"), family_path="commitment__account__family", locked=call_locked),
    Kind("transactions", "Transaction", "Transactions", INVEST, Transaction, f.TransactionForm,
         lambda ws: Transaction.objects.filter(account__family__workspace=ws).select_related("account", "instrument", "batch").order_by("-trade_date", "-id"),
         (("Date", lambda o: _d(o.trade_date)), ("Account", lambda o: o.account), ("Type", lambda o: o.get_txn_type_display()),
          ("Security", lambda o: o.instrument or ""), ("Quantity", lambda o: o.quantity.normalize() if o.quantity else ""), ("Amount", lambda o: o.amount),
          ("Source", lambda o: o.batch.source if o.batch_id else "manual")),
         search=("account__name", "instrument__name", "instrument__ticker", "description"), family_path="account__family",
         locked=transaction_locked, check=check_ledger,
         help="Manual entries, including opening positions: enter them as transfers in with cost basis and the original purchase date."),
    Kind("valuations", "NAV statement / appraisal", "NAV statements & appraisals", INVEST, Valuation, f.ValuationForm,
         lambda ws: Valuation.objects.filter(account__family__workspace=ws).select_related("account", "instrument").order_by("-as_of", "-id"),
         (("Date", lambda o: _d(o.as_of)), ("Holding", lambda o: o.instrument), ("Account", lambda o: o.account), ("Value", lambda o: o.value),
          ("Method", lambda o: o.get_method_display()), ("Status", lambda o: o.get_status_display())),
         search=("instrument__name", "account__name"), family_path="account__family", editable=False, save=save_valuation,
         help="Marks for funds and other holdings without a market price. To correct one, delete it and add the right one."),
    Kind("prices", "Price", "Prices", INVEST, Price, f.PriceForm,
         lambda ws: Price.objects.filter(instrument__workspace=ws).select_related("instrument", "batch").order_by("-as_of", "instrument__name"),
         (("Date", lambda o: _d(o.as_of)), ("Security", lambda o: o.instrument), ("Price", lambda o: o.price.normalize()), ("Source", lambda o: o.source)),
         search=("instrument__name", "instrument__ticker"), locked=price_locked, help="Manual prices; price files load them in bulk."),
]}

GROUPS = [FAMILY, INVEST]
