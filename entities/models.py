"""M1 Entities & Ownership: the legal and family structure, which Campfire's accounting entities don't model.

Tenancy: Workspace (Campfire customer) -> Family -> everything else. A single-family office has one
Family; a multi-family office has one per client family.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from campfire_integration.models import Workspace


class Family(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="families")
    name = models.CharField(max_length=200)
    code = models.SlugField(max_length=50, help_text="Short code used in reports and Campfire dimension values.")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "families"
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["workspace", "code"], name="family_code_unique_per_workspace")]

    def __str__(self) -> str:
        return self.name


class Branch(models.Model):
    """A household / family branch. Nested branches model generations (G1 -> G2 branch -> G3 household)."""

    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name="branches")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="children")
    name = models.CharField(max_length=200)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name_plural = "branches"
        ordering = ["family", "sort_order", "name"]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        if self.parent_id and self.parent.family_id != self.family_id:
            raise ValidationError("A branch's parent must belong to the same family.")


class Person(models.Model):
    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name="people")
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.SET_NULL, related_name="people")
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    preferred_name = models.CharField(max_length=100, blank=True)
    # Year only: enough for generation/age views without holding a full date of birth.
    birth_year = models.PositiveSmallIntegerField(null=True, blank=True)
    generation = models.PositiveSmallIntegerField(null=True, blank=True, help_text="1 = founding generation")
    is_principal = models.BooleanField(default=False)
    email = models.EmailField(blank=True)
    campfire_user_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "people"
        ordering = ["family", "generation", "last_name", "first_name"]

    def __str__(self) -> str:
        return self.display_name

    @property
    def display_name(self) -> str:
        return f"{self.preferred_name or self.first_name} {self.last_name}"


class Entity(models.Model):
    class EntityType(models.TextChoices):
        REVOCABLE_TRUST = "revocable_trust", "Revocable trust"
        IRREVOCABLE_TRUST = "irrevocable_trust", "Irrevocable trust"
        LLC = "llc", "LLC"
        LIMITED_PARTNERSHIP = "lp", "Limited partnership"
        GENERAL_PARTNERSHIP = "gp", "General partnership"
        CORPORATION = "corporation", "Corporation"
        PRIVATE_FOUNDATION = "private_foundation", "Private foundation"
        DONOR_ADVISED_FUND = "daf", "Donor-advised fund"
        ESTATE = "estate", "Estate"
        OTHER = "other", "Other"

    class TaxClassification(models.TextChoices):
        DISREGARDED = "disregarded", "Disregarded entity"
        PARTNERSHIP = "partnership", "Partnership"
        C_CORPORATION = "c_corp", "C corporation"
        S_CORPORATION = "s_corp", "S corporation"
        GRANTOR_TRUST = "grantor_trust", "Grantor trust"
        NON_GRANTOR_TRUST = "non_grantor_trust", "Non-grantor trust"
        TAX_EXEMPT = "tax_exempt", "Tax-exempt"
        ESTATE = "estate", "Estate"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending formation"
        ACTIVE = "active", "Active"
        DISSOLVED = "dissolved", "Dissolved"

    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name="entities")
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.SET_NULL, related_name="entities")
    name = models.CharField(max_length=255)
    short_name = models.CharField(max_length=60, blank=True)
    entity_type = models.CharField(max_length=32, choices=EntityType.choices)
    tax_classification = models.CharField(max_length=32, choices=TaxClassification.choices)
    jurisdiction = models.CharField(max_length=20, blank=True, help_text="ISO 3166-2 style, e.g. US-DE")
    formation_date = models.DateField(null=True, blank=True)
    termination_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    tax_id_last4 = models.CharField(max_length=4, blank=True)
    # Set when the entity keeps books in Campfire; many family-office entities (e.g. a trust that only
    # holds LLC interests) are tracked here without a ledger of their own.
    campfire_entity_id = models.BigIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "entities"
        ordering = ["family", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["family", "campfire_entity_id"],
                condition=Q(campfire_entity_id__isnull=False),
                name="entity_campfire_id_unique_per_family",
            )
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_booked(self) -> bool:
        return self.campfire_entity_id is not None

    @property
    def is_taxable(self) -> bool:
        return self.tax_classification != self.TaxClassification.TAX_EXEMPT

    def clean(self) -> None:
        if self.branch_id and self.branch.family_id != self.family_id:
            raise ValidationError("The branch must belong to the same family.")
        if self.campfire_entity_id is not None:
            clash = Entity.objects.filter(
                family__workspace_id=self.family.workspace_id, campfire_entity_id=self.campfire_entity_id
            ).exclude(pk=self.pk)
            if clash.exists():
                raise ValidationError("Another entity in this workspace is already linked to that Campfire entity.")


class OwnershipInterest(models.Model):
    """An owner's percentage interest in an entity over a date range. Exactly one owner: an entity or a person."""

    class InterestType(models.TextChoices):
        MEMBER = "member", "Member (LLC)"
        LIMITED_PARTNER = "lp", "Limited partner"
        GENERAL_PARTNER = "gp", "General partner"
        SHAREHOLDER = "shareholder", "Shareholder"
        BENEFICIARY = "beneficiary", "Beneficiary (economic share)"
        OTHER = "other", "Other"

    owned = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="owner_interests")
    owner_entity = models.ForeignKey(
        Entity, null=True, blank=True, on_delete=models.CASCADE, related_name="ownership_interests"
    )
    owner_person = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.CASCADE, related_name="ownership_interests"
    )
    percentage = models.DecimalField(
        max_digits=9,
        decimal_places=6,
        validators=[MinValueValidator(Decimal("0.000001")), MaxValueValidator(Decimal("100"))],
    )
    interest_type = models.CharField(max_length=16, choices=InterestType.choices)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True, help_text="Last day the interest applies; empty = current")

    class Meta:
        ordering = ["owned", "-percentage"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(owner_entity__isnull=False) & Q(owner_person__isnull=True))
                | (Q(owner_entity__isnull=True) & Q(owner_person__isnull=False)),
                name="ownership_exactly_one_owner",
            ),
            models.CheckConstraint(
                condition=Q(percentage__gt=0) & Q(percentage__lte=100), name="ownership_percentage_range"
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True) | Q(effective_to__gte=models.F("effective_from")),
                name="ownership_dates_ordered",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.owner} owns {self.percentage}% of {self.owned}"

    @property
    def owner(self) -> Entity | Person:
        return self.owner_entity or self.owner_person  # type: ignore[return-value]

    def clean(self) -> None:
        if self.owner_entity_id and self.owner_entity_id == self.owned_id:
            raise ValidationError("An entity cannot own itself.")
        owner_family = (self.owner_entity or self.owner_person).family_id if (self.owner_entity_id or self.owner_person_id) else None
        if owner_family is not None and owner_family != self.owned.family_id:
            raise ValidationError("Owner and owned entity must belong to the same family.")


