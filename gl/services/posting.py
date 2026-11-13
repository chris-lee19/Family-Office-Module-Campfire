"""From unposted investment transactions to journal entries in Campfire.

    preview = build_preview(workspace, through=date)   # nothing is written or sent
    postings = send(workspace, through=date, client=CampfireClient(workspace))

What is posted: transactions from the workspace's GL start date (`GLSettings.post_from`; earlier activity is in
the GL through opening balances) up to `through` that no open posting carries yet, in accounts owned by an entity
linked to a Campfire entity (`Entity.campfire_entity_id`). Contributions paid through a Campfire bill are
already in the GL (the bill booked them) and are skipped. Accounts owned by a person, or by an entity that
isn't linked, are reported, not posted.

One journal entry per Campfire entity and calendar month, dated the month end (or `through`, if earlier).
Lines are summarized by GL account and tags. A group with any problem (an unmapped role, a ledger that
doesn't replay, a non-USD account) is held back whole; the others go.

Campfire answers 201 when it posts the entry and 202 when it holds it for approval (always, with the draft
scopes the app requests by default). A `JournalEntry.created` event, or `refresh_statuses`, then finds the
posted entry by its `source_id`. If an entry is deleted or rejected in Campfire, `release` frees its
transactions so the next run posts them again.
"""

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import requests
from django.db import IntegrityError, transaction
from django.utils import timezone

from campfire_integration.client import CampfireClient, CampfireError, NotConnected
from campfire_integration.models import Workspace
from gl.models import INSTRUMENT_ROLES, SOURCE, AccountMapping, DimensionTag, GLSettings, JournalPosting, PostedTransaction, Role
from gl.services.rules import Leg, RuleError, legs_for
from investments.models import Account, Transaction
from investments.services.ledger import LedgerError, replay

ZERO = Decimal(0)
CENT = Decimal("0.01")
BASE_CURRENCY = "USD"
JE_PATH = "/coa/api/journal_entry"
S = JournalPosting.Status


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


# ------------------------------------------------------------------------------------------------ mapping


class Mappings:
    def __init__(self, workspace: Workspace) -> None:
        self.by_role: dict[str, list[AccountMapping]] = defaultdict(list)
        for m in AccountMapping.objects.filter(workspace=workspace):
            self.by_role[m.role].append(m)
        for candidates in self.by_role.values():
            candidates.sort(key=lambda m: -m.specificity)

    def resolve(self, role: str, *, account: Account, campfire_entity_id: int, asset_class_id: int | None) -> AccountMapping | None:
        for m in self.by_role.get(role, []):
            if m.account_id and m.account_id != account.pk:
                continue
            if m.campfire_entity_id and m.campfire_entity_id != campfire_entity_id:
                continue
            if m.asset_class_id and m.asset_class_id != asset_class_id:
                continue
            return m
        return None


class Tags:
    def __init__(self, workspace: Workspace) -> None:
        self.ids = {(t.dimension, t.key): t.campfire_tag_id for t in DimensionTag.objects.filter(workspace=workspace)}
        self.configured = {t.dimension for t in DimensionTag.objects.filter(workspace=workspace).only("dimension")}

    def for_leg(self, leg: Leg, missing: set[str]) -> tuple[int, ...]:
        txn, D = leg.txn, DimensionTag.Dimension
        wanted = [(D.FAMILY, txn.account.family.code, txn.account.family.name)]
        if leg.role in INSTRUMENT_ROLES and txn.instrument is not None:
            wanted += [(D.ASSET_CLASS, txn.instrument.asset_class.code, txn.instrument.asset_class.name),
                       (D.HOLDING, str(txn.instrument.pk), str(txn.instrument))]
        tags = []
        for dimension, key, label in wanted:
            tag = self.ids.get((dimension, key))
            if tag is not None:
                tags.append(tag)
            elif dimension in self.configured:  # a dimension that is set up but lacks this value
                missing.add(f"{D(dimension).label} '{label}'")
        return tuple(sorted(tags))


