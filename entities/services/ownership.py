"""Look-through ownership: each person's effective economic share of each entity on a date.

Ownership is a directed graph (owner -> owned) with percentages. A person's effective share of an
entity is the sum over every path from the person to the entity of the product of the percentages
along it. For example, if a person owns 50% of an LLC that owns 40% of an LP, the person has 20% of
the LP through that path.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.db.models import Q

from entities.models import Branch, Entity, Family, OwnershipInterest

HUNDRED = Decimal(100)
ONE = Decimal(1)
TOLERANCE = Decimal("0.0001")  # in percentage points


class OwnershipCycleError(Exception):
    def __init__(self, entity_ids: list[int]) -> None:
        super().__init__(f"Ownership cycle through entities {entity_ids}")
        self.entity_ids = entity_ids


@dataclass(frozen=True)
class Edge:
    owned_id: int
    owner_entity_id: int | None
    owner_person_id: int | None
    fraction: Decimal  # 0..1


@dataclass(frozen=True)
class OwnershipIssue:
    entity: Entity
    total_percentage: Decimal

    @property
    def kind(self) -> str:
        return "over-allocated" if self.total_percentage > HUNDRED else "under-allocated"


def active_interests(family: Family, as_of: date):  # type: ignore[no-untyped-def]
    return OwnershipInterest.objects.filter(owned__family=family, effective_from__lte=as_of).filter(
        Q(effective_to__isnull=True) | Q(effective_to__gte=as_of)
    )


def _edges(family: Family, as_of: date) -> dict[int, list[Edge]]:
    by_owned: dict[int, list[Edge]] = defaultdict(list)
    for row in active_interests(family, as_of).values("owned_id", "owner_entity_id", "owner_person_id", "percentage"):
        by_owned[row["owned_id"]].append(
            Edge(row["owned_id"], row["owner_entity_id"], row["owner_person_id"], row["percentage"] / HUNDRED)
        )
    return by_owned


def effective_ownership(family: Family, as_of: date) -> dict[int, dict[int | None, Decimal]]:
    """{entity_id: {person_id: fraction}} for every entity in the family.

    Fractions are 0..1. Key `None` holds the share that does not trace to a person, either because
    the entity's owners total less than 100% or because a chain ends at an entity with no owners
    (e.g. a charitable foundation).
    """
    edges = _edges(family, as_of)
    memo: dict[int, dict[int | None, Decimal]] = {}
    visiting: list[int] = []

    def resolve(entity_id: int) -> dict[int | None, Decimal]:
        if entity_id in memo:
            return memo[entity_id]
        if entity_id in visiting:
            raise OwnershipCycleError(visiting[visiting.index(entity_id):] + [entity_id])
        visiting.append(entity_id)
        shares: dict[int | None, Decimal] = defaultdict(Decimal)
        allocated = Decimal(0)
        for edge in edges.get(entity_id, []):
            allocated += edge.fraction
            if edge.owner_person_id is not None:
                shares[edge.owner_person_id] += edge.fraction
            else:
                for person_id, share in resolve(edge.owner_entity_id).items():  # type: ignore[arg-type]
                    shares[person_id] += edge.fraction * share
        if allocated < ONE:
            shares[None] += ONE - allocated
        visiting.pop()
        memo[entity_id] = dict(shares)
        return memo[entity_id]

    for entity_id in Entity.objects.filter(family=family).values_list("id", flat=True):
        resolve(entity_id)
    return memo


def branch_ownership(family: Family, as_of: date) -> dict[int, dict[int | None, Decimal]]:
    """{entity_id: {branch_id: fraction}}: look-through ownership rolled up to family branches.

    People with no branch, and unallocated shares, roll up under `None`.
    """
    person_branch = dict(family.people.values_list("id", "branch_id"))
    result: dict[int, dict[int | None, Decimal]] = {}
    for entity_id, shares in effective_ownership(family, as_of).items():
        rolled: dict[int | None, Decimal] = defaultdict(Decimal)
        for person_id, share in shares.items():
            rolled[person_branch.get(person_id) if person_id is not None else None] += share
        result[entity_id] = dict(rolled)
    return result


def ownership_issues(family: Family, as_of: date) -> list[OwnershipIssue]:
    """Entities whose direct owners don't total 100%, for the data-quality view.

    Entities with no owners at all (e.g. foundations) are not flagged.
    """
    totals: dict[int, Decimal] = defaultdict(Decimal)
    for row in active_interests(family, as_of).values("owned_id", "percentage"):
        totals[row["owned_id"]] += row["percentage"]
    entities = Entity.objects.in_bulk(list(totals))
    return [
        OwnershipIssue(entities[entity_id], total)
        for entity_id, total in sorted(totals.items())
        if abs(total - HUNDRED) > TOLERANCE
    ]


def branch_names(family: Family) -> dict[int, str]:
    return dict(Branch.objects.filter(family=family).values_list("id", "name"))