class EntityRole(models.Model):
    """Non-economic roles: who controls or signs for an entity. The holder is a family member or a named outsider."""

    class Role(models.TextChoices):
        GRANTOR = "grantor", "Grantor / settlor"
        TRUSTEE = "trustee", "Trustee"
        SUCCESSOR_TRUSTEE = "successor_trustee", "Successor trustee"
        TRUST_PROTECTOR = "trust_protector", "Trust protector"
        MANAGER = "manager", "Manager"
        OFFICER = "officer", "Officer"
        DIRECTOR = "director", "Director"
        AUTHORIZED_SIGNER = "authorized_signer", "Authorized signer"

    entity = models.ForeignKey(Entity, on_delete=models.CASCADE, related_name="roles")
    role = models.CharField(max_length=32, choices=Role.choices)
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.CASCADE, related_name="roles")
    external_name = models.CharField(max_length=255, blank=True, help_text="e.g. a corporate trustee")
    effective_from = models.DateField(null=True, blank=True)
    effective_to = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["entity", "role"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(person__isnull=False) & Q(external_name="")) | (Q(person__isnull=True) & ~Q(external_name="")),
                name="role_exactly_one_holder",
            )
        ]

    def __str__(self) -> str:
        return f"{self.holder_name}: {self.get_role_display()} of {self.entity}"

    @property
    def holder_name(self) -> str:
        return self.person.display_name if self.person_id else self.external_name
