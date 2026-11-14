"""The demo budget for the fictional Alder family, set against the demo books (gl/management/commands/
campfire_demo_books.py) so Budget vs. Actual has something to say: most lines on budget, a few over (aviation, the
Seacliff Avenue residence, travel, legal fees nobody budgeted) and a few under (grants, property costs).

Only offered to a workspace whose family is the demo family (code "alder"). Fictional figures, like the rest of the demo.
"""

from decimal import Decimal
from typing import Any

from campfire_integration.client import CampfireClient
from campfire_integration.models import Workspace
from entities.models import Family
from gl.models import Budget, FamilyProperty
from gl.services import budget as svc
from gl.services import statements as st

RRT, ERT, DYNASTY, GST = "Robert Alder Revocable Trust", "Eleanor Alder Revocable Trust", "Alder 2012 Dynasty Trust", "Claire Moreno 2019 GST Trust"
MGMT, LP, ARE, FOUNDATION = "Alder Management LLC", "Alder Family Investments LP", "Alder Real Estate Holdings LLC", "Alder Family Foundation"

# (entity, account name, how): ("even", annual) | ("quarterly", annual) | ("months", {month: amount}) | ("base", monthly, {month: extra})
PLAN: list[tuple[str, str, tuple[Any, ...]]] = [
    (RRT, "Household staff", ("even", 198_000)),
    (RRT, "Residence operating costs", ("even", 105_000)),
    (RRT, "Aviation", ("even", 180_000)),
    (RRT, "Clubs and memberships", ("months", {1: 24_000})),
    (RRT, "Travel", ("even", 40_000)),
    (RRT, "Professional and advisory fees", ("quarterly", 38_000)),
    (RRT, "Interest income", ("months", {6: 12_500, 12: 12_500})),
    (ERT, "Residence operating costs", ("base", 3_200, {6: 4_800, 7: 4_800, 8: 4_800})),
    (ERT, "Household staff", ("months", {6: 6_500, 7: 6_500, 8: 6_500})),
    (ERT, "Travel", ("months", {2: 20_000, 7: 30_000})),
    (ERT, "Clubs and memberships", ("months", {1: 8_500})),
    (ERT, "Insurance expense", ("months", {2: 14_000})),
    (ERT, "Professional and advisory fees", ("quarterly", 28_000)),
    (GST, "Education", ("months", {1: 28_500, 8: 28_500})),
    (GST, "Travel", ("months", {5: 10_000})),
    (GST, "Legal fees", ("months", {2: 2_500})),
    (GST, "Tax preparation and accounting fees", ("months", {4: 3_800})),
    (DYNASTY, "Trustee fees", ("quarterly", 44_000)),
    (DYNASTY, "Tax preparation and accounting fees", ("months", {4: 6_500, 10: 4_800})),
    (DYNASTY, "Clubs and memberships", ("months", {1: 4_000})),
    (MGMT, "Professional and advisory fees", ("even", 102_000)),
    (MGMT, "Bank and custody fees", ("even", 1_800)),
    (MGMT, "Management fee income - related parties", ("quarterly", 120_000)),
    (LP, "Management fees - related party", ("quarterly", 60_000)),
    (LP, "Tax preparation and accounting fees", ("months", {3: 18_500})),
    (ARE, "Rental income", ("even", 744_000)),
    (ARE, "Property taxes and maintenance", ("base", 4_800, {4: 41_500, 10: 41_500})),
    (ARE, "Interest expense", ("even", 207_000)),
    (ARE, "Insurance expense", ("even", 18_000)),
    (ARE, "Management fees - related party", ("quarterly", 60_000)),
    (FOUNDATION, "Charitable grants", ("even", 250_000)),
    (FOUNDATION, "Tax preparation and accounting fees", ("months", {5: 5_200, 11: 2_300})),
]
PROPERTIES = [("Seacliff Avenue residence", FamilyProperty.Kind.RESIDENCE), ("Lakeview cottage", FamilyProperty.Kind.VACATION)]


def is_demo(workspace: Workspace) -> bool:
    return Family.objects.filter(workspace=workspace, code="alder", is_active=True).exists()


def needs_demo_budget(workspace: Workspace, year: int) -> bool:
    return is_demo(workspace) and not Budget.objects.filter(workspace=workspace, year=year).exists()


def months(how: tuple[Any, ...]) -> list[Decimal]:
    kind = how[0]
    if kind in ("even", "quarterly"):
        return svc.spread(Decimal(how[1]), kind)
    if kind == "months":
        return [Decimal(how[1].get(m, 0)) for m in range(1, 13)]
    return [Decimal(how[1] + how[2].get(m, 0)) for m in range(1, 13)]  # base + extras


def add_properties(workspace: Workspace, actor: str) -> int:
    added = 0
    for name, kind in PROPERTIES:
        _, made = FamilyProperty.objects.get_or_create(workspace=workspace, name=name, defaults={"kind": kind, "created_by": actor})
        added += made
    return added


def create(workspace: Workspace, client: CampfireClient, year: int, actor: str, actor_id: str) -> Budget:
    """The demo family's budget for the year, as a draft (so approving it can be shown), and its homes as properties."""
    names = {s.label: s.entity_id for s in st.scopes(client) if not s.rollup}
    accounts = {a.name.lower(): a for a in svc.pl_accounts(client)}
    lines = []
    for entity, account, how in PLAN:
        a = accounts.get(account.lower())
        if entity in names and a is not None:
            lines.append({"entity_id": names[entity], "entity": entity, "account_id": a.id, "number": a.number, "account": a.name, "income": a.income,
                          "months": months(how)})
    if not lines:
        raise svc.BudgetError("The demo entities and accounts aren't in Campfire, so there is nothing to budget.")
    if add_properties(workspace, actor):
        from gl.services import lifestyle

        if lifestyle.is_set_up(workspace):  # the new homes become tags too
            lifestyle.set_up(workspace, client)
    return svc.new_version(workspace, year, actor, actor_id, "Demo budget for the Alder family", lines)

