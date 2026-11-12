import json
import logging
import secrets
import time
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from campfire_integration import client, events
from campfire_integration.frames import FrameAuthError, sign_in, verify_frame_request
from campfire_integration.models import CampfireEvent, Workspace
from campfire_integration.security import IdTokenError, verify_id_token, verify_signature

log = logging.getLogger(__name__)

SESSION_OAUTH = "campfire_oauth"


def _message(request: HttpRequest, title: str, message: str, status: int = 400) -> HttpResponse:
    return render(request, "campfire/message.html", {"title": title, "message": message}, status=status)


def _campfire_return_url(candidate: str | None) -> str | None:
    """Only Campfire's own origin may be a return target."""
    origin = settings.CAMPFIRE["FRONTEND_ORIGIN"]
    if candidate and candidate.startswith(origin + "/"):
        return candidate
    return None


@require_GET
def connect(request: HttpRequest) -> HttpResponse:
    """Start OAuth (authorization code + PKCE + OIDC). Campfire's Connect button lands here."""
    if not settings.CAMPFIRE["CLIENT_ID"]:
        return _message(request, "Not configured", "CAMPFIRE_CLIENT_ID is not set.", status=500)
    verifier, challenge = client.pkce_pair()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    request.session[SESSION_OAUTH] = {
        "state": state,
        "verifier": verifier,
        "nonce": nonce,
        "return_to": _campfire_return_url(request.GET.get("return_to")),
    }
    return redirect(client.authorize_url(state=state, nonce=nonce, code_challenge=challenge))


@require_GET
def callback(request: HttpRequest) -> HttpResponse:
    pending: dict[str, Any] | None = request.session.pop(SESSION_OAUTH, None)
    if request.GET.get("error"):
        detail = request.GET.get("error_description", "")
        return _message(request, "Could not connect to Campfire", f"Campfire returned {request.GET['error']} {detail}".strip())
    if not pending or request.GET.get("state") != pending["state"]:
        return _message(request, "Could not connect to Campfire", "State mismatch: refusing the code.")
    try:
        tokens = client.exchange_code(code=request.GET.get("code", ""), verifier=pending["verifier"])
    except client.CampfireError as e:
        return _message(request, "Could not connect to Campfire", f"Token exchange failed ({e.status_code}).")

    id_token = tokens.get("id_token")
    if not id_token:
        return _message(request, "Could not connect to Campfire", "No id_token returned; add openid to the app's scopes.")
    try:
        identity = verify_id_token(
            id_token=id_token,
            signing_key=client.id_token_signing_key(id_token),
            issuer=client.issuer(),
            client_id=settings.CAMPFIRE["CLIENT_ID"],
            nonce=pending["nonce"],
        )
    except IdTokenError as e:
        return _message(request, "Could not connect to Campfire", f"ID token rejected: {e}")

    # The token response does not name the workspace; the install config endpoint does.
    whoami = client.fetch_install_config(access_token=tokens["access_token"])
    customer_id = int(whoami["customer_id"])
    workspace, _ = Workspace.objects.get_or_create(customer_id=customer_id)
    workspace.name = whoami.get("customer_name") or workspace.name
    workspace.apply_tokens(tokens)
    workspace.install_config = whoami.get("install_config") or {}
    workspace.save()
    client.CampfireClient(workspace).put_install_config({**workspace.install_config, "base_url": settings.APP_URL})

    person = whoami.get("user") or {}
    sign_in(
        request,
        workspace=workspace,
        user={
            "sub": identity.get("sub"),
            "campfire_user_id": person.get("id"),
            "name": identity.get("name") or person.get("name") or "",
            "email": identity.get("email") or person.get("email") or "",
        },
    )
    if workspace.onboarded_at is None:
        workspace.onboarded_at = timezone.now()  # onboarding wizard arrives with COA/dimension setup (P1)
        workspace.save(update_fields=["onboarded_at", "updated_at"])
    return redirect(pending.get("return_to") or "home")


@require_GET
def manage(request: HttpRequest) -> HttpResponse:
    """Campfire's "Manage" link on the connection page: a signed, single-use entry that signs the person in."""
    try:
        ctx = verify_frame_request(request, surfaces=("manage",))
    except FrameAuthError as e:
        return _message(request, "Cannot open Family Office", str(e), status=e.status)
    sign_in(request, workspace=ctx.workspace, user={"campfire_user_id": ctx.claims.get("user_id")})
    return redirect("home")


@require_POST
def logout(request: HttpRequest) -> HttpResponse:
    request.session.flush()
    return redirect("home")


@csrf_exempt
@require_POST
def receive_event(request: HttpRequest) -> HttpResponse:
    """Signed thin events from Campfire (`event_subscriptions` in the manifest)."""
    raw = request.body
    if not verify_signature(
        secrets=settings.CAMPFIRE["SIGNING_SECRETS"], headers=dict(request.headers), body=raw, now=time.time()
    ):
        return JsonResponse({"error": "bad signature"}, status=401)
    try:
        body = json.loads(raw)
    except ValueError:
        return JsonResponse({"error": "not JSON"}, status=400)
    if not isinstance(body, dict):
        return JsonResponse({"error": "expected an object"}, status=400)

    customer_id = int((body.get("customer") or {}).get("id") or 0)
    event = CampfireEvent.objects.create(
        workspace=Workspace.objects.filter(customer_id=customer_id).first(),
        customer_id=customer_id,
        topic=str(body.get("topic", "")),
        object_id=str(body.get("id") or ""),
        payload=body,
    )
    events.dispatch(event)
    return JsonResponse({"ok": True})
