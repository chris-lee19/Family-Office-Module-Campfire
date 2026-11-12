import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from campfire_integration.manifest import build_manifest


class Command(BaseCommand):
    help = "Write the Campfire app manifest for APP_URL; paste it into Developer -> Apps -> Manifest."

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--output", default="manifest.json", help="File to write ('-' for stdout)")

    def handle(self, *args, **options):  # type: ignore[no-untyped-def]
        if not settings.APP_URL.startswith("https://"):
            raise CommandError(f"APP_URL must be a public https URL Campfire can reach (got {settings.APP_URL!r}).")
        text = json.dumps(build_manifest(), indent=2)
        if options["output"] == "-":
            self.stdout.write(text)
            return
        Path(options["output"]).write_text(text + "\n")
        self.stdout.write(self.style.SUCCESS(f"Wrote {options['output']}"))
