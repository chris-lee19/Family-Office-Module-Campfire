"""Every Family Office page is served two ways: framed inside Campfire (verified `cf_ctx`) and standalone
for someone signed in with Campfire. A page is a function (request, workspace, framed) -> response."""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from campfire_integration.frames import FrameAuthError, session_workspace, verify_frame_request
from campfire_integration.models import Workspace
from core.framing import campfire_frame

Page = Callable[[HttpRequest, Workspace, bool], HttpResponse]


def framed(page: Page, surfaces: tuple[str, ...] = ("nav",)) -> Callable[[HttpRequest], HttpResponse]:
    @campfire_frame
    def view(request: HttpRequest) -> HttpResponse:
        try:
            ctx = verify_frame_request(request, surfaces=surfaces)
        except FrameAuthError as e:
            return render(request, "campfire/message.html", {"title": "Cannot open this page", "message": str(e), "framed": True}, status=e.status)
        return page(request, ctx.workspace, True)

    return view


def standalone(page: Page) -> Callable[[HttpRequest], HttpResponse]:
    def view(request: HttpRequest) -> HttpResponse:
        workspace = session_workspace(request)
        if workspace is None:
            return redirect("home")
        return page(request, workspace, False)

    return view
