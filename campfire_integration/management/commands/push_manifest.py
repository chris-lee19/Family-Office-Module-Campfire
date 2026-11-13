"""Apply the app manifest to the Campfire app over the API (instead of pasting it into the Manifest tab).

    python manage.py push_manifest --dry-run          # show what would change
    python manage.py push_manifest                    # apply everything (needs APP_URL = your public HTTPS URL)
    python manage.py push_manifest --scopes-only      # only the permissions; URLs and extensions stay as they are

Uses CAMPFIRE_API_KEY (a workspace admin's key; app management refuses OAuth tokens). The app's name, tagline,
description, category and icon are kept as they are in Campfire. Permission changes land in a development
version: click **Promote** on the app page in Campfire to make them live, then reconnect the workspace.
"""

import json
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from campfire_integration.client import ApiKeyClient, CampfireError, NotConnected
from campfire_integration.manifest import build_manifest

APPS_PATH = "/ca/api/v1/oauth-apps"
EXTENSION_SECTIONS = ("slots", "panels", "navigation", "event_subscriptions", "hooks")
KEPT_APP_FIELDS = ("name", "tagline", "description", "category", "icon_url")


def find_app(client: ApiKeyClient, app_id: str | None) -> dict[str, Any]:
    apps = client.get_json(APPS_PATH)
    apps = apps if isinstance(apps, list) else apps.get("results", [])
    if app_id:
        match = [a for a in apps if a.get("id") == app_id]
    else:
        client_id = settings.CAMPFIRE["CLIENT_ID"]
        match = [a for a in apps if client_id and a.get("client_id") == client_id] or [a for a in apps if a.get("name") == "Family Office"]
    if len(match) != 1:
        names = ", ".join(f"{a.get('name')} ({a.get('id')})" for a in apps) or "none"
        raise CommandError(f"Can't tell which app to update; pass --app-id. Apps in this workspace: {names}.")
    return match[0]


def desired_manifest(current: dict[str, Any], *, scopes_only: bool) -> dict[str, Any]:
    ours = build_manifest()
    if scopes_only:
        # Every extension section must be sent (a missing one is deleted), so send what is there now.
        return {"app": {**current["app"], "allowed_scopes": ours["app"]["allowed_scopes"]},
                **{k: current.get(k, []) for k in EXTENSION_SECTIONS}}
    host = urlparse(settings.APP_URL).hostname or ""
    if not settings.APP_URL.startswith("https://") or "your-subdomain" in settings.APP_URL or host in ("localhost", "127.0.0.1"):
        raise CommandError(f"APP_URL must be the public HTTPS URL Campfire reaches the app on (now {settings.APP_URL!r}). Use --scopes-only until it is.")
    ours["app"].update({k: current["app"][k] for k in KEPT_APP_FIELDS if k in current.get("app", {})})
    return ours


def summarize(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    lines = []
    b_app, a_app = before.get("app", {}), after.get("app", {})
    b_scopes, a_scopes = set(b_app.get("allowed_scopes", [])), set(a_app.get("allowed_scopes", []))
    if a_scopes - b_scopes:
        lines.append("add scopes: " + " ".join(sorted(a_scopes - b_scopes)))
    if b_scopes - a_scopes:
        lines.append("remove scopes: " + " ".join(sorted(b_scopes - a_scopes)))
    for key in ("redirect_uris", "connect_url", "manage_url"):
        if b_app.get(key) != a_app.get(key):
            lines.append(f"{key}: {b_app.get(key)!r} -> {a_app.get(key)!r}")
    if "allowed_domains" in after and before.get("allowed_domains") != after["allowed_domains"]:
        lines.append(f"allowed_domains: {before.get('allowed_domains')} -> {after['allowed_domains']}")
    for key in EXTENSION_SECTIONS:
        if before.get(key, []) != after.get(key, []):
            lines.append(f"{key}: {len(before.get(key, []))} -> {len(after.get(key, []))} entries")
    return lines


class Command(BaseCommand):
    help = "Apply the app manifest to the Campfire app over the API."

    def add_arguments(self, parser):  # type: ignore[no-untyped-def]
        parser.add_argument("--app-id", help="Campfire app id (default: the app with CAMPFIRE_CLIENT_ID, or the one named Family Office)")
        parser.add_argument("--scopes-only", action="store_true", help="Change only allowed_scopes; keep URLs and extensions")
        parser.add_argument("--dry-run", action="store_true", help="Show the changes without applying them")

    def handle(self, *args, **opts):  # type: ignore[no-untyped-def]
        try:
            client = ApiKeyClient()
        except NotConnected as e:
            raise CommandError(str(e)) from e
        app = find_app(client, opts["app_id"])
        path = f"{APPS_PATH}/{app['id']}/manifest"
        current = client.get_json(path)
        desired = desired_manifest(current, scopes_only=opts["scopes_only"])
        changes = summarize(current, desired)
        self.stdout.write(f"App {app.get('name')} ({app['id']}):")
        for line in changes or ["no changes"]:
            self.stdout.write(f"  {line}")
        if opts["dry_run"] or not changes:
            return
        r = client.request("PUT", path, json=desired)
        if r.status_code >= 400:
            raise CommandError(f"Campfire refused the manifest ({r.status_code}): {r.text[:1500]}")
        saved = r.json()
        if json.dumps(saved.get("app", {}), sort_keys=True) != json.dumps(current.get("app", {}), sort_keys=True):
            self.stdout.write(self.style.WARNING(
                "Permissions/details changed in a development version: open Developer -> Apps -> "
                f"{app.get('name')} in Campfire and click Promote, then reconnect the workspace."
            ))
        self.stdout.write(self.style.SUCCESS("Manifest applied."))


__all__ = ["Command", "desired_manifest", "summarize", "find_app", "CampfireError"]
