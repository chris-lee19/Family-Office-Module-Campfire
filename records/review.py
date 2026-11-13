"""The review queue: what needs a second person's decision, and the decisions.

- Held marks: a NAV statement or appraisal that moved further than the threshold from the expected value (the last
  accepted mark rolled forward for cash flows) is held, not used. A reviewer accepts it, rejects it, or corrects it
  (the original is rejected and the corrected value is recorded; it goes through the same check).
- Imports with warnings: a custodian file or onboarding workbook that was imported with warnings (a journal booked
  as a fee, a large price move, ...) is signed off by a reviewer.
- Checked, not imported: uploads that passed the checks but were never imported or discarded.

Preparer ≠ reviewer (SEC-07): when `ControlSettings.require_second_person` is on, the person who recorded the mark
or uploaded the file can't decide on it, and a reviewer whose identity isn't known can't decide at all.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from campfire_integration.models import Workspace
from investments.models import ImportBatch, ImportIssue, Valuation
from investments.services.valuation import record_valuation, rolled_forward_value
from records.models import AuditEntry, ControlSettings

V = Valuation.Status
B = ImportBatch.Status


class ReviewRefused(Exception):
    pass


@dataclass(frozen=True)
class Reviewer:
    name: str
    id: str  # stable id (cfu:<campfire user id>); "" when unknown


@dataclass
class HeldMark:
    valuation: Valuation
    expected: Decimal | None
    previous_date: date | None


@dataclass
class Queue:
    held: list[HeldMark]
    warned: list[ImportBatch]  # imported with warnings, not signed off
    waiting: list[ImportBatch]  # checked, neither imported nor discarded
    require_second_person: bool

    @property
    def count(self) -> int:
        return len(self.held) + len(self.warned) + len(self.waiting)


def queue(workspace: Workspace) -> Queue:
    held = []
    for v in Valuation.objects.filter(account__family__workspace=workspace, status=V.HELD).select_related("account", "instrument").order_by("as_of", "id"):
        expected = rolled_forward_value(v.account, v.instrument, v.as_of)
        held.append(HeldMark(v, expected[0] if expected else None, expected[1].as_of if expected else None))
    warned = list(
        ImportBatch.objects.filter(workspace=workspace, status=B.COMMITTED, reviewed_at__isnull=True,
                                   issues__severity=ImportIssue.Severity.WARNING).distinct().order_by("committed_at")
    )
    waiting = list(ImportBatch.objects.filter(workspace=workspace, status=B.VALIDATED).order_by("created_at"))
    return Queue(held, warned, waiting, ControlSettings.for_workspace(workspace).require_second_person)


def _check(workspace: Workspace, reviewer: Reviewer, preparer_id: str, preparer: str) -> None:
    if not ControlSettings.for_workspace(workspace).require_second_person:
        return
    if not reviewer.id:
        raise ReviewRefused("We can't tell who you are, so you can't review. Open this page from Campfire, or sign in with Campfire.")
    if preparer_id and preparer_id == reviewer.id:
        raise ReviewRefused(f"You {preparer} this, so someone else has to review it (preparer ≠ reviewer).")


def _audit(workspace: Workspace, reviewer: Reviewer, kind: str, obj: Any, changes: dict[str, list[str]]) -> None:
    AuditEntry.objects.create(workspace=workspace, actor=reviewer.name, action=AuditEntry.Action.UPDATED, kind=kind,
                              object_id=str(obj.pk), object_repr=str(obj)[:255], changes=changes)


def _held(workspace: Workspace, valuation_id: int) -> Valuation:
    v = Valuation.objects.select_for_update().filter(account__family__workspace=workspace, pk=valuation_id).first()
    if v is None:
        raise ReviewRefused("That mark isn't in this workspace.")
    if v.status != V.HELD:
        raise ReviewRefused(f"That mark was already {v.get_status_display().lower()}.")
    return v


def _decide(v: Valuation, status: str, reviewer: Reviewer, note: str) -> None:
    v.status = status
    v.reviewed_by, v.reviewed_by_id, v.reviewed_at = reviewer.name, reviewer.id, timezone.now()
    v.review_note = note
    v.save(update_fields=["status", "reviewed_by", "reviewed_by_id", "reviewed_at", "review_note"])


def accept(workspace: Workspace, valuation_id: int, reviewer: Reviewer, note: str = "") -> Valuation:
    with transaction.atomic():
        v = _held(workspace, valuation_id)
        _check(workspace, reviewer, v.created_by_id, "recorded")
        _decide(v, V.ACCEPTED, reviewer, note)
        _audit(workspace, reviewer, "valuations", v, {"status": ["Held for review", "Accepted"], **({"review note": ["", note]} if note else {})})
        return v


def reject(workspace: Workspace, valuation_id: int, reviewer: Reviewer, note: str) -> Valuation:
    if not note.strip():
        raise ReviewRefused("Say why it's rejected.")
    with transaction.atomic():
        v = _held(workspace, valuation_id)
        _check(workspace, reviewer, v.created_by_id, "recorded")
        _decide(v, V.REJECTED, reviewer, note)
        _audit(workspace, reviewer, "valuations", v, {"status": ["Held for review", "Rejected"], "review note": ["", note]})
        return v


def correct(workspace: Workspace, valuation_id: int, value: Decimal, reviewer: Reviewer, note: str = "") -> tuple[Valuation, Valuation]:
    """Reject the held mark and record the corrected value, which goes through the usual check."""
    if value < 0:
        raise ReviewRefused("A value can't be negative.")
    with transaction.atomic():
        v = _held(workspace, valuation_id)
        _check(workspace, reviewer, v.created_by_id, "recorded")
        reason = f"Corrected to {value}" + (f": {note}" if note else "")
        _decide(v, V.REJECTED, reviewer, reason)
        new = record_valuation(account=v.account, instrument=v.instrument, as_of=v.as_of, value=value, method=v.method,
                               batch=v.batch, source_ref=v.source_ref, created_by=reviewer.name, created_by_id=reviewer.id)
        new.review_note = f"Correction of {v.value} (mark {v.pk})" + (f": {note}" if note else "")
        new.save(update_fields=["review_note"])
        _audit(workspace, reviewer, "valuations", v, {"status": ["Held for review", "Rejected"], "value": [str(v.value), f"corrected to {value}"]})
        return v, new


def sign_off(workspace: Workspace, batch_id: int, reviewer: Reviewer, note: str = "") -> ImportBatch:
    with transaction.atomic():
        batch = ImportBatch.objects.select_for_update().filter(workspace=workspace, pk=batch_id).first()
        if batch is None:
            raise ReviewRefused("That import isn't in this workspace.")
        if batch.status != B.COMMITTED or batch.reviewed_at is not None:
            raise ReviewRefused("That import doesn't need a sign-off.")
        _check(workspace, reviewer, batch.created_by_id, "uploaded")
        batch.reviewed_by, batch.reviewed_by_id, batch.reviewed_at, batch.review_note = reviewer.name, reviewer.id, timezone.now(), note
        batch.save(update_fields=["reviewed_by", "reviewed_by_id", "reviewed_at", "review_note"])
        _audit(workspace, reviewer, "imports", batch, {"signed off": ["", reviewer.name], **({"review note": ["", note]} if note else {})})
        return batch


def set_policy(workspace: Workspace, require_second_person: bool, reviewer: Reviewer) -> ControlSettings:
    settings = ControlSettings.for_workspace(workspace)
    if settings.require_second_person != require_second_person:
        _audit(workspace, reviewer, "settings", settings, {"preparer ≠ reviewer": [str(settings.require_second_person), str(require_second_person)]})
        settings.require_second_person = require_second_person
        settings.save()
    return settings
