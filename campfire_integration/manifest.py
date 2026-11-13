"""The Campfire app manifest (Developer -> Apps -> Manifest tab), generated from settings.

Only extension points the app actually serves are declared. Add a page here when its view exists.
"""

from typing import Any
from urllib.parse import urlparse

from django.conf import settings

APP_NAME = "Family Office"

# Sidebar section in Campfire: (key, label, icon, path). Each page is a framed view.
NAV_PAGES: list[tuple[str, str, str, str]] = [
    ("home", "Home", "home", "/fo/frame/home"),
    ("investments", "Investments", "chart-pie", "/fo/frame/investments"),
    ("entities", "Entities & Ownership", "users", "/fo/frame/entities"),
    ("imports", "Imports", "upload", "/fo/frame/imports"),
    ("calls", "Capital calls", "banknote", "/fo/frame/calls"),
    ("gl", "General ledger", "book-open", "/fo/frame/gl"),
    ("review", "Review", "check-circle", "/fo/frame/review"),
    ("close", "Month-end close", "calendar-check", "/fo/frame/close"),
    ("records", "Records", "database", "/fo/frame/records"),
]

# Event topics to receive at /campfire/events (each needs a read scope in allowed_scopes). Add a topic only
# when a handler exists for it (gl/handlers.py).
EVENT_TOPICS: list[str] = [
    "JournalEntry.created", "JournalEntry.updated", "JournalEntry.deleted",
    "Bill.created", "Bill.updated", "Bill.deleted", "Bill.paid", "Bill.payment",
]
# Campfire refuses a manifest whose topics aren't covered by allowed_scopes (any tier of the resource).
TOPIC_RESOURCE = {"JournalEntry": "journal_entries", "Bill": "bills", "Invoice": "invoices"}


def covered_topics(scopes: list[str]) -> list[str]:
    resources = {s.split(":")[0] for s in scopes if ":" in s}
    return [t for t in EVENT_TOPICS if TOPIC_RESOURCE.get(t.split(".")[0]) in resources]


def build_manifest() -> dict[str, Any]:
    app_url = settings.APP_URL
    topics = covered_topics(list(settings.CAMPFIRE["SCOPES"]))
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
            [{"topics": topics, "endpoint_url": app_url + "/campfire/events", "is_active": True}] if topics else []
        ),
        "hooks": [],
    }
    return manifest
