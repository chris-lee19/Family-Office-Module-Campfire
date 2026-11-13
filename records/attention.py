"""Needs attention: everything across the app that wants a person, in one list, most urgent first (REC-05).

Each item says what is wrong, why it matters, and links to the page that fixes it. Urgent: something is wrong or late
(a held mark, an overdue call, a failed posting, a tie-out break, GL setup missing). To do: routine work that is due
(entries ready to send, imports to sign off, a month to close, stale prices). FYI: things to know about.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from campfire_integration.models import Workspace
from entities.models import Entity
from gl.models import AccountMapping, JournalPosting, MonthClose, Role
from gl.services.close import booked_entities, previous_month
from gl.services.posting import build_preview
from gl.services.calls import problems as call_problems
from investments.models import Account, CapitalCall, Commitment, ImportBatch, ImportIssue, Instrument, Price, Valuation
from investments.services.ledger import LedgerError, replay

URGENT, TODO, INFO = "urgent", "todo", "info"
ORDER = {URGENT: 0, TODO: 1, INFO: 2}
PRICE_STALE_DAYS = 7
MARK_STALE_DAYS = 120
CALL_SOON_DAYS = 14
APPROVAL_WAIT_DAYS = 3


@dataclass(frozen=True)
class Item:
    severity: str
    area: str
    title: str
    detail: str
    target: str  # page name for pages.page_url, e.g. "records:review"
    action: str  # button label
    args: tuple = ()  # type: ignore[type-arg]


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def collect(workspace: Workspace, today: date | None = None) -> list[Item]:
    today = today or timezone.localdate()
    items: list[Item] = []
    add = items.append
    accounts = Account.objects.filter(family__workspace=workspace)

    # Review
    held = Valuation.objects.filter(account__family__workspace=workspace, status=Valuation.Status.HELD).select_related("instrument")
    if held:
        names = ", ".join(sorted({str(v.instrument) for v in held})[:3])
        add(Item(URGENT, "Review", _plural(len(held), "mark") + " held for review",
                 f"{names}: moved further than expected, so not used in values until someone decides.", "records:review", "Review"))
    warned = ImportBatch.objects.filter(workspace=workspace, status=ImportBatch.Status.COMMITTED, reviewed_at__isnull=True,
                                        issues__severity=ImportIssue.Severity.WARNING).distinct().count()
    if warned:
        add(Item(TODO, "Review", _plural(warned, "import") + " with warnings to sign off", "Imported files whose warnings a second person should check.",
                 "records:review", "Sign off"))
    waiting = ImportBatch.objects.filter(workspace=workspace, status=ImportBatch.Status.VALIDATED).count()
    if waiting:
        add(Item(TODO, "Imports", _plural(waiting, "upload") + " checked but not imported", "Import or discard them so nothing is left half-done.",
                 "records:review", "Open"))
    recent_rejects = ImportBatch.objects.filter(workspace=workspace, status=ImportBatch.Status.REJECTED, created_at__gte=timezone.now() - timedelta(days=14))
    unresolved = [b for b in recent_rejects if not ImportBatch.objects.filter(workspace=workspace, status=ImportBatch.Status.COMMITTED,
                                                                              file_name=b.file_name, created_at__gt=b.created_at).exists()]
    if unresolved:
        add(Item(INFO, "Imports", _plural(len(unresolved), "rejected file") + " not re-imported",
                 ", ".join(sorted({b.file_name or b.reference for b in unresolved})[:3]) + ": fix the rows listed and upload again.", "investments:imports", "Imports"))

    # Capital calls
    open_calls = list(CapitalCall.objects.filter(commitment__account__family__workspace=workspace, status__in=CapitalCall.OPEN_STATUSES)
                      .select_related("commitment__account__entity", "commitment__instrument", "commitment__account__family"))
    overdue = [c for c in open_calls if c.due_date < today]
    if overdue:
        total = sum((c.amount for c in overdue), Decimal(0))
        add(Item(URGENT, "Capital calls", _plural(len(overdue), "capital call") + f" overdue (${total:,.0f})",
                 ", ".join(f"{c.commitment.instrument} due {c.due_date}" for c in overdue[:3]), "gl:calls", "Capital calls"))
    unsent = [c for c in open_calls if c.status in (CapitalCall.Status.RECEIVED, CapitalCall.Status.APPROVED) and today <= c.due_date <= today + timedelta(days=CALL_SOON_DAYS)]
    if unsent:
        blocked = [c for c in unsent if call_problems(c)]
        add(Item(URGENT if blocked else TODO, "Capital calls", _plural(len(unsent), "call") + f" due within {CALL_SOON_DAYS} days not sent to Campfire",
                 ("Blocked: " + call_problems(blocked[0])[0]) if blocked else ", ".join(f"{c.commitment.instrument} due {c.due_date}" for c in unsent[:3]),
                 "gl:calls", "Send bills"))
    awaiting = [c for c in open_calls if c.status == CapitalCall.Status.SUBMITTED]
    if awaiting:
        add(Item(INFO, "Capital calls", _plural(len(awaiting), "capital-call bill") + " awaiting approval in Campfire",
                 "Approve and pay them in Campfire; the app books the contribution when they're paid.", "gl:calls", "Check"))

    # Prices and marks
    stale_prices = []
    for inst in Instrument.objects.filter(workspace=workspace, is_unitized=True, transactions__account__family__workspace=workspace).distinct():
        holders = accounts.filter(transactions__instrument=inst).distinct()
        if not any(_holds(a, inst, today) for a in holders):
            continue
        p = Price.objects.filter(instrument=inst, as_of__lte=today).order_by("-as_of").first()
        if p is None or (today - p.as_of).days > PRICE_STALE_DAYS:
            stale_prices.append(f"{inst} ({p.as_of if p else 'never priced'})")
    if stale_prices:
        add(Item(TODO, "Investments", _plural(len(stale_prices), "security", "securities") + " without a recent price",
                 ", ".join(stale_prices[:4]) + ". Values use the last price.", "investments:imports", "Import prices"))
    old_marks = []
    for a in accounts.filter(tracks_cash=False):
        for inst_id in a.transactions.order_by().values_list("instrument_id", flat=True).distinct():  # order_by(): default ordering defeats distinct
            if inst_id is None:
                continue
            mark = Valuation.objects.filter(account=a, instrument_id=inst_id, status=Valuation.Status.ACCEPTED).order_by("-as_of").first()
            if mark is not None and (today - mark.as_of).days > MARK_STALE_DAYS:
                old_marks.append(f"{mark.instrument} ({mark.as_of})")
    if old_marks:
        add(Item(INFO, "Investments", _plural(len(old_marks), "holding") + f" with a mark older than {MARK_STALE_DAYS} days",
                 ", ".join(old_marks[:4]) + ". Record the latest statement or appraisal.", "records:new", "Add a mark", ("valuations",)))

    # GL
    booked = booked_entities(workspace)
    if booked:
        mapped = set(AccountMapping.objects.filter(workspace=workspace).values_list("role", flat=True))
        missing = [Role(r).label for r in Role if r not in mapped]
        if missing:
            add(Item(URGENT, "General ledger", _plural(len(missing), "posting role") + " not mapped to a GL account",
                     ", ".join(missing[:4]) + ".", "gl:gl", "Map accounts"))
        preview = build_preview(workspace, through=today)
        if preview.held:
            add(Item(URGENT, "General ledger", _plural(len(preview.held), "entry", "entries") + " held back", preview.held[0].errors[0], "gl:gl", "Fix"))
        if preview.ready:
            add(Item(TODO, "General ledger", _plural(len(preview.ready), "entry", "entries") + " ready to send to Campfire",
                     f"{sum(len(j.transactions) for j in preview.ready)} transactions not in the GL yet.", "gl:gl", "Review and send"))
    failed = JournalPosting.objects.filter(workspace=workspace, status__in=[JournalPosting.Status.FAILED, JournalPosting.Status.UNCERTAIN],
                                           created_at__gte=timezone.now() - timedelta(days=30))
    if failed.exists():
        add(Item(URGENT, "General ledger", _plural(failed.count(), "entry", "entries") + " failed or unanswered",
                 (failed.first().error or "Check them on the General ledger page.")[:160], "gl:gl", "Open"))
    slow = JournalPosting.objects.filter(workspace=workspace, status=JournalPosting.Status.SUBMITTED,
                                         submitted_at__lt=timezone.now() - timedelta(days=APPROVAL_WAIT_DAYS)).count()
    if slow:
        add(Item(INFO, "General ledger", _plural(slow, "entry", "entries") + f" awaiting approval in Campfire for over {APPROVAL_WAIT_DAYS} days",
                 "Approve or reject them in Campfire.", "gl:gl", "Check"))

    # Month-end close
    if booked:
        last = previous_month(today)
        closes = {c.campfire_entity_id: c for c in MonthClose.objects.filter(workspace=workspace, month=last)}
        open_entities = [cf for cf in booked if not (closes.get(cf) and closes[cf].signed_off_at)]
        if open_entities:
            add(Item(TODO, "Close", f"{last:%B} isn't closed for " + _plural(len(open_entities), "entity", "entities"),
                     ", ".join(booked[cf][0].name for cf in open_entities[:3]) + ".", "gl:close", f"Close {last:%B}"))
        breaks = [c for c in closes.values() if c.tieout_at and not c.tieout_ok]
        if breaks:
            add(Item(URGENT, "Close", "Tie-out doesn't tie for " + _plural(len(breaks), "entity", "entities") + f" ({last:%B})",
                     "; ".join(f"{d['label']}: {Decimal(d['difference']):,.2f}" for d in breaks[0].tieout_detail[:2]), "gl:close", "See the differences"))

    # Setup
    unlinked = Entity.objects.filter(family__workspace=workspace, campfire_entity_id__isnull=True, accounts__isnull=False).distinct()
    if unlinked.exists() and booked:
        add(Item(INFO, "Setup", _plural(unlinked.count(), "entity", "entities") + " with accounts not linked to Campfire",
                 ", ".join(e.name for e in unlinked[:3]) + ": their activity isn't posted to the GL.", "gl:gl", "Link"))
    no_number = accounts.filter(tracks_cash=True, external_id="").count()
    if no_number:
        add(Item(INFO, "Setup", _plural(no_number, "custody account") + " without a custodian account number",
                 "Custodian files can't be matched to them.", "records:list", "Accounts", ("accounts",)))
    no_vendor = Commitment.objects.filter(account__family__workspace=workspace, campfire_vendor_id__isnull=True)
    if no_vendor.exists() and booked:
        has_calls = no_vendor.filter(calls__status__in=CapitalCall.OPEN_STATUSES).exists()
        add(Item(TODO if has_calls else INFO, "Setup", _plural(no_vendor.count(), "fund") + " without a Campfire vendor (GP)",
                 "Capital calls can't be sent as bills until the GP is chosen.", "gl:calls", "Choose vendors"))

    return sorted(items, key=lambda i: ORDER[i.severity])


def _holds(account: Account, inst: Instrument, today: date) -> bool:
    try:
        return replay(account, today).quantity(inst.pk) > 0
    except LedgerError:
        return True


@dataclass
class Headline:
    families: int
    total: Decimal
    unfunded: Decimal
    calls_due_30: Decimal


def headline(workspace: Workspace, today: date | None = None) -> Headline:
    from entities.models import Family
    from investments.services.portfolio import build_portfolio

    today = today or timezone.localdate()
    total = unfunded = Decimal(0)
    families = list(Family.objects.filter(workspace=workspace, is_active=True))
    for f in families:
        p = build_portfolio(f, today)
        total += p.total
        unfunded += p.unfunded
    due = CapitalCall.objects.filter(commitment__account__family__workspace=workspace, status__in=CapitalCall.OPEN_STATUSES,
                                     due_date__lte=today + timedelta(days=30)).aggregate(s=Sum("amount"))["s"] or Decimal(0)
    return Headline(len(families), total, unfunded, due)

