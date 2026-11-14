"""M3 GL bridge: how investment activity becomes journal entries in Campfire, and what was sent.

The investment book of record (investments.Transaction) stays the source of truth. Posting turns transactions
that aren't in the GL yet into summarized, balanced journal entries, one per Campfire entity and month, and
records which transactions each entry carries (lineage both ways: Campfire's `source_id` names the posting,
the posting lists the transactions, each transaction names its import batch and file line).
"""

from django.core.exceptions import ValidationError
from django.db import models

from campfire_integration.models import Workspace
from investments.models import Account, AssetClass, Transaction

MONEY = {"max_digits": 20, "decimal_places": 2}
SOURCE = "Family Office"  # Campfire journal entry `source`; `source_id` is JournalPosting.source_id


class Role(models.TextChoices):
    """What a posting line is for. Each role is mapped to a Campfire GL account (AccountMapping)."""

    CASH = "cash", "Cash at custodian"
    CASH_CLEARING = "cash_clearing", "Investment cash clearing"
    TRANSFERS = "transfers", "Transfers clearing"
    INVESTMENTS = "investments", "Investments at cost"
    DIVIDEND_INCOME = "dividend_income", "Dividend income"
    INTEREST_INCOME = "interest_income", "Interest income"
    FUND_INCOME = "fund_income", "Fund income distributions"
    REALIZED_GAIN = "realized_gain", "Realized gain / loss"
    INVESTMENT_FEES = "investment_fees", "Investment fees"
    UNREALIZED = "unrealized", "Unrealized appreciation (balance sheet)"
    UNREALIZED_GAIN = "unrealized_gain", "Unrealized gain / loss (P&L)"


# Roles whose balances the tie-out compares with the GL (REC-04). The others are clearing or P&L accounts.
TIED_OUT_ROLES = (Role.CASH, Role.INVESTMENTS)
# Lines in these roles carry the asset-class and holding dimensions.
INSTRUMENT_ROLES = {Role.INVESTMENTS, Role.DIVIDEND_INCOME, Role.INTEREST_INCOME, Role.FUND_INCOME, Role.REALIZED_GAIN, Role.INVESTMENT_FEES,
                    Role.UNREALIZED, Role.UNREALIZED_GAIN}


class GLSettings(models.Model):
    """Per-workspace GL bridge settings."""

    workspace = models.OneToOneField(Workspace, on_delete=models.CASCADE, related_name="gl_settings")
    # Activity before this date is already in the GL (opening balances at go-live) and is never posted.
    post_from = models.DateField(null=True, blank=True)
    # Accounting policy: empty keeps the books at cost. A date books investments at market value from that month on,
    # with a month-end entry that brings unrealized appreciation to market value less cost.
    fair_value_from = models.DateField(null=True, blank=True)
    # Rate (percent) for the estimate of income taxes on unrealized appreciation in the Statement of Net Worth
    # (personal financial statements, ASC 274). 0 leaves the estimate out.
    estimated_tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    class Meta:
        verbose_name_plural = "GL settings"

    def __str__(self) -> str:
        return f"GL settings for {self.workspace}"

    @classmethod
    def for_workspace(cls, workspace: Workspace) -> "GLSettings":
        return cls.objects.get_or_create(workspace=workspace)[0]


