from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Family
from investments.demo import seed_alder_investments


class Command(BaseCommand):
    help = "Create the fictional Alder family (entities, ownership and investments) in a workspace."

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
            delete_family(existing)
        family = seed_alder(workspace)
        seed_alder_investments(family, timezone.localdate())
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {family}: {family.entities.count()} entities, {family.accounts.count()} accounts."
        ))


def delete_family(family: Family) -> None:
    """Accounts protect their owners, so remove them (and with them transactions, marks, commitments) first."""
    from investments.models import Account, ImportBatch, Instrument

    workspace = family.workspace
    Account.objects.filter(family=family).delete()
    ImportBatch.objects.filter(family=family).delete()
    family.delete()
    # Instruments are shared across a workspace's families; drop only ones nothing references any more.
    Instrument.objects.filter(
        workspace=workspace, transactions__isnull=True, valuations__isnull=True, commitments__isnull=True
    ).delete()
