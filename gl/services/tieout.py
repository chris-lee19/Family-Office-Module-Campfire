"""Subledger-to-GL tie-out (REC-04): the investment book vs Campfire's GL, per Campfire entity and GL account.

For the balance-sheet roles (custody cash, investments at cost) the FO balance is computed from the ledger
replay of every account booked in that Campfire entity. Part of it may not be in the GL yet: transactions not
posted, and entries waiting for approval in Campfire. What should be in the GL is the FO balance less those;
the difference against Campfire's trial balance is what needs explaining (an entry booked in Campfire by hand,
a deleted entry, a mapping pointing at an account also used for something else).
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from campfire_integration.client import CampfireClient
from campfire_integration.models import Workspace
from gl.models import TIED_OUT_ROLES, JournalPosting, Role
from gl.services.posting import Mappings, build_preview
from investments.models import Account, Instrument
from investments.services.ledger import LedgerError, replay

ZERO = Decimal(0)
CENT = Decimal("0.01")
TB_PATH = "/ca/api/get_trial_balance"


@dataclass
class TieRow:
    campfire_entity_id: int
    campfire_account_id: int
    label: str
    fo_balance: Decimal = ZERO
    unposted: Decimal = ZERO  # FO activity not sent to Campfire yet
    awaiting_approval: Decimal = ZERO  # sent, not posted yet
    gl_balance: Decimal | None = None

    @property
    def expected_gl(self) -> Decimal:
        return self.fo_balance - self.unposted - self.awaiting_approval

    @property
    def difference(self) -> Decimal | None:
        return None if self.gl_balance is None else self.gl_balance - self.expected_gl

    @property
    def ties(self) -> bool:
        return self.difference is not None and abs(self.difference) < CENT


@dataclass
class TieOut:
    as_of: date
    rows: list[TieRow]
    errors: list[str]

    @property
    def all_tie(self) -> bool:
        return bool(self.rows) and not self.errors and all(r.ties for r in self.rows)


def fo_balances(workspace: Workspace, as_of: date) -> tuple[dict[tuple[int, int], TieRow], list[str]]:
    mappings = Mappings(workspace)
    rows: dict[tuple[int, int], TieRow] = {}
    errors: list[str] = []

    def add(cf_entity: int, role: str, account: Account, asset_class_id: int | None, amount: Decimal) -> None:
        m = mappings.resolve(role, account=account, campfire_entity_id=cf_entity, asset_class_id=asset_class_id)
        if m is None:
            errors.append(f"No GL account mapped for {Role(role).label} ({account.name}).")
            return
        row = rows.setdefault((cf_entity, m.campfire_account_id), TieRow(cf_entity, m.campfire_account_id, m.campfire_account_label))
        row.fo_balance += amount

    accounts = Account.objects.filter(family__workspace=workspace, entity__campfire_entity_id__isnull=False).select_related("entity")
    for account in accounts:
        cf_entity = account.entity.campfire_entity_id  # type: ignore[union-attr]
        try:
            state = replay(account, as_of)
        except LedgerError as e:
            errors.append(f"{account.name}: {e}")
            continue
        if account.tracks_cash and state.cash:
            add(cf_entity, Role.CASH, account, None, state.cash)  # type: ignore[arg-type]
        for instrument_id, instrument in Instrument.objects.in_bulk(state.instrument_ids()).items():
            add(cf_entity, Role.INVESTMENTS, account, instrument.asset_class_id, state.cost_basis(instrument_id))  # type: ignore[arg-type]
    return rows, list(dict.fromkeys(errors))


def gl_balances(client: CampfireClient, cf_entity: int, as_of: date) -> dict[int, Decimal]:
    data = client.get_json(TB_PATH, params={"start_date": as_of.replace(month=1, day=1).isoformat(), "end_date": as_of.isoformat(), "entity": cf_entity})
    out: dict[int, Decimal] = {}
    for a in data.get("trial_balance", {}).get("accounts", []):
        end = a.get("ending_balances") or a.get("balances") or {}
        out[int(a["id"])] = (Decimal(str(end.get("debits") or 0)) - Decimal(str(end.get("credits") or 0))).quantize(CENT)
    return out


def tie_out(workspace: Workspace, as_of: date, client: CampfireClient) -> TieOut:
    rows, errors = fo_balances(workspace, as_of)
    tied_roles = {str(r) for r in TIED_OUT_ROLES}

    # Not in the GL yet: unposted activity, and entries awaiting approval.
    preview = build_preview(workspace, through=as_of)
    for j in preview.journals:
        for leg, mapping, _ in j.legs:
            if mapping is not None and leg.role in tied_roles and (j.campfire_entity_id, mapping.campfire_account_id) in rows:
                rows[(j.campfire_entity_id, mapping.campfire_account_id)].unposted += leg.amount
    waiting = JournalPosting.objects.filter(workspace=workspace, status__in=[JournalPosting.Status.SUBMITTED, JournalPosting.Status.UNCERTAIN], je_date__lte=as_of)
    for p in waiting:
        for ln in p.lines:
            key = (p.campfire_entity_id, int(ln["account"]))
            if key in rows:
                rows[key].awaiting_approval += Decimal(ln["debit"]) - Decimal(ln["credit"])

    for cf_entity in sorted({k[0] for k in rows}):
        balances = gl_balances(client, cf_entity, as_of)
        for (entity_id, account_id), row in rows.items():
            if entity_id == cf_entity:
                row.gl_balance = balances.get(account_id, ZERO)
    ordered = sorted(rows.values(), key=lambda r: (r.campfire_entity_id, r.label))
    return TieOut(as_of, ordered, errors)
