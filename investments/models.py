"""M2 Investments: the investment book of record.

Transactions are the source of truth. Positions, cost basis, lots and realized gains are derived from
them (investments.services), so a corrected transaction corrects everything downstream. Prices value
unitized securities; NAV/appraisal valuations value private funds and other non-unitized holdings.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from campfire_integration.models import Workspace
from entities.models import Entity, Family, Person

MONEY = {"max_digits": 20, "decimal_places": 2}
QTY = {"max_digits": 24, "decimal_places": 8}


class AssetClass(models.Model):
    """Per-workspace taxonomy (MD-06). The app owns it and pushes it to Campfire as tag values."""

    class Liquidity(models.TextChoices):
        LIQUID = "liquid", "Liquid"
        SEMI_LIQUID = "semi_liquid", "Semi-liquid"
        ILLIQUID = "illiquid", "Illiquid"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="asset_classes")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    code = models.SlugField(max_length=40)
    name = models.CharField(max_length=100)
    liquidity = models.CharField(max_length=16, choices=Liquidity.choices, default=Liquidity.LIQUID)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name_plural = "asset classes"
        ordering = ["workspace", "sort_order", "name"]
        constraints = [models.UniqueConstraint(fields=["workspace", "code"], name="asset_class_code_unique")]

    def __str__(self) -> str:
        return self.name


class Instrument(models.Model):
    """Anything held: a listed security, a private fund interest, a property, a loan, a collectible."""

    class Kind(models.TextChoices):
        CASH_EQUIVALENT = "cash_equivalent", "Cash equivalent / money market"
        EQUITY = "equity", "Listed equity"
        FIXED_INCOME = "fixed_income", "Bond / fixed income"
        FUND = "fund", "Mutual fund / ETF"
        PRIVATE_FUND = "private_fund", "Private fund (LP interest)"
        DIRECT_PRIVATE = "direct_private", "Direct private company"
        REAL_ESTATE = "real_estate", "Real estate (direct)"
        LOAN = "loan", "Loan receivable / note"
        COLLECTIBLE = "collectible", "Art / collectible"
        DIGITAL_ASSET = "digital_asset", "Digital asset"
        OTHER = "other", "Other"

    UNITIZED_KINDS = {Kind.CASH_EQUIVALENT, Kind.EQUITY, Kind.FIXED_INCOME, Kind.FUND, Kind.DIGITAL_ASSET}

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="instruments")
    name = models.CharField(max_length=255)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    asset_class = models.ForeignKey(AssetClass, on_delete=models.PROTECT, related_name="instruments")
    ticker = models.CharField(max_length=20, blank=True)
    cusip = models.CharField(max_length=9, blank=True)
    isin = models.CharField(max_length=12, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    # Unitized: quantity x price, FIFO tax lots. Non-unitized: cost = contributions - return of capital,
    # value = latest NAV/appraisal rolled forward.
    is_unitized = models.BooleanField()
    sponsor = models.CharField(max_length=255, blank=True, help_text="Manager / GP / issuer")
    vintage_year = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["workspace", "name"]

    def __str__(self) -> str:
        return self.ticker or self.name


class Account(models.Model):
    """A container of holdings owned by one entity or one person: a custody account, an LP interest, a property."""

    class AccountType(models.TextChoices):
        BROKERAGE = "brokerage", "Brokerage / custody"
        RETIREMENT = "retirement", "Retirement (IRA, 401k)"
        PRIVATE_INVESTMENT = "private_investment", "Private investment (fund interest, direct deal)"
        REAL_ASSET = "real_asset", "Real asset (property, collectible)"
        OTHER = "other", "Other"

    class TaxTreatment(models.TextChoices):
        TAXABLE = "taxable", "Taxable"
        TAX_DEFERRED = "tax_deferred", "Tax-deferred"
        TAX_EXEMPT = "tax_exempt", "Tax-exempt"

    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name="accounts")
    entity = models.ForeignKey(Entity, null=True, blank=True, on_delete=models.PROTECT, related_name="accounts")
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.PROTECT, related_name="accounts")
    name = models.CharField(max_length=255)
    account_type = models.CharField(max_length=24, choices=AccountType.choices)
    tax_treatment = models.CharField(max_length=16, choices=TaxTreatment.choices, default=TaxTreatment.TAXABLE)
    custodian = models.CharField(max_length=255, blank=True)
    account_number_last4 = models.CharField(max_length=4, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    # Brokerage accounts hold cash; a fund interest doesn't (calls are paid from a bank account in Campfire).
    tracks_cash = models.BooleanField(default=True)
    source = models.CharField(max_length=40, default="manual", help_text="Adapter that feeds it: manual, custodian_file, document_ai, ...")
    external_id = models.CharField(max_length=100, blank=True, help_text="ID in the source system")
    opened_on = models.DateField(null=True, blank=True)
    closed_on = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["family", "name"]
        constraints = [
            models.CheckConstraint(
                condition=(Q(entity__isnull=False) & Q(person__isnull=True)) | (Q(entity__isnull=True) & Q(person__isnull=False)),
                name="account_exactly_one_owner",
            )
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def owner(self) -> Entity | Person:
        return self.entity or self.person  # type: ignore[return-value]

    def clean(self) -> None:
        owner = self.entity if self.entity_id else self.person if self.person_id else None
        if owner is not None and owner.family_id != self.family_id:
            raise ValidationError("The account owner must belong to the account's family.")


class ImportBatch(models.Model):
    """One load from a source (a custodian file, an extracted document, a manual entry session). Lineage root.

    A custodian file can hold accounts of several families (a multi-family office's master account), so a
    batch belongs to the workspace; `family` is set when everything in it belongs to one family.
    File imports go validated -> committed (or rejected); other sources create batches already committed.
    """

    class Kind(models.TextChoices):
        TRANSACTIONS = "transactions", "Transactions"
        PRICES = "prices", "Prices"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        VALIDATED = "validated", "Validated, not yet imported"
        COMMITTED = "committed", "Imported"
        REJECTED = "rejected", "Rejected"
        DISCARDED = "discarded", "Discarded"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="import_batches")
    family = models.ForeignKey(Family, null=True, blank=True, on_delete=models.CASCADE, related_name="import_batches")
    source = models.CharField(max_length=40, help_text="Adapter: custodian_file, document_ai, manual, demo, ...")
    reference = models.CharField(max_length=255, blank=True, help_text="File name, document id, ...")
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.OTHER)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.COMMITTED)
    file_format = models.CharField(max_length=60, blank=True, help_text="Code of the file layout used")
    file_name = models.CharField(max_length=255, blank=True)
    file_sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    content = models.TextField(blank=True, help_text="The file as received, kept for re-validation and audit")
    options = models.JSONField(default=dict, blank=True)
    rows_total = models.PositiveIntegerField(default=0)
    rows_new = models.PositiveIntegerField(default=0)
    rows_duplicate = models.PositiveIntegerField(default=0)
    rows_ignored = models.PositiveIntegerField(default=0)
    created_by = models.CharField(max_length=255, blank=True)
    created_by_id = models.CharField(max_length=64, blank=True, help_text="Stable id of who uploaded it (cfu:<campfire user id>)")
    created_at = models.DateTimeField(auto_now_add=True)
    committed_at = models.DateTimeField(null=True, blank=True)
    # Sign-off of an import that came in with warnings (the review queue).
    reviewed_by = models.CharField(max_length=255, blank=True)
    reviewed_by_id = models.CharField(max_length=64, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.source} {self.reference}".strip()

    @property
    def source_label(self) -> str:
        return {"custodian_file": "Custodian file", "onboarding": "Onboarding workbook", "demo": "Demo data",
                "document_ai": "Document"}.get(self.source, self.source.replace("_", " ").capitalize())

    def clean(self) -> None:
        if self.family_id and self.family.workspace_id != self.workspace_id:
            raise ValidationError("The batch's family must belong to its workspace.")


class ImportIssue(models.Model):
    """A validation finding on an import batch (REC-08). Errors block the import; warnings don't."""

    class Severity(models.TextChoices):
        ERROR = "error", "Error"
        WARNING = "warning", "Warning"
        INFO = "info", "Info"

    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="issues")
    row_number = models.PositiveIntegerField(null=True, blank=True, help_text="Line in the file; empty for file-level findings")
    severity = models.CharField(max_length=8, choices=Severity.choices)
    code = models.CharField(max_length=40)
    message = models.CharField(max_length=500)

    class Meta:
        ordering = ["batch", "id"]

    def __str__(self) -> str:
        where = f"line {self.row_number}: " if self.row_number else ""
        return f"{self.severity} {where}{self.message}"


