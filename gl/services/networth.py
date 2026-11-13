"""Net worth = the investment book (at market value) + what Campfire holds for each booked entity: bank cash, other
assets, and liabilities (loans, payables, taxes), read from its trial balance (Reporting).

Accounts the GL bridge already mirrors (custody cash, clearing, investments at cost, unrealized appreciation: every
account mapped to a posting role) are left out: their value comes from the investment book, at market value, so
counting them again would double them. Income, expense and equity accounts aren't assets or liabilities.

Balances are read into a dated snapshot (`refresh_balances`), so pages don't call Campfire on every view. Each
entity's balances go to people by look-through ownership, like its investments.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from campfire_integration.client import CampfireClient
from campfire_integration.models import Workspace
from entities.models import Entity, Family, Person
from entities.services.ownership import effective_ownership
from gl.models import AccountMapping, CampfireBalanceSnapshot
from gl.services.close import booked_entities
from gl.services.setup import list_accounts
from gl.services.tieout import TB_PATH
from investments.services.portfolio import UNALLOCATED, Portfolio, build_portfolio

ZERO = Decimal(0)
CENT = Decimal("0.01")
CASH, OTHER, LIABILITY = "cash", "other", "liability"
LABELS = {CASH: "Bank cash (Campfire)", OTHER: "Other assets (Campfire)", LIABILITY: "Liabilities (Campfire)"}


def refresh_balances(workspace: Workspace, client: CampfireClient, as_of: date) -> list[CampfireBalanceSnapshot]:
    """Read every booked entity's balance-sheet balances at `as_of` from Campfire."""
    subtypes = {int(a["id"]): a.get("account_subtype") or "" for a in list_accounts(client)}
    saved = []
    for cf in booked_entities(workspace):
        data = client.get_json(TB_PATH, params={"start_date": as_of.replace(month=1, day=1).isoformat(), "end_date": as_of.isoformat(), "entity": cf})
        lines = []
        for a in data.get("trial_balance", {}).get("accounts", []):
            if a.get("account_type") not in ("ASSET", "LIABILITY") or not str(a.get("id", "")).isdigit():
                continue
            end = a.get("ending_balances") or a.get("balances") or {}
            net = (Decimal(str(end.get("debits") or 0)) - Decimal(str(end.get("credits") or 0))).quantize(CENT)
            if net:
                lines.append({"account_id": int(a["id"]), "number": a.get("number") or "", "name": a.get("name") or "",
                              "account_type": a["account_type"], "subtype": subtypes.get(int(a["id"]), ""), "net": str(net)})
        snap, _ = CampfireBalanceSnapshot.objects.update_or_create(workspace=workspace, campfire_entity_id=cf, as_of=as_of, defaults={"lines": lines})
        saved.append(snap)
    return saved


@dataclass
class CampfireLine:
    entity: Entity
    account_id: int
    label: str
    category: str
    amount: Decimal  # positive: an asset's value or a liability's size


@dataclass
class NetWorth:
    portfolio: Portfolio
    lines: list[CampfireLine] = field(default_factory=list)
    by_person: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    fetched_at: datetime | None = None
    balances_as_of: date | None = None
    missing: list[str] = field(default_factory=list)  # booked entities with no balances read yet

    def total(self, category: str) -> Decimal:
        return sum((ln.amount for ln in self.lines if ln.category == category), ZERO)

    @property
    def investments(self) -> Decimal:
        return self.portfolio.total

    @property
    def net(self) -> Decimal:
        return self.investments + self.total(CASH) + self.total(OTHER) - self.total(LIABILITY)


def classify(line: dict[str, Any]) -> str:
    if line["account_type"] == "LIABILITY":
        return LIABILITY
    return CASH if line.get("subtype") == "BANK" else OTHER


def build_net_worth(family: Family, as_of: date) -> NetWorth:
    nw = NetWorth(build_portfolio(family, as_of))
    for person, value in nw.portfolio.by_person.items():
        nw.by_person[person] += value
    mirrored = set(AccountMapping.objects.filter(workspace=family.workspace).values_list("campfire_account_id", flat=True))
    shares = effective_ownership(family, as_of)
    people = {p.id: p.display_name for p in Person.objects.filter(family=family)}
    booked = Entity.objects.filter(family=family, campfire_entity_id__isnull=False).order_by("name")
    seen: set[int] = set()
    for entity in booked:
        if entity.campfire_entity_id in seen:
            continue  # several FO entities booked in one Campfire entity: its balances count once, on the first
        seen.add(entity.campfire_entity_id)  # type: ignore[arg-type]
        snap = (CampfireBalanceSnapshot.objects.filter(workspace=family.workspace, campfire_entity_id=entity.campfire_entity_id, as_of__lte=as_of)
                .order_by("-as_of").first())
        if snap is None:
            nw.missing.append(entity.name)
            continue
        nw.fetched_at = max(filter(None, [nw.fetched_at, snap.fetched_at]))
        nw.balances_as_of = max(filter(None, [nw.balances_as_of, snap.as_of]))
        for raw in snap.lines:
            if raw["account_id"] in mirrored:
                continue
            category = classify(raw)
            net = Decimal(raw["net"])
            amount = -net if category == LIABILITY else net
            nw.lines.append(CampfireLine(entity, raw["account_id"], f"{raw['number']} {raw['name']}".strip(), category, amount))
            signed = -amount if category == LIABILITY else amount
            for person_id, share in shares.get(entity.pk, {None: Decimal(1)}).items():
                nw.by_person[people[person_id] if person_id else UNALLOCATED] += signed * share
    for key in list(nw.by_person):
        nw.by_person[key] = nw.by_person[key].quantize(CENT)
    return nw
