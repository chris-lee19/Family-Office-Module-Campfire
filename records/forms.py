"""Forms for the Records screens. Every choice list is limited to the workspace (`scoped`), which is what keeps
one Campfire workspace's records out of another's forms."""

from typing import Any

from django import forms
from django.db.models import Model, QuerySet

from campfire_integration.models import Workspace
from entities.models import Branch, Entity, EntityRole, Family, OwnershipInterest, Person
from investments.models import Account, AssetClass, CapitalCall, Commitment, Instrument, Price, Transaction, Valuation


def scoped(model: type[Model], workspace: Workspace) -> QuerySet:  # type: ignore[type-arg]
    """The rows of `model` that belong to the workspace. Unknown models get nothing (fail closed)."""
    paths = {
        Family: "workspace", AssetClass: "workspace", Instrument: "workspace",
        Branch: "family__workspace", Person: "family__workspace", Entity: "family__workspace", Account: "family__workspace",
        Commitment: "account__family__workspace",
    }
    path = paths.get(model)
    if path is None:
        return model.objects.none()
    return model.objects.filter(**{path: workspace})


class WorkspaceForm(forms.ModelForm):
    """Scopes every choice to the workspace, renders dates as date pickers, and sets `workspace` on new rows."""

    def __init__(self, *args: Any, workspace: Workspace, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.workspace = workspace
        if hasattr(self.instance, "workspace_id") and self.instance.workspace_id is None:
            self.instance.workspace = workspace
        for field in self.fields.values():
            if isinstance(field, forms.ModelChoiceField):
                field.queryset = scoped(field.queryset.model, workspace)
            elif isinstance(field, forms.DateField):
                field.widget = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
        self.setup()

    def setup(self) -> None:
        """Per-form adjustments (labels, locked fields)."""

    def lock(self, *names: str, reason: str = "") -> None:
        for name in names:
            if name in self.fields:
                self.fields[name].disabled = True
                if reason:
                    self.fields[name].help_text = reason

    @property
    def is_new(self) -> bool:
        return self.instance.pk is None


def _one_of(form: forms.ModelForm, a: str, b: str, what: str) -> None:
    data = form.cleaned_data
    if bool(data.get(a)) == bool(data.get(b)):
        raise forms.ValidationError(f"Choose exactly one {what}.")


# ------------------------------------------------------------------------------------------------- family


class FamilyForm(WorkspaceForm):
    class Meta:
        model = Family
        fields = ["name", "code", "is_active"]
        help_texts = {"code": "Short code, used in reports and as a Campfire tag value, e.g. 'alder'."}


class BranchForm(WorkspaceForm):
    class Meta:
        model = Branch
        fields = ["family", "parent", "name", "sort_order"]
        labels = {"parent": "Part of branch"}

    def setup(self) -> None:
        if not self.is_new:
            self.lock("family", reason="A branch stays in its family.")


class PersonForm(WorkspaceForm):
    class Meta:
        model = Person
        fields = ["family", "branch", "first_name", "last_name", "preferred_name", "birth_year", "generation", "is_principal", "email"]
        help_texts = {"birth_year": "Year only.", "generation": "1 = founding generation."}

    def setup(self) -> None:
        if not self.is_new:
            self.lock("family", reason="A person stays in their family.")


class EntityForm(WorkspaceForm):
    class Meta:
        model = Entity
        fields = ["family", "branch", "name", "short_name", "entity_type", "tax_classification", "jurisdiction",
                  "formation_date", "termination_date", "status", "tax_id_last4", "notes"]
        labels = {"tax_id_last4": "Tax ID (last 4)"}
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def setup(self) -> None:
        if not self.is_new:
            self.lock("family", reason="An entity stays in its family.")


class OwnershipForm(WorkspaceForm):
    class Meta:
        model = OwnershipInterest
        fields = ["owned", "owner_entity", "owner_person", "interest_type", "percentage", "effective_from", "effective_to"]
        labels = {"owned": "Entity owned", "owner_entity": "Owner (entity)", "owner_person": "Owner (person)", "percentage": "Percentage (%)"}

    def clean(self) -> dict[str, Any]:
        data = super().clean()
        _one_of(self, "owner_entity", "owner_person", "owner: an entity or a person")
        return data


class RoleForm(WorkspaceForm):
    class Meta:
        model = EntityRole
        fields = ["entity", "role", "person", "external_name", "effective_from", "effective_to"]
        labels = {"person": "Holder (family member)", "external_name": "Holder (outside party)"}

    def clean(self) -> dict[str, Any]:
        data = super().clean()
        _one_of(self, "person", "external_name", "holder: a family member or an outside party")
        return data


# ----------------------------------------------------------------------------------------- investments


class AssetClassForm(WorkspaceForm):
    class Meta:
        model = AssetClass
        fields = ["name", "code", "parent", "liquidity", "sort_order"]
        labels = {"parent": "Part of asset class"}

    def clean_parent(self) -> AssetClass | None:
        parent = self.cleaned_data.get("parent")
        node = parent
        while node is not None:
            if node.pk == self.instance.pk:
                raise forms.ValidationError("An asset class can't sit under itself.")
            node = node.parent
        return parent


class InstrumentForm(WorkspaceForm):
    class Meta:
        model = Instrument
        fields = ["name", "kind", "asset_class", "ticker", "cusip", "isin", "currency", "sponsor", "vintage_year"]
        labels = {"kind": "Type", "sponsor": "Manager / GP / issuer"}

    def setup(self) -> None:
        if not self.is_new and self.instance.transactions.exists():
            self.lock("kind", "currency", reason="Fixed once the security has transactions.")

    def clean(self) -> dict[str, Any]:
        data = super().clean()
        kind = data.get("kind") or self.instance.kind
        self.instance.is_unitized = kind in Instrument.UNITIZED_KINDS
        return data


class AccountForm(WorkspaceForm):
    owner_entity = forms.ModelChoiceField(queryset=Entity.objects.none(), required=False, label="Owner (entity)")
    owner_person = forms.ModelChoiceField(queryset=Person.objects.none(), required=False, label="Owner (person)")

    class Meta:
        model = Account
        fields = ["family", "name", "owner_entity", "owner_person", "account_type", "tax_treatment", "custodian", "external_id",
                  "account_number_last4", "currency", "tracks_cash", "opened_on", "closed_on"]
        labels = {"external_id": "Custodian account number", "account_number_last4": "Account number (last 4)",
                  "tracks_cash": "Holds cash (custody account)"}
        help_texts = {"external_id": "As it appears in the custodian's files; imports match on it.",
                      "tracks_cash": "Off for fund interests and property: their cash moves through a bank account in Campfire."}

    def setup(self) -> None:
        self.fields["owner_entity"].queryset = scoped(Entity, self.workspace)
        self.fields["owner_person"].queryset = scoped(Person, self.workspace)
        if not self.is_new:
            self.fields["owner_entity"].initial = self.instance.entity_id
            self.fields["owner_person"].initial = self.instance.person_id
            self.lock("family", reason="An account stays in its family.")
            if self.instance.transactions.filter(gl_item__isnull=False).exists():
                self.lock("owner_entity", "owner_person", "currency", reason="Fixed once activity has been sent to the GL.")
        self.order_fields(self.Meta.fields)

    def clean(self) -> dict[str, Any]:
        data = super().clean()
        _one_of(self, "owner_entity", "owner_person", "owner: an entity or a person")
        self.instance.entity = data.get("owner_entity")
        self.instance.person = data.get("owner_person")
        return data


class CommitmentForm(WorkspaceForm):
    class Meta:
        model = Commitment
        fields = ["account", "instrument", "amount", "commitment_date", "currency"]
        labels = {"account": "Account (the fund interest)", "instrument": "Fund", "amount": "Commitment amount"}

    def setup(self) -> None:
        free = scoped(Account, self.workspace).filter(commitment__isnull=True)
        if not self.is_new:
            free = free | scoped(Account, self.workspace).filter(pk=self.instance.account_id)
        self.fields["account"].queryset = free
        self.fields["instrument"].queryset = scoped(Instrument, self.workspace).filter(is_unitized=False)


class CapitalCallForm(WorkspaceForm):
    class Meta:
        model = CapitalCall
        fields = ["commitment", "notice_date", "due_date", "amount", "source_ref"]
        labels = {"source_ref": "Notice reference"}

    def clean(self) -> dict[str, Any]:
        data = super().clean()
        if data.get("notice_date") and data.get("due_date") and data["due_date"] < data["notice_date"]:
            raise forms.ValidationError("The due date is before the notice date.")
        return data


class TransactionForm(WorkspaceForm):
    class Meta:
        model = Transaction
        fields = ["account", "txn_type", "instrument", "trade_date", "settle_date", "quantity", "amount", "fees",
                  "acquired_on", "is_recallable", "description"]
        labels = {"txn_type": "Type", "instrument": "Security / fund", "amount": "Amount (gross, positive)",
                  "acquired_on": "Originally acquired on", "is_recallable": "Recallable distribution"}
        help_texts = {"quantity": "Units, positive. Required for listed securities.",
                      "acquired_on": "Securities received by transfer: the original purchase date (holding period).",
                      "amount": "Buy: cost; sell: gross proceeds; transfer in: cost basis. Direction comes from the type."}

    def clean(self) -> dict[str, Any]:
        data = super().clean()
        if data.get("settle_date") and data.get("trade_date") and data["settle_date"] < data["trade_date"]:
            raise forms.ValidationError("The settlement date is before the trade date.")
        return data


class ValuationForm(WorkspaceForm):
    class Meta:
        model = Valuation
        fields = ["account", "instrument", "as_of", "value", "method", "source_ref"]
        labels = {"instrument": "Fund / holding", "as_of": "Statement date", "source_ref": "Statement reference"}
        help_texts = {"value": "A value far from the last mark rolled forward is held for review, not booked."}

    def setup(self) -> None:
        self.fields["instrument"].queryset = scoped(Instrument, self.workspace).filter(is_unitized=False)


class PriceForm(WorkspaceForm):
    class Meta:
        model = Price
        fields = ["instrument", "as_of", "price"]
        labels = {"instrument": "Security", "as_of": "Price date"}

    def setup(self) -> None:
        self.fields["instrument"].queryset = scoped(Instrument, self.workspace).filter(is_unitized=True)
        self.instance.source = self.instance.source or "manual"
