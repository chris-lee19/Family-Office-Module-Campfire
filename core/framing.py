"""Responses that Campfire is allowed to show in an iframe (sidebar pages, panels, drawers)."""

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse


def campfire_frame(view: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    """Allow framing by Campfire's frontend origin only, instead of the default X-Frame-Options: DENY."""

    @wraps(view)
    def wrapped(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        response = view(request, *args, **kwargs)
        response.xframe_options_exempt = True
        response["Content-Security-Policy"] = f"frame-ancestors {settings.CAMPFIRE['FRONTEND_ORIGIN']}"
        return response

    return wrapped
