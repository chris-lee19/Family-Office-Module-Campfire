"""Every Family Office page is served two ways: framed inside Campfire (verified `cf_ctx`) and standalone
for someone signed in with Campfire. A page is a function (request, workspace, framed, **url_kwargs) -> response.

Inside a frame, Campfire's `cf_ctx` is single use, and cookies in a third-party iframe are unreliable. So
when a frame opens, the app issues its own short-lived signed frame token (`fo_t`), bound to the workspace,
and every link and form inside the frame carries it. Framed views accept a fresh `cf_ctx` or a valid `fo_t`.
Framed POSTs are exempt from Django's cookie-based CSRF check because the token in the form, which a
third-party site cannot read, does that job.
"""

from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from django.core import signing
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from campfire_integration.frames import SESSION_USER, FrameAuthError, actor_for, session_workspace, verify_frame_request
from campfire_integration.models import Workspace
from core.framing import campfire_frame

Page = Callable[..., HttpResponse]

FRAME_TOKEN_PARAM = "fo_t"
FRAME_TOKEN_SALT = "fo.frame-session"
FRAME_TOKEN_MAX_AGE = 60 * 60  # seconds; reopening the page from Campfire's sidebar issues a new one


def issue_frame_token(workspace: Workspace, claims: dict[str, Any]) -> str:
    return signing.dumps({"w": workspace.pk, "c": workspace.customer_id, "u": claims.get("user_id")}, salt=FRAME_TOKEN_SALT)


def verify_frame_token(token: str) -> tuple[Workspace, dict[str, Any]]:
    try:
        data = signing.loads(token, salt=FRAME_TOKEN_SALT, max_age=FRAME_TOKEN_MAX_AGE)
    except signing.SignatureExpired as e:
        raise FrameAuthError("This page has been open too long. Open it again from Campfire.") from e
    except signing.BadSignature as e:
        raise FrameAuthError("Open this page from Campfire.") from e
    workspace = Workspace.objects.filter(pk=data.get("w"), customer_id=data.get("c")).first()
    if workspace is None or not workspace.is_connected:
        raise FrameAuthError("This Campfire workspace has not connected the Family Office app.", status=403)
    return workspace, data


def framed(page: Page, surfaces: tuple[str, ...] = ("nav",)) -> Callable[..., HttpResponse]:
    @csrf_exempt
    @campfire_frame
    def view(request: HttpRequest, **kwargs: Any) -> HttpResponse:
        try:
            if request.method == "GET" and request.GET.get("cf_ctx"):
                ctx = verify_frame_request(request, surfaces=surfaces)
                workspace, user_id = ctx.workspace, ctx.claims.get("user_id")
                token = issue_frame_token(workspace, ctx.claims)
            else:
                token = request.POST.get(FRAME_TOKEN_PARAM) or request.GET.get(FRAME_TOKEN_PARAM, "")
                if not token:
                    raise FrameAuthError("Open this page from Campfire.")
                workspace, data = verify_frame_token(token)
                user_id = data.get("u")
        except FrameAuthError as e:
            return render(request, "campfire/message.html", {"title": "Cannot open this page", "message": str(e), "framed": True}, status=e.status)
        request.fo_frame_token = token  # type: ignore[attr-defined]
        request.fo_workspace = workspace  # type: ignore[attr-defined]
        request.fo_actor, request.fo_actor_id = actor_for(workspace, user_id)  # type: ignore[attr-defined]
        return page(request, workspace, True, **kwargs)

    return view


def record_framed(page: Page, *, object_type: str, surfaces: tuple[str, ...]) -> Callable[..., HttpResponse]:
    """A page about one Campfire record (a panel or an iframe button on a bill or journal entry). The first request
    carries a `cf_ctx` that must name this record; follow-up requests in the frame carry the frame token."""

    @csrf_exempt
    @campfire_frame
    def view(request: HttpRequest, record_id: int, **kwargs: Any) -> HttpResponse:
        try:
            if request.method == "GET" and request.GET.get("cf_ctx"):
                ctx = verify_frame_request(request, surfaces=surfaces, object_type=object_type, object_id=str(record_id))
                workspace, user_id = ctx.workspace, ctx.claims.get("user_id")
                token = issue_frame_token(workspace, ctx.claims)
            else:
                token = request.POST.get(FRAME_TOKEN_PARAM) or request.GET.get(FRAME_TOKEN_PARAM, "")
                if not token:
                    raise FrameAuthError("Open this from Campfire.")
                workspace, data = verify_frame_token(token)
                user_id = data.get("u")
        except FrameAuthError as e:
            return render(request, "campfire/message.html", {"title": "Cannot open this", "message": str(e), "framed": True}, status=e.status)
        request.fo_frame_token = token  # type: ignore[attr-defined]
        request.fo_workspace = workspace  # type: ignore[attr-defined]
        request.fo_actor, request.fo_actor_id = actor_for(workspace, user_id)  # type: ignore[attr-defined]
        return page(request, workspace, True, record_id=record_id, **kwargs)

    return view


def standalone(page: Page) -> Callable[..., HttpResponse]:
    def view(request: HttpRequest, **kwargs: Any) -> HttpResponse:
        workspace = session_workspace(request)
        if workspace is None:
            return redirect("home")
        request.fo_workspace = workspace  # type: ignore[attr-defined]
        user = request.session.get(SESSION_USER) or {}
        request.fo_actor, request.fo_actor_id = actor_for(  # type: ignore[attr-defined]
            workspace, user.get("campfire_user_id"), name=user.get("email") or user.get("name") or "", sub=user.get("sub") or "")
        return page(request, workspace, False, **kwargs)

    return view


def page_url(request: HttpRequest, framed: bool, name: str, *args: Any) -> str:
    """URL of another page in the same mode: `app:frame_<name>` + frame token inside Campfire, else `app:<name>`."""
    app, _, view = name.rpartition(":")
    url = reverse(f"{app}:frame_{view}" if framed else name, args=args)
    token = getattr(request, "fo_frame_token", None)
    return f"{url}?{urlencode({FRAME_TOKEN_PARAM: token})}" if framed and token else url
