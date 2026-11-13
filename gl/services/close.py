"""Month-end close, per Campfire entity: data loaded → reviewed → posted → approved in Campfire → ties → signed off.

Each step shows its evidence. Reviewed, posted and approved are worked out from the data; data-loaded is confirmed
by a person (custodian files arrive on their own schedule, and a month may have no activity); the tie-out runs
against Campfire's trial balance at month end and must be newer than the last entry posted; sign-off comes last,
from someone other than whoever sent that month's entries when preparer ≠ reviewer is on.
"""

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from campfire_integration.client import CampfireClient
from campfire_integration.models import Workspace
from entities.models import Entity
from gl.models import GLSettings, JournalPosting, MonthClose
from gl.services.posting import build_preview
from gl.services.tieout import tie_out
from investments.models import Account, ImportBatch, ImportIssue, Instrument, Price, Transaction, Valuation
from records.models import AuditEntry, ControlSettings

P = JournalPosting.Status
PRICE_STALE_DAYS = 7
MARK_STALE_DAYS = 120


class CloseRefused(Exception):
    pass


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def previous_month(d: date) -> date:
    return month_start(month_start(d) - timedelta(days=1))


@dataclass
class Step:
    key: str
    label: str
    done: bool
    evidence: list[str] = field(default_factory=list)
    problem: bool = False  # something is wrong (not just not done yet)


@dataclass
class EntityClose:
    campfire_entity_id: int
    label: str
    record: MonthClose
    steps: list[Step]

    @property
    def next_step(self) -> Step | None:
        return next((s for s in self.steps if not s.done), None)

    @property
    def closed(self) -> bool:
        return self.record.signed_off_at is not None


def booked_entities(workspace: Workspace) -> dict[int, list[Entity]]:
    out: dict[int, list[Entity]] = defaultdict(list)
    for e in Entity.objects.filter(family__workspace=workspace, campfire_entity_id__isnull=False).order_by("name"):
        out[e.campfire_entity_id].append(e)  # type: ignore[index]
    return dict(out)


def _record(workspace: Workspace, cf_entity: int, month: date) -> MonthClose:
    return MonthClose.objects.get_or_create(workspace=workspace, campfire_entity_id=cf_entity, month=month_start(month))[0]


def _data_step(rec: MonthClose, accounts: list[Account], start: date, end: date) -> Step:
    ev = []
    custody = [a for a in accounts if a.tracks_cash]
    if custody:
        in_month = Transaction.objects.filter(account__in=custody, trade_date__range=(start, end)).count()
        last = (ImportBatch.objects.filter(transactions__account__in=custody, status=ImportBatch.Status.COMMITTED, source="custodian_file")
                .aggregate(m=Max("committed_at"))["m"])
        ev.append(f"{in_month} custody transactions in the month; last custodian file imported {timezone.localdate(last) if last else 'never'}.")
        held = {t for t in Transaction.objects.filter(account__in=custody, instrument__is_unitized=True, trade_date__lte=end).values_list("instrument_id", flat=True)}
        stale = []
        for inst in Instrument.objects.filter(pk__in=held):
            p = Price.objects.filter(instrument=inst, as_of__lte=end).order_by("-as_of").first()
            if p is None or (end - p.as_of).days > PRICE_STALE_DAYS:
                stale.append(f"{inst} ({p.as_of if p else 'no price'})")
        if stale:
            ev.append("Month-end prices missing or stale: " + ", ".join(stale[:8]) + (" …" if len(stale) > 8 else "") + ".")
    marked = [a for a in accounts if not a.tracks_cash]
    for a in marked:
        mark = Valuation.objects.filter(account=a, as_of__lte=end, status=Valuation.Status.ACCEPTED).order_by("-as_of").first()
        if mark is None:
            ev.append(f"{a}: no accepted mark yet.")
        elif (end - mark.as_of).days > MARK_STALE_DAYS:
            ev.append(f"{a}: last mark {mark.as_of} ({(end - mark.as_of).days} days before month end).")
    if not accounts:
        ev.append("No accounts are booked in this entity.")
    done = rec.data_done_at is not None or not accounts
    if rec.data_done_at:
        ev.append(f"Confirmed complete by {rec.data_done_by} on {timezone.localdate(rec.data_done_at)}.")
    return Step("data", "Data loaded", done, ev)


