"""Partnership accounting and K-1s.

Capital accounts: for an entity taxed as a partnership, each partner's opening capital, contributions, share of
income, distributions and closing capital for a year. The entity's totals come from its books in Campfire (trial
balance); they are split between the partners by their ownership percentages on the last day of the period. The
ledger carries no partner dimension, so special allocations and uneven contributions aren't reflected: the split is
pro rata, and says so.

K-1s to issue: the same year's income by kind (ordinary, interest, dividends, capital gains, rental), per partner.
K-1s to receive: one expected per partnership interest the family holds and tax year, with what has arrived.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from campfire_integration.client import CampfireClient
from campfire_integration.models import Workspace
from entities.models import Entity
from entities.services.ownership import active_interests
from gl.services.statements import TRIAL_BALANCE_PATH
from investments.models import K1, Account, Instrument, Transaction

ZERO, CENT = Decimal(0), Decimal("0.01")
INCOME_TYPES, EXPENSE_TYPES = ("REVENUE", "OTHER_INCOME"), ("COGS", "OPERATING_EXPENSES", "OTHER_EXPENSE")
PARTNERSHIP_INTERESTS = (Instrument.Kind.PRIVATE_FUND, Instrument.Kind.DIRECT_PRIVATE)
# How an income account shows on a K-1, by what its name says (first match wins).
K1_LINES = (("interest", "Interest income"), ("dividend", "Dividends"), ("gain", "Capital gains (losses)"), ("rent", "Rental income"))
ORDINARY, DEDUCTIONS = "Ordinary income", "Deductions"


@dataclass
class Partner:
    name: str
    percentage: Decimal
    opening: Decimal = ZERO
    contributions: Decimal = ZERO
    income: Decimal = ZERO
    distributions: Decimal = ZERO
    k1: dict[str, Decimal] = field(default_factory=dict)

    @property
    def closing(self) -> Decimal:
        return self.opening + self.contributions + self.income - self.distributions


@dataclass
class CapitalStatement:
    entity: Entity
    start: date
    end: date
    partners: list[Partner]
    k1_lines: list[str]
    other_changes: Decimal = ZERO  # what the books moved that isn't income, a contribution or a distribution

    def total(self, name: str) -> Decimal:
        return sum((getattr(p, name) for p in self.partners), ZERO)

    def k1_total(self, line: str) -> Decimal:
        return sum((p.k1.get(line, ZERO) for p in self.partners), ZERO)


def partnerships(workspace: Workspace) -> list[Entity]:
    """Entities whose income passes through to partners or members."""
    T, X = Entity.EntityType, Entity.TaxClassification
    qs = Entity.objects.filter(family__workspace=workspace).select_related("family").order_by("name")
    return [e for e in qs if e.tax_classification == X.PARTNERSHIP or e.entity_type in (T.LIMITED_PARTNERSHIP, T.GENERAL_PARTNERSHIP)]


def partners_of(entity: Entity, as_of: date) -> list[tuple[str, Decimal]]:
    out = []
    for i in active_interests(entity.family, as_of):
        if i.owned_id == entity.pk:
            out.append((i.owner_entity.name if i.owner_entity_id else i.owner_person.display_name, Decimal(i.percentage)))
    return sorted(out, key=lambda p: (-p[1], p[0]))


def _split(amount: Decimal, shares: list[Decimal]) -> list[Decimal]:
    """Split to the cent in proportion to the shares; the rounding difference goes to the largest share."""
    total = sum(shares, ZERO)
    if not total:
        return [ZERO for _ in shares]
    parts = [(amount * s / total).quantize(CENT) for s in shares]
    parts[shares.index(max(shares))] += amount - sum(parts, ZERO)
    return parts


def _k1_line(name: str) -> str:
    lower = name.lower()
    return next((label for needle, label in K1_LINES if needle in lower), ORDINARY)


def capital_accounts(client: CampfireClient, entity: Entity, year: int, today: date) -> CapitalStatement:
    start, end = date(year, 1, 1), min(date(year, 12, 31), today)
    data = client.get_json(TRIAL_BALANCE_PATH, params={"entity": entity.campfire_entity_id, "start_date": start.isoformat(), "end_date": end.isoformat()})
    net = lambda b: Decimal(str((b or {}).get("debits") or 0)) - Decimal(str((b or {}).get("credits") or 0))  # noqa: E731  (debit positive)
    opening = closing = contributions = distributions = ZERO
    by_line: dict[str, Decimal] = {}
    for a in data.get("trial_balance", {}).get("accounts", []):
        kind, activity = a.get("account_type"), a.get("activity") or {}
        if a.get("id") == "total":
            continue
        if kind in ("ASSET", "LIABILITY"):
            opening += net(a.get("beginning_balances"))
            closing += net(a.get("ending_balances"))
        elif kind == "EQUITY":
            contributions += Decimal(str(activity.get("credits") or 0))
            distributions += Decimal(str(activity.get("debits") or 0))
        elif kind in INCOME_TYPES:
            line = _k1_line(a.get("name") or "")
            by_line[line] = by_line.get(line, ZERO) - net(activity)
        elif kind in EXPENSE_TYPES:
            by_line[DEDUCTIONS] = by_line.get(DEDUCTIONS, ZERO) - net(activity)
    owners = partners_of(entity, end)
    held = sum((p for _, p in owners), ZERO)
    if held < 100:
        owners.append(("Not assigned to a partner", Decimal(100) - held))
    shares = [p for _, p in owners]
    partners = [Partner(name, pct) for name, pct in owners]
    q = lambda v: Decimal(v).quantize(CENT)  # noqa: E731
    income = sum(by_line.values(), ZERO)
    for attr, amount in (("opening", opening), ("contributions", contributions), ("income", income), ("distributions", distributions)):
        for partner, part in zip(partners, _split(q(amount), shares)):
            setattr(partner, attr, part)
    order = [ORDINARY] + [label for _, label in K1_LINES] + [DEDUCTIONS]
    lines = [ln for ln in order if q(by_line.get(ln, ZERO))]
    for ln in lines:
        for partner, part in zip(partners, _split(q(by_line[ln]), shares)):
            partner.k1[ln] = part
    other = q(closing) - sum((p.closing for p in partners), ZERO)
    return CapitalStatement(entity, start, end, partners, lines, other)


# ---------------------------------------------------------------------------------------------- K-1s to receive


@dataclass
class ExpectedK1:
    account: Account
    instrument: Instrument
    tax_year: int
    record: K1 | None

    @property
    def status(self) -> str:
        return self.record.status if self.record else K1.Status.EXPECTED

    @property
    def received(self) -> bool:
        return self.status != K1.Status.EXPECTED


def tax_years(workspace: Workspace, today: date) -> list[int]:
    first = (Transaction.objects.filter(account__family__workspace=workspace, instrument__kind__in=PARTNERSHIP_INTERESTS)
             .order_by("trade_date").values_list("trade_date", flat=True).first())
    last = today.year - 1
    return list(range(last, max((first.year if first else last), last - 6) - 1, -1))


def expected_k1s(workspace: Workspace, tax_year: int) -> list[ExpectedK1]:
    """One K-1 for each partnership interest that had activity up to the end of the tax year."""
    pairs = (Transaction.objects.filter(account__family__workspace=workspace, instrument__kind__in=PARTNERSHIP_INTERESTS,
                                        trade_date__year__lte=tax_year).order_by().values_list("account_id", "instrument_id").distinct())
    records = {(k.account_id, k.instrument_id): k for k in K1.objects.filter(account__family__workspace=workspace, tax_year=tax_year)}
    accounts = {a.pk: a for a in Account.objects.filter(family__workspace=workspace).select_related("entity", "person")}
    instruments = Instrument.objects.in_bulk({i for _, i in pairs})
    out = [ExpectedK1(accounts[a], instruments[i], tax_year, records.get((a, i))) for a, i in pairs if a in accounts and i in instruments]
    return sorted(out, key=lambda k: (k.received, k.instrument.name, k.account.name))


def record_k1(workspace: Workspace, data: Any, actor: str) -> K1:
    as_int = lambda v: int(v) if str(v or "").isdigit() else 0  # noqa: E731
    account = Account.objects.filter(family__workspace=workspace, pk=as_int(data.get("account"))).first()
    instrument = Instrument.objects.filter(workspace=workspace, pk=as_int(data.get("instrument"))).first()
    year, status = as_int(data.get("tax_year")), str(data.get("status") or "")
    if account is None or instrument is None or not 2000 <= year <= 2100 or status not in K1.Status.values:
        raise ValueError("Choose the K-1 and its status.")

    def money(key: str) -> Decimal:
        text = str(data.get(key) or "").replace(",", "").replace("$", "").replace("(", "-").replace(")", "").strip()
        try:
            return Decimal(text or "0").quantize(CENT)
        except ArithmeticError:
            raise ValueError("Enter the amounts as numbers.") from None

    received = None
    if status != K1.Status.EXPECTED:
        try:
            received = date.fromisoformat(str(data.get("received_on") or ""))
        except ValueError:
            raise ValueError("Enter the date the K-1 arrived.") from None
    k1, _ = K1.objects.update_or_create(account=account, instrument=instrument, tax_year=year, defaults={
        "status": status, "received_on": received, "ordinary_income": money("ordinary_income"), "interest_income": money("interest_income"),
        "dividend_income": money("dividend_income"), "capital_gains": money("capital_gains"), "other_income": money("other_income"),
        "distributions": money("distributions"), "note": str(data.get("note") or "").strip()[:255], "recorded_by": actor})
    return k1
