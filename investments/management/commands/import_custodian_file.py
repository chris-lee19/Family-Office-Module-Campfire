from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from campfire_integration.models import Workspace
from investments.demo import alder_activity_file, alder_price_file
from investments.importers.custodian import commit, stage
from investments.importers.formats import FormatError, available_formats, resolve_format
from investments.models import ImportIssue


class Command(BaseCommand):
    help = (
        "Validate a custodian file into an import batch and, with --commit, import it. "
        "Use --write-sample to produce the demo family's sample files."
    )

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("path", nargs="?", help="File to import (or to write with --write-sample)")
        parser.add_argument("--customer-id", type=int, help="Campfire customer id of the workspace")
        parser.add_argument("--format", dest="fmt", default="pershing_style_activity", help="File layout code")
        parser.add_argument("--commit", action="store_true", help="Import the file if it validates (default: validate only)")
        parser.add_argument("--create-securities", action="store_true", help="Create securities the file names but aren't set up")
        parser.add_argument("--list-formats", action="store_true", help="List the layouts available to the workspace")
        parser.add_argument("--write-sample", choices=["activity", "prices"], help="Write the Alder demo sample file to PATH")

    def handle(self, *args, **opts):  # type: ignore[no-untyped-def]
        if opts["write_sample"]:
            if not opts["path"]:
                raise CommandError("Give the path to write the sample to.")
            builder = alder_activity_file if opts["write_sample"] == "activity" else alder_price_file
            Path(opts["path"]).write_text(builder(timezone.localdate()))
            self.stdout.write(self.style.SUCCESS(f"Wrote {opts['path']}"))
            return

        if opts["customer_id"] is None:
            raise CommandError("--customer-id is required.")
        workspace = Workspace.objects.filter(customer_id=opts["customer_id"]).first()
        if workspace is None:
            raise CommandError("No such workspace.")
        if opts["list_formats"]:
            for f in available_formats(workspace):
                self.stdout.write(f"{f.code:32} {f.kind:13} {f.name}")
            return
        if not opts["path"]:
            raise CommandError("Give the file to import.")
        try:
            fmt = resolve_format(workspace, opts["fmt"])
        except FormatError as e:
            raise CommandError(str(e)) from e
        path = Path(opts["path"])
        batch = stage(workspace, fmt=fmt, content=path.read_bytes(), file_name=path.name, created_by="command line",
                      create_securities=opts["create_securities"])
        if opts["commit"] and batch.status == batch.Status.VALIDATED:
            batch = commit(batch, fmt=fmt)

        for issue in batch.issues.all():
            where = f"line {issue.row_number}: " if issue.row_number else ""
            style = {ImportIssue.Severity.ERROR: self.style.ERROR, ImportIssue.Severity.WARNING: self.style.WARNING}.get(issue.severity, str)
            self.stdout.write(style(f"  {issue.severity:7} {where}{issue.message}"))
        new = "passed checks" if batch.status == batch.Status.REJECTED else "new"
        summary = (
            f"Batch {batch.pk} {batch.get_status_display().lower()}: {batch.rows_total} rows, {batch.rows_new} {new}, "
            f"{batch.rows_duplicate} already imported, {batch.rows_ignored} ignored."
        )
        if batch.status == batch.Status.REJECTED:
            raise CommandError(summary)
        if batch.status == batch.Status.VALIDATED:
            summary += " Nothing imported yet; run again with --commit."
        self.stdout.write(self.style.SUCCESS(summary))
