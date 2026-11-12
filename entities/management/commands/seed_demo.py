"""Seed a fictional family ("Alder") into a workspace for demos and local development.

Structure (all names fictional):

    G1  Robert & Eleanor Alder
        Robert Alder Revocable Trust      100% Robert (beneficiary)
        Eleanor Alder Revocable Trust     100% Eleanor (beneficiary)
        Alder Management LLC              50% Robert RT / 50% Eleanor RT
        Alder Family Investments LP       1% GP Alder Management LLC; LPs: 24.5% Robert RT, 24.5% Eleanor RT,
                                          50% Alder 2012 Dynasty Trust
        Alder Real Estate Holdings LLC    100% Alder Family Investments LP
        Alder Family Foundation           private foundation, no owners
    G2  Claire (Branch A) and James (Branch B)
        Alder 2012 Dynasty Trust          50% Claire / 50% James (beneficiaries)
        Claire Moreno 2019 GST Trust      100% Claire
"""

from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from campfire_integration.models import Workspace
from entities.models import Branch, Entity, EntityRole, Family, OwnershipInterest, Person

E = Entity.EntityType
T = Entity.TaxClassification
IT = OwnershipInterest.InterestType
R = EntityRole.Role


class Command(BaseCommand):
    help = "Create the fictional Alder family in a workspace."

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--customer-id", type=int, required=True, help="Campfire customer id of the workspace")
        parser.add_argument(
            "--create-workspace", action="store_true", help="Create the workspace row if missing (local dev without Campfire)"
        )
        parser.add_argument("--reset", action="store_true", help="Delete and recreate the Alder family if present")

    @transaction.atomic
    def handle(self, *args, **opts):  # type: ignore[no-untyped-def]
        workspace = Workspace.objects.filter(customer_id=opts["customer_id"]).first()
        if workspace is None:
            if not opts["create_workspace"]:
                raise CommandError("No such workspace. Connect it from Campfire first, or pass --create-workspace.")
            workspace = Workspace.objects.create(customer_id=opts["customer_id"], name="Demo workspace")
        existing = Family.objects.filter(workspace=workspace, code="alder").first()
        if existing:
            if not opts["reset"]:
                raise CommandError("The Alder family already exists in this workspace; pass --reset to recreate it.")
            existing.delete()
        family = seed_alder(workspace)
        self.stdout.write(self.style.SUCCESS(f"Seeded {family} with {family.entities.count()} entities."))


def seed_alder(workspace: Workspace) -> Family:
    family = Family.objects.create(workspace=workspace, name="Alder Family", code="alder")
    g1 = Branch.objects.create(family=family, name="Founders (G1)", sort_order=0)
    branch_a = Branch.objects.create(family=family, parent=g1, name="Branch A: Claire", sort_order=1)
    branch_b = Branch.objects.create(family=family, parent=g1, name="Branch B: James", sort_order=2)

    def person(first: str, last: str, gen: int, branch: Branch, year: int, principal: bool = False) -> Person:
        return Person.objects.create(
            family=family, first_name=first, last_name=last, generation=gen, branch=branch, birth_year=year, is_principal=principal
        )

    robert = person("Robert", "Alder", 1, g1, 1951, principal=True)
    eleanor = person("Eleanor", "Alder", 1, g1, 1954, principal=True)
    claire = person("Claire", "Moreno", 2, branch_a, 1980)
    james = person("James", "Alder", 2, branch_b, 1983)
    person("Sofia", "Moreno", 3, branch_a, 2010)

    def entity(name: str, etype: str, tax: str, jurisdiction: str, formed: date, branch: Branch | None = None, **kw) -> Entity:  # type: ignore[no-untyped-def]
        return Entity.objects.create(
            family=family, name=name, entity_type=etype, tax_classification=tax, jurisdiction=jurisdiction,
            formation_date=formed, branch=branch, **kw,
        )

    robert_rt = entity("Robert Alder Revocable Trust", E.REVOCABLE_TRUST, T.GRANTOR_TRUST, "US-CA", date(2005, 3, 1), g1)
    eleanor_rt = entity("Eleanor Alder Revocable Trust", E.REVOCABLE_TRUST, T.GRANTOR_TRUST, "US-CA", date(2005, 3, 1), g1)
    dynasty = entity("Alder 2012 Dynasty Trust", E.IRREVOCABLE_TRUST, T.NON_GRANTOR_TRUST, "US-SD", date(2012, 11, 15))
    gst = entity("Claire Moreno 2019 GST Trust", E.IRREVOCABLE_TRUST, T.NON_GRANTOR_TRUST, "US-DE", date(2019, 6, 3), branch_a)
    mgmt = entity("Alder Management LLC", E.LLC, T.PARTNERSHIP, "US-DE", date(2008, 1, 10), g1, short_name="AM LLC")
    lp = entity("Alder Family Investments LP", E.LIMITED_PARTNERSHIP, T.PARTNERSHIP, "US-DE", date(2008, 2, 1), short_name="AFI LP")
    re_llc = entity("Alder Real Estate Holdings LLC", E.LLC, T.DISREGARDED, "US-DE", date(2014, 4, 22), short_name="ARE LLC")
    entity("Alder Family Foundation", E.PRIVATE_FOUNDATION, T.TAX_EXEMPT, "US-CA", date(2010, 9, 1))

    start = date(2020, 1, 1)

    def owns(owned: Entity, owner: Entity | Person, pct: str, kind: str) -> None:
        OwnershipInterest.objects.create(
            owned=owned,
            owner_entity=owner if isinstance(owner, Entity) else None,
            owner_person=owner if isinstance(owner, Person) else None,
            percentage=Decimal(pct),
            interest_type=kind,
            effective_from=start,
        )

    owns(robert_rt, robert, "100", IT.BENEFICIARY)
    owns(eleanor_rt, eleanor, "100", IT.BENEFICIARY)
    owns(dynasty, claire, "50", IT.BENEFICIARY)
    owns(dynasty, james, "50", IT.BENEFICIARY)
    owns(gst, claire, "100", IT.BENEFICIARY)
    owns(mgmt, robert_rt, "50", IT.MEMBER)
    owns(mgmt, eleanor_rt, "50", IT.MEMBER)
    owns(lp, mgmt, "1", IT.GENERAL_PARTNER)
    owns(lp, robert_rt, "24.5", IT.LIMITED_PARTNER)
    owns(lp, eleanor_rt, "24.5", IT.LIMITED_PARTNER)
    owns(lp, dynasty, "50", IT.LIMITED_PARTNER)
    owns(re_llc, lp, "100", IT.MEMBER)

    EntityRole.objects.create(entity=robert_rt, role=R.GRANTOR, person=robert)
    EntityRole.objects.create(entity=robert_rt, role=R.TRUSTEE, person=robert)
    EntityRole.objects.create(entity=eleanor_rt, role=R.GRANTOR, person=eleanor)
    EntityRole.objects.create(entity=eleanor_rt, role=R.TRUSTEE, person=eleanor)
    EntityRole.objects.create(entity=dynasty, role=R.GRANTOR, person=robert)
    EntityRole.objects.create(entity=dynasty, role=R.TRUSTEE, external_name="Northwind Trust Company")
    EntityRole.objects.create(entity=dynasty, role=R.TRUST_PROTECTOR, external_name="Hollis & Park LLP")
    EntityRole.objects.create(entity=gst, role=R.TRUSTEE, person=james)
    EntityRole.objects.create(entity=mgmt, role=R.MANAGER, person=robert)
    return family
