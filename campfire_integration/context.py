"""Template context shared by every page."""

from typing import Any

from django.conf import settings
from django.http import HttpRequest


def campfire(request: HttpRequest) -> dict[str, Any]:
    return {"campfire_origin": settings.CAMPFIRE["FRONTEND_ORIGIN"]}