class CustodianFormat(models.Model):
    """A workspace's own file layout: a built-in preset plus overrides (columns, codes, date format, keywords).

    See investments.importers.formats for the keys `config` accepts.
    """

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="custodian_formats")
    code = models.SlugField(max_length=60)
    name = models.CharField(max_length=120)
    preset = models.CharField(max_length=60, help_text="Built-in layout this one starts from")
    config = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["workspace", "name"]
        constraints = [models.UniqueConstraint(fields=["workspace", "code"], name="custodian_format_code_unique")]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        from investments.importers.formats import FormatError, from_model

        try:
            from_model(self)
        except FormatError as e:
            raise ValidationError(str(e)) from e


class Transaction(models.Model):
    class Type(models.TextChoices):
        BUY = "buy", "Buy"
        SELL = "sell", "Sell"
        TRANSFER_IN = "transfer_in", "Securities transferred in (with cost basis)"
        TRANSFER_OUT = "transfer_out", "Securities transferred out"
        DIVIDEND = "dividend", "Dividend"
        INTEREST = "interest", "Interest"
        FEE = "fee", "Fee"
        DEPOSIT = "deposit", "Cash deposit"
        WITHDRAWAL = "withdrawal", "Cash withdrawal"
        CONTRIBUTION = "contribution", "Capital contribution (call paid)"
        DISTRIBUTION_ROC = "distribution_roc", "Distribution: return of capital"
        DISTRIBUTION_INCOME = "distribution_income", "Distribution: income"
        DISTRIBUTION_GAIN = "distribution_gain", "Distribution: realized gain"

    SECURITY_TYPES = {Type.BUY, Type.SELL, Type.TRANSFER_IN, Type.TRANSFER_OUT}
    DISTRIBUTION_TYPES = {Type.DISTRIBUTION_ROC, Type.DISTRIBUTION_INCOME, Type.DISTRIBUTION_GAIN}

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="transactions")
    instrument = models.ForeignKey(Instrument, null=True, blank=True, on_delete=models.PROTECT, related_name="transactions")
    txn_type = models.CharField(max_length=24, choices=Type.choices)
    trade_date = models.DateField(db_index=True)
    settle_date = models.DateField(null=True, blank=True)
    quantity = models.DecimalField(null=True, blank=True, **QTY, help_text="Positive; direction comes from the type")
    # Gross amount, always positive; direction comes from the type. For a buy/transfer-in it's the cost,
    # for a sell the gross proceeds.
    amount = models.DecimalField(**MONEY)
    fees = models.DecimalField(default=Decimal("0"), **MONEY)
    # For transfer-in: original acquisition date (holding period).
    acquired_on = models.DateField(null=True, blank=True)
    # A distribution the fund may call again (adds back to unfunded commitment).
    is_recallable = models.BooleanField(default=False)
    description = models.CharField(max_length=255, blank=True)
    batch = models.ForeignKey(ImportBatch, null=True, blank=True, on_delete=models.PROTECT, related_name="transactions")
    source_ref = models.CharField(max_length=255, blank=True, help_text="Line/record id in the source, for lineage and de-duplication")

    class Meta:
        ordering = ["account", "trade_date", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gte=0) & Q(fees__gte=0), name="txn_amounts_non_negative"),
            models.CheckConstraint(condition=Q(quantity__isnull=True) | Q(quantity__gt=0), name="txn_quantity_positive"),
            models.UniqueConstraint(
                fields=["account", "source_ref"], condition=~Q(source_ref=""), name="txn_source_ref_unique_per_account"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.trade_date} {self.get_txn_type_display()} {self.instrument or ''} {self.amount}"

    def clean(self) -> None:
        needs_instrument = self.txn_type in self.SECURITY_TYPES or self.txn_type in self.DISTRIBUTION_TYPES or self.txn_type == self.Type.CONTRIBUTION
        if needs_instrument and not self.instrument_id:
            raise ValidationError(f"A {self.get_txn_type_display()} needs an instrument.")
        if self.instrument_id and self.instrument.is_unitized and self.txn_type in self.SECURITY_TYPES and not self.quantity:
            raise ValidationError("Buys, sells and transfers of unitized securities need a quantity.")