# ------------------------------------------------------------------------------------------------ preview


@dataclass
class Line:
    campfire_account_id: int
    account_label: str
    tags: tuple[int, ...]
    roles: set[str]
    amount: Decimal  # debit positive

    @property
    def debit(self) -> Decimal:
        return self.amount if self.amount > 0 else ZERO

    @property
    def credit(self) -> Decimal:
        return -self.amount if self.amount < 0 else ZERO

    @property
    def description(self) -> str:
        return "FO: " + ", ".join(sorted(Role(r).label for r in self.roles))


@dataclass
class ProposedJournal:
    campfire_entity_id: int
    period_end: date
    je_date: date
    entities: set[str] = field(default_factory=set)
    families: set[str] = field(default_factory=set)
    transactions: list[Transaction] = field(default_factory=list)
    legs: list[tuple[Leg, AccountMapping | None, tuple[int, ...]]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: set[str] = field(default_factory=set)

    @property
    def lines(self) -> list[Line]:
        merged: dict[tuple[int, tuple[int, ...]], Line] = {}
        for leg, mapping, tags in self.legs:
            if mapping is None:
                continue
            key = (mapping.campfire_account_id, tags)
            line = merged.setdefault(key, Line(mapping.campfire_account_id, mapping.campfire_account_label, tags, set(), ZERO))
            line.amount += leg.amount
            line.roles.add(leg.role)
        lines = [ln for ln in merged.values() if ln.amount]
        return sorted(lines, key=lambda ln: (ln.amount < 0, ln.account_label, ln.tags))

    @property
    def total(self) -> Decimal:
        return sum((ln.debit for ln in self.lines), ZERO)

    @property
    def entity_label(self) -> str:
        return ", ".join(sorted(self.entities))

    @property
    def memo(self) -> str:
        return f"Family office investment activity, {self.period_end:%B %Y} ({len(self.transactions)} transactions)"

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class Preview:
    through: date
    journals: list[ProposedJournal]
    not_linked: dict[str, int]  # owner -> transactions not posted because the owner isn't linked to Campfire
    billed_contributions: int  # contributions already booked by a Campfire bill
    post_from: date | None = None
    before_start: int = 0  # transactions before post_from: in the GL through opening balances

    @property
    def ready(self) -> list[ProposedJournal]:
        return [j for j in self.journals if j.ok and j.lines]

    @property
    def held(self) -> list[ProposedJournal]:
        return [j for j in self.journals if not j.ok]


def unposted(workspace: Workspace, through: date):  # type: ignore[no-untyped-def]
    return (
        Transaction.objects.filter(account__family__workspace=workspace, trade_date__lte=through, gl_item__isnull=True)
        .select_related("account__entity", "account__person", "account__family", "instrument__asset_class")
        .order_by("trade_date", "id")
    )


def _billed(qs):  # type: ignore[no-untyped-def]
    return qs.filter(capital_call__campfire_bill_id__isnull=False)


def build_preview(workspace: Workspace, *, through: date) -> Preview:
    post_from = GLSettings.for_workspace(workspace).post_from
    qs = unposted(workspace, through)
    before_start = 0
    if post_from is not None:
        before_start = qs.filter(trade_date__lt=post_from).count()
        qs = qs.filter(trade_date__gte=post_from)
    billed = _billed(qs).count()
    mappings, tags = Mappings(workspace), Tags(workspace)
    journals: dict[tuple[int, date], ProposedJournal] = {}
    not_linked: dict[str, int] = defaultdict(int)
    by_account: dict[int, list[Transaction]] = defaultdict(list)
    for txn in qs.exclude(pk__in=_billed(qs).values("pk")):
        entity = txn.account.entity
        if entity is None or entity.campfire_entity_id is None:
            not_linked[str(txn.account.owner)] += 1
            continue
        by_account[txn.account_id].append(txn)

    for txns in by_account.values():
        account = txns[0].account
        cf_entity = account.entity.campfire_entity_id  # type: ignore[union-attr]
        try:
            state = replay(account, through)
            ledger_error = ""
        except LedgerError as e:
            state, ledger_error = None, str(e)
        for txn in txns:
            period = month_end(txn.trade_date)
            j = journals.setdefault((cf_entity, period), ProposedJournal(cf_entity, period, min(period, through)))
            j.entities.add(account.entity.name)  # type: ignore[union-attr]
            j.families.add(account.family.name)
            j.transactions.append(txn)
            if ledger_error:
                j.errors.append(f"{account.name}: the ledger doesn't replay ({ledger_error}).")
                continue
            if account.currency != BASE_CURRENCY:
                j.errors.append(f"{account.name} is in {account.currency}; only {BASE_CURRENCY} accounts are posted for now.")
                continue
            try:
                legs = legs_for(txn, state)  # type: ignore[arg-type]
            except RuleError as e:
                j.errors.append(str(e))
                continue
            for leg in legs:
                asset_class_id = txn.instrument.asset_class_id if txn.instrument else None
                mapping = mappings.resolve(leg.role, account=account, campfire_entity_id=cf_entity, asset_class_id=asset_class_id)
                if mapping is None:
                    j.errors.append(f"No GL account mapped for {Role(leg.role).label}" + (f" ({txn.instrument.asset_class})" if txn.instrument else "") + ".")
                j.legs.append((leg, mapping, tags.for_leg(leg, j.warnings)))
    for j in journals.values():
        j.errors = list(dict.fromkeys(j.errors))  # de-duplicate, keep order
        if j.ok and sum((ln.amount for ln in j.lines), ZERO) != ZERO:
            j.errors.append("The entry doesn't balance.")
    ordered = sorted(journals.values(), key=lambda j: (j.period_end, j.entity_label))
    return Preview(through, ordered, dict(not_linked), billed, post_from, before_start)


# ------------------------------------------------------------------------------------------------- sending


def _payload(posting: JournalPosting) -> dict[str, Any]:
    return {
        "date": posting.je_date.isoformat(),
        "type": "journal_entry",
        "memo": posting.memo,
        "entity": str(posting.campfire_entity_id),
        "currency": posting.currency,
        "source": SOURCE,
        "source_id": posting.source_id,
        "ref_number": posting.ref_number,
        "transactions": [
            {
                "account": str(ln["account"]),
                **({"debit_amount_native": float(ln["debit"])} if Decimal(ln["debit"]) else {"credit_amount_native": float(ln["credit"])}),
                "tags": [{"id": str(t)} for t in ln["tags"]],
                "bank_description": ln["description"],
                "external_id": ln["external_id"],
            }
            for ln in posting.lines
        ],
    }


def _create_posting(workspace: Workspace, j: ProposedJournal, created_by: str) -> JournalPosting:
    with transaction.atomic():
        posting = JournalPosting.objects.create(
            workspace=workspace, campfire_entity_id=j.campfire_entity_id, entity_label=j.entity_label[:255],
            period_end=j.period_end, je_date=j.je_date, memo=j.memo[:255], total=j.total, status=S.UNCERTAIN,
            transaction_ids=[t.pk for t in j.transactions], created_by=created_by,
        )
        posting.lines = [
            {"account": ln.campfire_account_id, "account_label": ln.account_label, "debit": str(ln.debit), "credit": str(ln.credit),
             "tags": list(ln.tags), "description": ln.description[:255], "external_id": f"{posting.source_id}-{n}"}
            for n, ln in enumerate(j.lines, start=1)
        ]
        posting.save(update_fields=["lines"])
        # The one-to-one on the transaction refuses a second posting of it (two people clicking at once).
        PostedTransaction.objects.bulk_create([PostedTransaction(posting=posting, transaction=t) for t in j.transactions])
    return posting


def submit(posting: JournalPosting, client: CampfireClient) -> JournalPosting:
    posting.submitted_at = timezone.now()
    try:
        r = client.request("POST", JE_PATH, json=_payload(posting))
    except (requests.RequestException, NotConnected) as e:
        # We can't tell whether Campfire saved it. Keep the transactions held; refresh_statuses looks for it.
        posting.status, posting.error = S.UNCERTAIN, f"{type(e).__name__}: {e}"
        posting.save()
        return posting
    try:
        body: Any = r.json()
    except ValueError:
        body = {"text": r.text[:2000]}
    posting.response = body
    if r.status_code == 201 or (r.status_code == 200 and isinstance(body, dict) and body.get("id")):
        posting.status, posting.posted_at = S.POSTED, timezone.now()
        posting.campfire_journal_id = body.get("id") if isinstance(body, dict) else None
        posting.error = ""
    elif r.status_code == 202:
        posting.status, posting.error = S.SUBMITTED, ""
    else:
        posting.status, posting.error = S.FAILED, f"Campfire answered {r.status_code}: {r.text[:1000]}"
        posting.save()
        posting.items.all().delete()  # nothing reached the GL; the transactions post with the next run
        return posting
    posting.save()
    return posting


def send(workspace: Workspace, *, through: date, client: CampfireClient, created_by: str = "") -> list[JournalPosting]:
    preview = build_preview(workspace, through=through)
    postings = []
    for j in preview.ready:
        try:
            posting = _create_posting(workspace, j, created_by)
        except IntegrityError:
            continue  # another run took these transactions meanwhile
        postings.append(submit(posting, client))
    return postings


# ----------------------------------------------------------------------------------------- after sending


def mark_posted(posting: JournalPosting, journal_id: int | None) -> JournalPosting:
    if posting.status == S.RELEASED:
        return posting
    posting.status = S.POSTED
    posting.campfire_journal_id = journal_id or posting.campfire_journal_id
    posting.posted_at = posting.posted_at or timezone.now()
    posting.error = ""
    posting.save(update_fields=["status", "campfire_journal_id", "posted_at", "error"])
    return posting


def release(posting: JournalPosting, reason: str) -> JournalPosting:
    """The entry is not (or no longer) in Campfire: free its transactions so the next run posts them again."""
    with transaction.atomic():
        posting.items.all().delete()
        posting.status = S.RELEASED
        posting.error = reason
        posting.save(update_fields=["status", "error"])
    return posting


def posting_for_source_id(workspace: Workspace, source_id: str | None) -> JournalPosting | None:
    prefix = "fo-posting-"
    if not source_id or not source_id.startswith(prefix) or not source_id[len(prefix):].isdigit():
        return None
    return JournalPosting.objects.filter(workspace=workspace, pk=int(source_id[len(prefix):])).first()


def refresh_statuses(workspace: Workspace, client: CampfireClient) -> int:
    """Look in Campfire's GL for entries awaiting approval or unanswered. Returns how many are now posted."""
    waiting = list(JournalPosting.objects.filter(workspace=workspace, status__in=[S.SUBMITTED, S.UNCERTAIN]))
    if not waiting:
        return 0
    start, end = min(p.je_date for p in waiting), max(p.je_date for p in waiting)
    found: dict[str, int] = {}
    params: dict[str, Any] | None = {"source": SOURCE, "start_date": start.isoformat(), "end_date": end.isoformat(), "limit": 100}
    path = JE_PATH
    while path:
        page = client.get_json(path, params=params)
        for je in page.get("results", []):
            if je.get("source_id") and not je.get("is_deleted"):
                found[je["source_id"]] = je["id"]
        nxt = page.get("next")
        path, params = (nxt.split(client_base(), 1)[-1], None) if nxt else ("", None)
    count = 0
    for p in waiting:
        if p.source_id in found:
            mark_posted(p, found[p.source_id])
            count += 1
    return count


def client_base() -> str:
    from django.conf import settings

    return settings.CAMPFIRE["BASE_URL"]


__all__ = [
    "Preview", "ProposedJournal", "build_preview", "send", "submit", "release", "mark_posted", "refresh_statuses",
    "posting_for_source_id", "CampfireError", "NotConnected",
]