def _review_step(accounts: list[Account], end: date) -> Step:
    held = Valuation.objects.filter(account__in=accounts, status=Valuation.Status.HELD, as_of__lte=end).count()
    unsigned = (ImportBatch.objects.filter(transactions__account__in=accounts, status=ImportBatch.Status.COMMITTED, reviewed_at__isnull=True,
                                           issues__severity=ImportIssue.Severity.WARNING).distinct().count())
    ev = []
    if held:
        ev.append(f"{held} mark{'s' if held != 1 else ''} held for review.")
    if unsigned:
        ev.append(f"{unsigned} import{'s' if unsigned != 1 else ''} with warnings not signed off.")
    return Step("review", "Reviewed", not held and not unsigned, ev or ["Nothing waiting for review."])


def _posted_step(workspace: Workspace, cf_entity: int, end: date) -> Step:
    preview = build_preview(workspace, through=end, campfire_entity_id=cf_entity)
    held_back = [j for j in preview.journals if not j.ok]
    ready = preview.ready
    ev = []
    if ready:
        market = sum(1 for j in ready if j.is_fair_value)
        ev.append(f"{len(ready)} entr{'ies' if len(ready) != 1 else 'y'} ready to send ({sum(len(j.transactions) for j in ready)} transactions"
                  + (f", {market} market value adjustment{'s' if market != 1 else ''}" if market else "") + ").")
    by_problem: dict[str, list[str]] = defaultdict(list)  # the same problem in several months is listed once
    for j in held_back:
        for e in j.errors:
            by_problem[e].append(f"{j.period_end:%b %Y}")
    for problem, months in by_problem.items():
        ev.append(f"Held back ({', '.join(months[:4])}{' …' if len(months) > 4 else ''}): {problem}")
    start = GLSettings.for_workspace(workspace).post_from
    if start and start > end:
        ev.append(f"Before the GL start date ({start}): nothing is posted for this month.")
    return Step("posted", "Posted to GL", not preview.journals, ev or ["Everything through month end is in the GL or awaiting approval."], problem=bool(held_back))


def _approved_step(workspace: Workspace, cf_entity: int, end: date) -> Step:
    waiting = JournalPosting.objects.filter(workspace=workspace, campfire_entity_id=cf_entity, je_date__lte=end,
                                            status__in=[P.SUBMITTED, P.UNCERTAIN])
    n = waiting.count()
    ev = [f"{n} entr{'ies' if n != 1 else 'y'} awaiting approval in Campfire: " + ", ".join(p.ref_number for p in waiting[:6])] if n else ["Nothing awaiting approval."]
    return Step("approved", "Approved in Campfire", n == 0, ev)


def _ties_step(workspace: Workspace, rec: MonthClose, cf_entity: int, end: date) -> Step:
    last_change = JournalPosting.objects.filter(workspace=workspace, campfire_entity_id=cf_entity, je_date__lte=end).aggregate(
        m=Max("posted_at"))["m"]
    if rec.tieout_at is None:
        return Step("ties", "Ties to GL", False, ["Not run for this month yet."])
    if last_change and last_change > rec.tieout_at:
        return Step("ties", "Ties to GL", False, ["Entries posted since the last tie-out: run it again."])
    if rec.tieout_ok:
        return Step("ties", "Ties to GL", True, [f"Tied on {timezone.localdate(rec.tieout_at)}."])
    ev = [f"{r['label']}: GL differs by {Decimal(r['difference']):,.2f}" for r in rec.tieout_detail[:6]] or ["Didn't tie."]
    return Step("ties", "Ties to GL", False, ev, problem=True)


def entity_close(workspace: Workspace, cf_entity: int, entities: list[Entity], month: date) -> EntityClose:
    start, end = month_start(month), month_end(month)
    rec = _record(workspace, cf_entity, start)
    accounts = list(Account.objects.filter(entity__in=entities).filter(Q(opened_on__isnull=True) | Q(opened_on__lte=end)))
    steps = [
        _data_step(rec, accounts, start, end),
        _review_step(accounts, end),
        _posted_step(workspace, cf_entity, end),
        _approved_step(workspace, cf_entity, end),
        _ties_step(workspace, rec, cf_entity, end),
    ]
    signed = rec.signed_off_at is not None
    steps.append(Step("signoff", "Signed off", signed,
                      [f"By {rec.signed_off_by} on {timezone.localdate(rec.signed_off_at)}." + (f" {rec.note}" if rec.note else "")] if signed else ["After every step above."]))
    return EntityClose(cf_entity, ", ".join(e.name for e in entities), rec, steps)