class Price(models.Model):
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="prices")
    as_of = models.DateField()
    price = models.DecimalField(**QTY)
    source = models.CharField(max_length=40, default="manual")
    batch = models.ForeignKey(ImportBatch, null=True, blank=True, on_delete=models.SET_NULL, related_name="prices")

    class Meta:
        ordering = ["instrument", "-as_of"]
        constraints = [models.UniqueConstraint(fields=["instrument", "as_of"], name="price_one_per_day")]


class Valuation(models.Model):
    """A statement value for a non-unitized holding (fund NAV, appraisal, manual mark)."""

    class Method(models.TextChoices):
        NAV_STATEMENT = "nav_statement", "Manager NAV statement"
        APPRAISAL = "appraisal", "Appraisal"
        MANUAL = "manual", "Manual mark"
        TRANSACTION = "transaction", "Recent transaction price"

    class Status(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        HELD = "held", "Held for review"
        REJECTED = "rejected", "Rejected"

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="valuations")
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name="valuations")
    as_of = models.DateField()
    value = models.DecimalField(**MONEY)
    method = models.CharField(max_length=20, choices=Method.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACCEPTED)
    change_pct = models.DecimalField(null=True, blank=True, max_digits=12, decimal_places=2, help_text="vs prior accepted value")
    review_note = models.TextField(blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    created_by_id = models.CharField(max_length=64, blank=True, help_text="Stable id of who recorded it (cfu:<campfire user id>)")
    reviewed_by = models.CharField(max_length=255, blank=True)
    reviewed_by_id = models.CharField(max_length=64, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    batch = models.ForeignKey(ImportBatch, null=True, blank=True, on_delete=models.PROTECT, related_name="valuations")
    source_ref = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["account", "instrument", "-as_of"]

    def __str__(self) -> str:
        return f"{self.instrument} {self.as_of} {self.value} ({self.status})"


class Commitment(models.Model):
    """A commitment to a private fund (or other drawdown vehicle) by the account's owner."""

    account = models.OneToOneField(Account, on_delete=models.CASCADE, related_name="commitment")
    instrument = models.ForeignKey(Instrument, on_delete=models.PROTECT, related_name="commitments")
    amount = models.DecimalField(**MONEY)
    commitment_date = models.DateField()
    currency = models.CharField(max_length=3, default="USD")
    # The fund's GP / administrator as a Campfire vendor: the payee of capital-call bills.
    campfire_vendor_id = models.BigIntegerField(null=True, blank=True)
    campfire_vendor_name = models.CharField(max_length=255, blank=True)

    def __str__(self) -> str:
        return f"{self.account}: {self.amount} to {self.instrument}"


class CapitalCall(models.Model):
    """A capital call notice through its life: received -> bill sent to Campfire (awaiting approval) -> bill approved
    (billed) -> paid in Campfire (the contribution is booked). Approval and payment happen in Campfire."""

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        APPROVED = "approved", "Approved for payment"
        SUBMITTED = "submitted", "Bill awaiting approval in Campfire"
        BILLED = "billed", "Bill approved in Campfire"
        PAID = "paid", "Paid"
        CANCELLED = "cancelled", "Cancelled"

    OPEN_STATUSES = (Status.RECEIVED, Status.APPROVED, Status.SUBMITTED, Status.BILLED)

    commitment = models.ForeignKey(Commitment, on_delete=models.CASCADE, related_name="calls")
    notice_date = models.DateField()
    due_date = models.DateField()
    amount = models.DecimalField(**MONEY)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.RECEIVED)
    campfire_bill_id = models.BigIntegerField(null=True, blank=True)
    paid_on = models.DateField(null=True, blank=True)
    contribution = models.OneToOneField(Transaction, null=True, blank=True, on_delete=models.SET_NULL, related_name="capital_call")
    source_ref = models.CharField(max_length=255, blank=True, help_text="Notice document reference")
    bill_sent_at = models.DateTimeField(null=True, blank=True)
    bill_sent_by = models.CharField(max_length=255, blank=True)
    bill_response = models.JSONField(null=True, blank=True)
    bill_note = models.TextField(blank=True, help_text="Last problem or event about the bill")

    class Meta:
        ordering = ["due_date"]

    def __str__(self) -> str:
        return f"{self.commitment.instrument} call {self.amount} due {self.due_date}"

    @property
    def bill_source_id(self) -> str:
        return f"fo-call-{self.pk}"
