"""Who is asking: a Campfire frame (verified `cf_ctx`) or a person signed in with Campfire (session)."""

from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.http import HttpRequest

from campfire_integration.models import UsedContextToken, Workspace
from campfire_integration.security import ContextTokenError, verify_context_token

SESSION_WORKSPACE = "workspace_id"
SESSION_USER = "campfire_user"


class FrameAuthError(Exception):
    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class FrameContext:
    workspace: Workspace
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def surface(self) -> str:
        return str(self.claims.get("surface", ""))

    @property
    def object_ids(self) -> list[str]:
        return [str(i) for i in self.claims.get("object_ids", [])]


def verify_frame_request(
    request: HttpRequest, *, surfaces: tuple[str, ...] | None = None, object_type: str | None = None, object_id: str | None = None
) -> FrameContext:
    """Verify the single-use `cf_ctx` Campfire puts on a framed page's URL and resolve the workspace."""
    token = request.GET.get("cf_ctx", "")
    if not token:
        raise FrameAuthError("Open this page from Campfire.")
    try:
        claims = verify_context_token(
            token=token,
            secrets=settings.CAMPFIRE["SIGNING_SECRETS"],
            client_id=settings.CAMPFIRE["CLIENT_ID"],
            claim_jti=UsedContextToken.claim,
        )
    except ContextTokenError as e:
        raise FrameAuthError(f"Campfire context rejected: {e}") from e
    if surfaces is not None and claims.get("surface") not in surfaces:
        raise FrameAuthError("This page was opened from the wrong place in Campfire.", status=403)
    object_ids = [str(i) for i in claims.get("object_ids", [])]
    if object_id is None:
        if object_ids:
            raise FrameAuthError("Token is for a record, not this page.", status=403)
    elif claims.get("object_type") != object_type or object_id not in object_ids:
        raise FrameAuthError("Token is not for this record.", status=403)
    workspace = Workspace.objects.filter(customer_id=int(claims["customer_id"])).first()
    if workspace is None or not workspace.is_connected:
        raise FrameAuthError("This Campfire workspace has not connected the Family Office app.", status=403)
    return FrameContext(workspace=workspace, claims=claims)


def sign_in(request: HttpRequest, *, workspace: Workspace, user: dict[str, Any]) -> None:
    request.session.cycle_key()
    request.session[SESSION_WORKSPACE] = workspace.pk
    request.session[SESSION_USER] = {"name": "", "email": "", **user}
    remember_user(workspace, user)


def remember_user(workspace: Workspace, user: dict[str, Any]) -> None:
    from campfire_integration.models import CampfireUser

    uid = user.get("campfire_user_id")
    if not uid:
        return
    known, _ = CampfireUser.objects.get_or_create(workspace=workspace, campfire_user_id=int(uid))
    changed = [f for f in ("name", "email") if user.get(f) and getattr(known, f) != user[f]]
    for f in changed:
        setattr(known, f, user[f])
    known.save()


def actor_for(workspace: Workspace, user_id: Any, *, name: str = "", email: str = "", sub: str = "") -> tuple[str, str]:
    """(display name, stable id) for the person making a request. The id is the Campfire user id when known."""
    from campfire_integration.models import CampfireUser

    if user_id:
        known = CampfireUser.objects.filter(workspace=workspace, campfire_user_id=int(user_id)).first()
        label = name or email or (str(known) if known else f"Campfire user {user_id}")
        return label, f"cfu:{int(user_id)}"
    if sub:
        return name or email or "Campfire user", f"sub:{sub}"
    return name or email or "Campfire user", ""


def session_workspace(request: HttpRequest) -> Workspace | None:
    pk = request.session.get(SESSION_WORKSPACE)
    if pk is None:
        return None
    workspace = Workspace.objects.filter(pk=pk).first()
    return workspace if workspace and workspace.is_connected else None
