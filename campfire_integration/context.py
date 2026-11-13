"""Template context shared by every page."""

from typing import Any

from django.conf import settings
from django.http import HttpRequest

from campfire_integration.frames import session_workspace


def campfire(request: HttpRequest) -> dict[str, Any]:
    return {
        "campfire_origin": settings.CAMPFIRE["FRONTEND_ORIGIN"],
        # Standalone pages show the sidebar only to someone signed in to a connected workspace (same rule as the pages).
        "signed_in": lambda: session_workspace(request) is not None,
    }
