"""Market value in the GL: a month-end entry per Campfire entity that brings unrealized appreciation to market value
less cost.

The books carry investments at cost (gl.services.rules). When the workspace books at market value
(`GLSettings.fair_value_from`), each month-end gets a true-up:

    Dr Unrealized appreciation (balance sheet) / Cr Unrealized gain or loss (P&L)     when holdings gained
    Dr Unrealized gain or loss (P&L) / Cr Unrealized appreciation (balance sheet)     when they lost

per holding, for the difference between its unrealized amount at the month end and what entries still open have
already booked for it (`FairValueItem`). So the first entry brings the books from cost to market, a sold holding's
unrealized balance comes back out the month it is sold (its gain is then realized by the cost-relief rule), and a
month that was skipped or released is caught up by the next one. Values are the ones the app reports: held marks
are not used until someone reviews them.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

from django.db.models import Max, Sum

from campfire_integration.models import Workspace
from gl.models import FairValueItem, GLSettings, JournalPosting, Role
from investments.models import Account, Instrument, Transaction
from investments.services.ledger import LedgerError, replay
from investments.services.portfolio import BASE_CURRENCY
from investments.services.valuation import holdings

ZERO = Decimal(0)
CENT = Decimal("0.01")
Key = tuple[int, int]  # (account id, instrument id)


@dataclass
class FairValueLine:
    account: Account
    instrument: Instrument
    cost_basis: Decimal
    market_value: Decimal
    unrealized: Decimal  # at the period end
    adjustment: Decimal  # booked by this entry


def month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def next_month_end(d: date) -> date:
    return month_end(d + timedelta(days=1))


def last_month_end(as_of: date) -> date:
    """The latest month end on or before `as_of`."""
    return as_of if month_end(as_of) == as_of else as_of.replace(day=1) - timedelta(days=1)


def first_period(workspace: Workspace) -> date | None:
    start = GLSettings.for_workspace(workspace).fair_value_from
    return month_end(start) if start else None


def booked(workspace: Workspace) -> dict[Key, Decimal]:
    """Unrealized amount in the GL (or on its way there) per holding: the sum of open entries' adjustments."""
    rows = (FairValueItem.objects.filter(posting__workspace=workspace, posting__status__in=JournalPosting.OPEN_STATUSES)
            .values("account_id", "instrument_id").annotate(total=Sum("adjustment")))
    return {(r["account_id"], r["instrument_id"]): r["total"] for r in rows}


def unrealized_at(account: Account, as_of: date) -> dict[int, tuple[Instrument, Decimal, Decimal]]:
    """instrument id -> (instrument, cost, market value) for the account's holdings on a date."""
    out = {}
    for h in holdings(account, as_of, replay(account, as_of)):
        if h.instrument is not None:
            out[h.instrument.pk] = (h.instrument, h.cost_basis.quantize(CENT), h.market_value.quantize(CENT))
    return out


def booked_accounts(workspace: Workspace, campfire_entity_id: int | None = None) -> dict[int, list[Account]]:
    qs = (Account.objects.filter(family__workspace=workspace, entity__campfire_entity_id__isnull=False, currency=BASE_CURRENCY)
          .select_related("entity__branch", "family"))
    if campfire_entity_id is not None:
        qs = qs.filter(entity__campfire_entity_id=campfire_entity_id)
    by_entity: dict[int, list[Account]] = defaultdict(list)
    for a in qs:
        by_entity[a.entity.campfire_entity_id].append(a)  # type: ignore[union-attr]
    return by_entity


def proposals(workspace: Workspace, *, through: date, mappings: Any, tags: Any, campfire_entity_id: int | None = None) -> list[Any]:
    """The market value entries not sent yet: one per Campfire entity and month end, oldest first."""
    from gl.services.posting import ProposedJournal
    from gl.services.rules import Leg

    start = first_period(workspace)
    if start is None:
        return []
    last_sent = dict(JournalPosting.objects.filter(workspace=workspace, kind=JournalPosting.Kind.FAIR_VALUE, status__in=JournalPosting.OPEN_STATUSES)
                     .values_list("campfire_entity_id").annotate(Max("period_end")))
    in_gl = booked(workspace)
    out = []
    for cf_entity, accounts in booked_accounts(workspace, campfire_entity_id).items():
        account_by_id = {a.pk: a for a in accounts}
        running = {k: v for k, v in in_gl.items() if k[0] in account_by_id}
        period = max(start, next_month_end(last_sent[cf_entity])) if cf_entity in last_sent else start
        blocked = ""
        while period <= through:
            j = ProposedJournal(cf_entity, period, period, kind=JournalPosting.Kind.FAIR_VALUE)
            j.entities.update(a.entity.name for a in accounts)  # type: ignore[union-attr]
            j.families.update(a.family.name for a in accounts)
            values: dict[Key, tuple[Instrument, Decimal, Decimal]] = {}
            for account in accounts:
                try:
                    for instrument_id, row in unrealized_at(account, period).items():
                        values[(account.pk, instrument_id)] = row
                except LedgerError as e:
                    j.errors.append(f"{account.name}: the ledger doesn't replay ({e}).")
            gone = Instrument.objects.in_bulk([k[1] for k in running if k not in values and running[k]])
            for key in sorted(set(values) | {k for k in running if running[k]}):
                account = account_by_id[key[0]]
                instrument, cost, value = values.get(key) or (gone[key[1]], ZERO, ZERO)
                target = value - cost
                adjustment = target - running.get(key, ZERO)
                if not adjustment:
                    continue
                running[key] = target
                j.fair_values.append(FairValueLine(account, instrument, cost, value, target, adjustment))
                holding = cast(Transaction, SimpleNamespace(account=account, instrument=instrument))  # what the tags need
                for role, amount in ((Role.UNREALIZED, adjustment), (Role.UNREALIZED_GAIN, -adjustment)):
                    leg = Leg(role, amount, holding)
                    mapping = mappings.resolve(role, account=account, campfire_entity_id=cf_entity, asset_class_id=instrument.asset_class_id)
                    if mapping is None:
                        j.errors.append(f"No GL account mapped for {Role(role).label} ({instrument.asset_class}).")
                    j.legs.append((leg, mapping, tags.for_leg(leg, j.warnings)))
            if blocked and (j.fair_values or j.errors):
                j.errors.append(f"Send the {blocked} market value entry first.")
            if j.errors and not blocked:
                blocked = f"{period:%B %Y}"
            if j.fair_values or j.errors:
                out.append(j)
            period = next_month_end(period)
    return out


def balances(workspace: Workspace, as_of: date) -> list[tuple[int, Account, Instrument, Decimal]]:
    """What unrealized appreciation should be in the GL on a date: each holding's unrealized amount at the latest
    month end on or before it (Campfire entity, account, instrument, amount). Empty before the first period."""
    start = first_period(workspace)
    period = last_month_end(as_of)
    if start is None or period < start:
        return []
    out = []
    for cf_entity, accounts in booked_accounts(workspace).items():
        for account in accounts:
            try:
                rows = unrealized_at(account, period)
            except LedgerError:
                continue  # the tie-out reports the ledger error for the account's other balances
            out += [(cf_entity, account, instrument, value - cost) for instrument, cost, value in rows.values() if value != cost]
    return out