class AccountMapping(models.Model):
    """Role -> Campfire GL account. Narrower mappings win: FO account, then Campfire entity, then asset class."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="gl_account_mappings")
    role = models.CharField(max_length=24, choices=Role.choices)
    campfire_entity_id = models.BigIntegerField(null=True, blank=True, help_text="Only for this Campfire entity")
    asset_class = models.ForeignKey(AssetClass, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    account = models.ForeignKey(Account, null=True, blank=True, on_delete=models.CASCADE, related_name="+", help_text="Only for this FO account")
    campfire_account_id = models.BigIntegerField()
    campfire_account_label = models.CharField(max_length=255, blank=True, help_text="Number and name, for display")

    class Meta:
        ordering = ["workspace", "role", "id"]

    def __str__(self) -> str:
        scope = [str(x) for x in (self.account, self.asset_class) if x] + ([f"entity {self.campfire_entity_id}"] if self.campfire_entity_id else [])
        return f"{self.get_role_display()}{' (' + ', '.join(scope) + ')' if scope else ''} -> {self.campfire_account_label or self.campfire_account_id}"

    @property
    def specificity(self) -> int:
        return 4 * bool(self.account_id) + 2 * bool(self.campfire_entity_id) + bool(self.asset_class_id)

    def clean(self) -> None:
        if self.asset_class_id and self.asset_class.workspace_id != self.workspace_id:
            raise ValidationError("The asset class belongs to another workspace.")
        if self.account_id and self.account.family.workspace_id != self.workspace_id:
            raise ValidationError("The account belongs to another workspace.")
        same = AccountMapping.objects.filter(
            workspace=self.workspace_id, role=self.role, campfire_entity_id=self.campfire_entity_id,
            asset_class=self.asset_class_id, account=self.account_id,
        ).exclude(pk=self.pk)
        if same.exists():
            raise ValidationError("There is already a mapping for this role and scope.")


class DimensionTag(models.Model):
    """The Campfire tag (custom dimension value) that stands for an FO value on posted lines."""

    class Dimension(models.TextChoices):
        FAMILY = "family", "Family"
        BRANCH = "branch", "Family branch"
        TAX_TREATMENT = "tax_treatment", "Tax treatment"
        CUSTODIAN = "custodian", "Custodian"
        ASSET_CLASS = "asset_class", "Asset class"
        HOLDING = "holding", "Holding / fund"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="gl_dimension_tags")
    dimension = models.CharField(max_length=16, choices=Dimension.choices)
    key = models.CharField(max_length=100, help_text="Family code, asset class code or instrument id")
    campfire_tag_id = models.BigIntegerField()
    label = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["workspace", "dimension", "label"]
        constraints = [models.UniqueConstraint(fields=["workspace", "dimension", "key"], name="gl_dimension_tag_unique")]

    def __str__(self) -> str:
        return f"{self.get_dimension_display()}: {self.label or self.key}"


class JournalPosting(models.Model):
    """One journal entry the app sent (or tried to send) to Campfire."""

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Awaiting approval in Campfire"
        POSTED = "posted", "Posted"
        FAILED = "failed", "Failed"
        UNCERTAIN = "uncertain", "No answer from Campfire"
        RELEASED = "released", "Released (deleted or rejected in Campfire)"

    class Kind(models.TextChoices):
        ACTIVITY = "activity", "Investment activity"
        FAIR_VALUE = "fair_value", "Market value adjustment"

    OPEN_STATUSES = (Status.SUBMITTED, Status.POSTED, Status.UNCERTAIN)  # its transactions count as posted

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="journal_postings")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.ACTIVITY)
    campfire_entity_id = models.BigIntegerField()
    entity_label = models.CharField(max_length=255, blank=True, help_text="FO entities booked in this Campfire entity")
    period_end = models.DateField()
    je_date = models.DateField()
    memo = models.CharField(max_length=255)
    currency = models.CharField(max_length=3, default="USD")
    lines = models.JSONField(default=list, help_text="As sent: account, debit, credit, tags, description, external_id")
    total = models.DecimalField(**MONEY, help_text="Total debits")
    transaction_ids = models.JSONField(default=list, help_text="Kept after a release, for the audit trail")
    status = models.CharField(max_length=12, choices=Status.choices)
    campfire_journal_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    response = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    created_by_id = models.CharField(max_length=64, blank=True, help_text="Stable id of who sent it (cfu:<campfire user id>)")
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # One live market value entry per entity and month (two people clicking Send at once).
            models.UniqueConstraint(fields=["workspace", "campfire_entity_id", "period_end"], name="fair_value_posting_unique",
                                    condition=models.Q(kind="fair_value", status__in=["submitted", "posted", "uncertain"])),
        ]

    def __str__(self) -> str:
        return f"{self.source_id} {self.je_date} {self.entity_label} ({self.get_status_display()})"

    @property
    def source_id(self) -> str:
        return f"fo-posting-{self.pk}"

    @property
    def ref_number(self) -> str:
        return f"FO-{self.pk}"


class PostedTransaction(models.Model):
    """A transaction carried by a posting. One per transaction: that is what stops double posting."""

    posting = models.ForeignKey(JournalPosting, on_delete=models.CASCADE, related_name="items")
    transaction = models.OneToOneField(Transaction, on_delete=models.CASCADE, related_name="gl_item")

    def __str__(self) -> str:
        return f"{self.posting.source_id}: {self.transaction}"


class FairValueItem(models.Model):
    """One holding's share of a market value adjustment: what the entry moved, and the unrealized balance it left.
    The unrealized amount in the GL for a holding is the sum of its items on entries that are still open."""

    posting = models.ForeignKey(JournalPosting, on_delete=models.CASCADE, related_name="fair_value_items")
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="+")
    instrument = models.ForeignKey("investments.Instrument", on_delete=models.CASCADE, related_name="+")
    cost_basis = models.DecimalField(**MONEY)
    market_value = models.DecimalField(**MONEY)
    unrealized = models.DecimalField(**MONEY, help_text="Market value less cost at the period end")
    adjustment = models.DecimalField(**MONEY, help_text="Change booked by this entry (debit to unrealized appreciation positive)")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["posting", "account", "instrument"], name="fair_value_item_unique")]

    def __str__(self) -> str:
        return f"{self.posting.source_id}: {self.instrument} {self.adjustment}"


class MonthClose(models.Model):
    """The month-end close of one Campfire entity: the steps people confirm (data complete, tie-out, sign-off)."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="month_closes")
    campfire_entity_id = models.BigIntegerField()
    month = models.DateField(help_text="First day of the month")
    data_done_by = models.CharField(max_length=255, blank=True)
    data_done_at = models.DateTimeField(null=True, blank=True)
    tieout_at = models.DateTimeField(null=True, blank=True)
    tieout_ok = models.BooleanField(default=False)
    tieout_detail = models.JSONField(default=list, blank=True, help_text="Rows that didn't tie: account, difference")
    signed_off_by = models.CharField(max_length=255, blank=True)
    signed_off_by_id = models.CharField(max_length=64, blank=True)
    signed_off_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-month", "campfire_entity_id"]
        constraints = [models.UniqueConstraint(fields=["workspace", "campfire_entity_id", "month"], name="month_close_unique")]

    def __str__(self) -> str:
        return f"Close {self.month:%Y-%m} entity {self.campfire_entity_id}"


