"""Family-level roll-ups: investment net worth by entity, person (look-through), asset class, liquidity, tax treatment.

Only accounts are summed, never ownership interests between entities, so nothing is counted twice.
An entity's holdings are attributed to people through effective (look-through) ownership; a person's
own accounts are 100% theirs. Amounts are in the account currency; multi-currency translation is P2,
so totals cover the family's base currency (USD) and other currencies are reported separately.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from entities.models import Family, Person
from entities.services.ownership import effective_ownership
from investments.models import Account, AssetClass, Commitment
from investments.services.ledger import ZERO, Realized, replay
from investments.services.privates import CommitmentSummary, summarize
from investments.services.valuation import Holding, accounts_for, holdings

BASE_CURRENCY = "USD"
CENT = Decimal("0.01")
CASH_LABEL = "Cash & equivalents"
UNALLOCATED = "Unallocated (no family owner)"


@dataclass
class Portfolio:
    family: Family
    as_of: date
    holdings: list[Holding] = field(default_factory=list)
    realized: list[Realized] = field(default_factory=list)
    commitments: list[CommitmentSummary] = field(default_factory=list)
    by_entity: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    by_person: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    by_asset_class: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    by_liquidity: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    by_tax_treatment: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    other_currencies: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    income: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return sum(self.by_entity.values(), ZERO)

    @property
    def unfunded(self) -> Decimal:
        return sum((c.unfunded for c in self.commitments), ZERO)

    @property
    def stale(self) -> list[Holding]:
        return [h for h in self.holdings if h.is_stale]

    @property
    def realized_gain(self) -> Decimal:
        return sum((r.gain for r in self.realized), ZERO)


def _owner_label(account: Account) -> str:
    return account.entity.name if account.entity_id else f"{account.person.display_name} (individual)"


def build_portfolio(family: Family, as_of: date) -> Portfolio:
    p = Portfolio(family, as_of)
    shares = effective_ownership(family, as_of)
    people = {person.id: person.display_name for person in Person.objects.filter(family=family)}
    asset_classes = {a.id: a for a in AssetClass.objects.filter(workspace=family.workspace_id)}
    liquidity_labels = dict(AssetClass.Liquidity.choices)
    tax_labels = dict(Account.TaxTreatment.choices)

    commitments = {c.account_id: c for c in Commitment.objects.filter(account__family=family).select_related("instrument", "account")}

    for account in accounts_for(family, as_of):
        state = replay(account, as_of)
        p.realized += state.realized
        p.income += sum(state.income.values(), ZERO)
        account_holdings = holdings(account, as_of, state)
        p.holdings += account_holdings
        if account.pk in commitments:
            c = commitments[account.pk]
            match = next((h for h in account_holdings if h.instrument == c.instrument), None)
            p.commitments.append(summarize(c, as_of, match))
        if account.currency != BASE_CURRENCY:
            p.other_currencies[account.currency] += sum((h.market_value for h in account_holdings), ZERO)
            continue
        for h in account_holdings:
            value = h.market_value
            p.by_entity[_owner_label(account)] += value
            p.by_tax_treatment[tax_labels[account.tax_treatment]] += value
            if h.instrument is None:
                p.by_asset_class[CASH_LABEL] += value
                p.by_liquidity[liquidity_labels[AssetClass.Liquidity.LIQUID]] += value
            else:
                ac = asset_classes[h.instrument.asset_class_id]
                top = asset_classes.get(ac.parent_id, ac) if ac.parent_id else ac
                p.by_asset_class[top.name] += value
                p.by_liquidity[liquidity_labels[ac.liquidity]] += value
            if account.person_id:
                p.by_person[people[account.person_id]] += value
            else:
                for person_id, share in shares.get(account.entity_id, {None: Decimal(1)}).items():
                    p.by_person[people[person_id] if person_id else UNALLOCATED] += value * share

    # Look-through shares split values into fractions of a cent; round once at the end.
    for key in list(p.by_person):
        p.by_person[key] = p.by_person[key].quantize(CENT)
    return p