def month_status(workspace: Workspace, month: date) -> list[EntityClose]:
    return [entity_close(workspace, cf, ents, month) for cf, ents in sorted(booked_entities(workspace).items(), key=lambda kv: kv[1][0].name)]


# ---------------------------------------------------------------------------------------------- actions


def _audit(workspace: Workspace, actor: str, rec: MonthClose, what: str, value: str) -> None:
    AuditEntry.objects.create(workspace=workspace, actor=actor, action=AuditEntry.Action.UPDATED, kind="close",
                              object_id=str(rec.pk), object_repr=str(rec)[:255], changes={what: ["", value]})


def confirm_data(workspace: Workspace, cf_entity: int, month: date, actor: str) -> MonthClose:
    rec = _record(workspace, cf_entity, month)
    rec.data_done_by, rec.data_done_at = actor, timezone.now()
    rec.save(update_fields=["data_done_by", "data_done_at"])
    _audit(workspace, actor, rec, "data loaded", "confirmed")
    return rec


def run_tieout(workspace: Workspace, month: date, client: CampfireClient, actor: str = "") -> list[MonthClose]:
    end = month_end(month)
    result = tie_out(workspace, end, client)
    by_entity: dict[int, list] = defaultdict(list)
    for row in result.rows:
        by_entity[row.campfire_entity_id].append(row)
    saved = []
    for cf in booked_entities(workspace):
        rows = by_entity.get(cf, [])
        rec = _record(workspace, cf, month)
        rec.tieout_at = timezone.now()
        rec.tieout_ok = not result.errors and all(r.ties for r in rows)
        rec.tieout_detail = [{"label": r.label, "difference": str(r.difference)} for r in rows if not r.ties] + [
            {"label": e, "difference": "0"} for e in result.errors]
        rec.save(update_fields=["tieout_at", "tieout_ok", "tieout_detail"])
        saved.append(rec)
    return saved


def sign_off(workspace: Workspace, cf_entity: int, month: date, actor: str, actor_id: str, note: str = "") -> MonthClose:
    entities = booked_entities(workspace).get(cf_entity)
    if not entities:
        raise CloseRefused("That entity isn't booked in Campfire.")
    status = entity_close(workspace, cf_entity, entities, month)
    missing = [s.label for s in status.steps[:-1] if not s.done]
    if missing:
        raise CloseRefused("Not yet: " + ", ".join(missing) + ".")
    if ControlSettings.for_workspace(workspace).require_second_person:
        if not actor_id:
            raise CloseRefused("We can't tell who you are, so you can't sign off.")
        senders = set(JournalPosting.objects.filter(workspace=workspace, campfire_entity_id=cf_entity, period_end=month_end(month))
                      .exclude(created_by_id="").values_list("created_by_id", flat=True))
        if actor_id in senders:
            raise CloseRefused("You sent this month's entries, so someone else has to sign off (preparer ≠ reviewer).")
    with transaction.atomic():
        rec = _record(workspace, cf_entity, month)
        rec.signed_off_by, rec.signed_off_by_id, rec.signed_off_at, rec.note = actor, actor_id, timezone.now(), note
        rec.save(update_fields=["signed_off_by", "signed_off_by_id", "signed_off_at", "note"])
        _audit(workspace, actor, rec, "signed off", note or "yes")
    return rec


def reopen(workspace: Workspace, cf_entity: int, month: date, actor: str, reason: str) -> MonthClose:
    if not reason.strip():
        raise CloseRefused("Say why the month is reopened.")
    rec = _record(workspace, cf_entity, month)
    rec.signed_off_by, rec.signed_off_by_id, rec.signed_off_at = "", "", None
    rec.data_done_at, rec.data_done_by = None, ""
    rec.save()
    _audit(workspace, actor, rec, "reopened", reason)
    return rec
