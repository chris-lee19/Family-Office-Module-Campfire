"""The Campfire app manifest (Developer -> Apps -> Manifest tab), generated from settings.

Only extension points the app actually serves are declared. Add a page here when its view exists.
"""

from typing import Any
from urllib.parse import urlparse

from django.conf import settings

APP_NAME = "Family Office"

# Sidebar section in Campfire: (key, label, icon, path). Each page is a framed view.
NAV_PAGES: list[tuple[str, str, str, str]] = [
    ("entities", "Entities & Ownership", "users", "/fo/frame/entities"),
]

# Event topics to receive at /campfire/events. Filled in as handlers are added
# (e.g. bill payment status for capital calls) once Campfire confirms topic names.
EVENT_TOPICS: list[str] = []


def build_manifest() -> dict[str, Any]:
    app_url = settings.APP_URL
    manifest: dict[str, Any] = {
        "app": {
            "name": APP_NAME,
            "tagline": "Entities, ownership, investments and family reporting on Campfire",
            "category": "other",
            "description": (
                "Family office operations on top of Campfire's general ledger: legal entities and ownership, "
                "investments and alternatives, reconciliation, documents and K-1s, liquidity and family reporting."
            ),
            "icon_url": None,
            "redirect_uris": [app_url + settings.CAMPFIRE["REDIRECT_PATH"]],
            "connect_url": app_url + "/campfire/connect",
            "allowed_scopes": list(settings.CAMPFIRE["SCOPES"]),
            "manage_url": app_url + "/campfire/manage",
        },
        "allowed_domains": [urlparse(app_url).hostname],
        "slots": [],
        "panels": [],
        "navigation": [
            {
                "key": "family-office",
                "label": APP_NAME,
                "icon": "landmark",
                "url_template": "",
                "is_active": True,
                "items": [
                    {"key": key, "label": label, "icon": icon, "url_template": app_url + path, "is_active": True}
                    for key, label, icon, path in NAV_PAGES
                ],
            }
        ],
        "event_subscriptions": (
            [{"topics": EVENT_TOPICS, "endpoint_url": app_url + "/campfire/events", "is_active": True}] if EVENT_TOPICS else []
        ),
        "hooks": [],
    }
    return manifest