class CampfireBalanceSnapshot(models.Model):
    """A Campfire entity's balance-sheet balances on a date, read from its trial balance (for net worth). Kept so
    pages don't call Campfire on every view; refreshed on demand."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="balance_snapshots")
    campfire_entity_id = models.BigIntegerField()
    as_of = models.DateField()
    fetched_at = models.DateTimeField(auto_now=True)
    lines = models.JSONField(default=list, help_text="account_id, number, name, account_type, subtype, net (debit - credit)")

    class Meta:
        ordering = ["-as_of", "campfire_entity_id"]
        constraints = [models.UniqueConstraint(fields=["workspace", "campfire_entity_id", "as_of"], name="balance_snapshot_unique")]

    def __str__(self) -> str:
        return f"Campfire balances {self.campfire_entity_id} {self.as_of}"


class BankLineDismissal(models.Model):
    """A Campfire bank-feed line someone said isn't a capital call or distribution: no longer suggested."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="bank_dismissals")
    bank_line_id = models.BigIntegerField()
    dismissed_by = models.CharField(max_length=255, blank=True)
    dismissed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "bank_line_id"], name="bank_dismissal_unique")]


class BillRequest(models.Model):
    """A bill entered in the app. It waits for approval here (by someone other than whoever entered it, when the
    workspace asks for a second person), and only then goes to Campfire's payables."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for approval"
        SENT = "sent", "In Campfire"
        AWAITING = "awaiting", "Approved here; awaiting approval in Campfire"
        REJECTED = "rejected", "Rejected"
        FAILED = "failed", "Campfire refused it"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="bill_requests")
    campfire_entity_id = models.BigIntegerField()
    entity_label = models.CharField(max_length=255)
    campfire_vendor_id = models.BigIntegerField()
    vendor_name = models.CharField(max_length=255)
    bill_number = models.CharField(max_length=120)
    bill_date = models.DateField()
    due_date = models.DateField()
    description = models.CharField(max_length=255)
    campfire_account_id = models.BigIntegerField()
    account_label = models.CharField(max_length=255)
    amount = models.DecimalField(**MONEY)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_by = models.CharField(max_length=255, blank=True)
    created_by_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.CharField(max_length=255, blank=True)
    decided_by_id = models.CharField(max_length=64, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    campfire_bill_id = models.BigIntegerField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.vendor_name} {self.bill_number} {self.amount} ({self.get_status_display()})"

    @property
    def source_id(self) -> str:
        return f"fo-bill-{self.pk}"


class JournalRequest(models.Model):
    """A manual journal entry (or a transfer between two entities) entered in the app. It waits for approval here,
    then goes to Campfire's ledger: one entry per entity."""

    class Kind(models.TextChoices):
        MANUAL = "manual", "Journal entry"
        TRANSFER = "transfer", "Transfer between entities"

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for approval"
        SENT = "sent", "In Campfire"
        AWAITING = "awaiting", "Approved here; awaiting approval in Campfire"
        REJECTED = "rejected", "Rejected"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="journal_requests")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.MANUAL)
    date = models.DateField()
    memo = models.CharField(max_length=255)
    # [{entity_id, entity, campfire_id (once sent), lines: [{account_id, account, debit, credit, description}]}]
    entries = models.JSONField(default=list)
    total = models.DecimalField(**MONEY, help_text="Total debits")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_by = models.CharField(max_length=255, blank=True)
    created_by_id = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.CharField(max_length=255, blank=True)
    decided_by_id = models.CharField(max_length=64, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.date} {self.memo} ({self.get_status_display()})"

    @property
    def ref_number(self) -> str:
        return f"FO-JE-{self.pk}"
