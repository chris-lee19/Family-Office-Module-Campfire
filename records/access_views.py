"""People & roles: who can view, enter, approve and manage in this workspace."""

from typing import Any

from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render

from campfire_integration import access, pages
from campfire_integration.models import CampfireUser, Membership, Workspace
from records.models import AuditEntry


def _audit(workspace: Workspace, actor: str, what: str, before: str, after: str) -> None:
    AuditEntry.objects.create(workspace=workspace, actor=actor, action=AuditEntry.Action.UPDATED, kind="roles", object_id=what[:40],
                              object_repr=f"Role: {what}"[:255], changes={"role": [before, after]})


def _handle(request: HttpRequest, workspace: Workspace) -> str:
    action, actor, me = request.POST.get("action", ""), getattr(request, "fo_actor", ""), getattr(request, "fo_actor_id", "")
    label = lambda role: access.ROLES[role][0] if role in access.ROLES else "Default"  # noqa: E731
    if action == "turn_on":
        if not me:
            raise ValueError("Sign in with Campfire first: roles are kept per person.")
        Membership.objects.update_or_create(workspace=workspace, user_key=me, defaults={"role": access.ADMIN, "updated_by": actor})
        _audit(workspace, actor, actor or me, "Roles off", "Admin (roles turned on)")
        return "Roles are on. You are the admin; everyone else can view until you give them a role."
    if action == "turn_off":
        Membership.objects.filter(workspace=workspace).delete()
        _audit(workspace, actor, "everyone", "Roles on", "Roles off")
        return "Roles are off: everyone who can open the app can do everything."
    if action == "default":
        role = request.POST.get("role", "")
        if role not in access.DEFAULTS:
            raise ValueError("Choose Viewer or No access.")
        before, workspace.default_role = workspace.default_role, role
        workspace.save(update_fields=["default_role"])
        _audit(workspace, actor, "people not listed", label(before), label(role))
        return f"People who aren't listed now get: {label(role)}."
    if action == "set":
        key, role = request.POST.get("user", ""), request.POST.get("role", "")
        if not key.startswith("cfu:") or role not in (*access.ASSIGNABLE, ""):
            raise ValueError("Choose a person and a role.")
        current = Membership.objects.filter(workspace=workspace, user_key=key).first()
        admins = set(Membership.objects.filter(workspace=workspace, role=access.ADMIN).values_list("user_key", flat=True))
        if current and current.role == access.ADMIN and role != access.ADMIN and admins == {key}:
            raise ValueError("That is the only admin. Make someone else an admin first, or turn roles off.")
        name = str(CampfireUser.objects.filter(workspace=workspace, campfire_user_id=int(key[4:])).first() or key) if key[4:].isdigit() else key
        if role:
            Membership.objects.update_or_create(workspace=workspace, user_key=key, defaults={"role": role, "updated_by": actor})
        elif current:
            current.delete()
        _audit(workspace, actor, name, label(current.role) if current else "Default", label(role))
        return f"{name}: {label(role) if role else 'the default role'}."
    raise ValueError("Unknown action.")


def access_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    message = error = ""
    if request.method == "POST":
        try:
            message = _handle(request, workspace)
        except ValueError as e:
            error = str(e)
    members = {m.user_key: m for m in Membership.objects.filter(workspace=workspace)}
    me = getattr(request, "fo_actor_id", "")
    role, on = access.role_of(workspace, me)  # again: the POST above may have changed it
    people: list[dict[str, Any]] = []
    seen = set()
    for u in CampfireUser.objects.filter(workspace=workspace).order_by("name", "email"):
        key = f"cfu:{u.campfire_user_id}"
        seen.add(key)
        people.append({"key": key, "name": str(u), "email": u.email, "last_seen": u.last_seen_at, "role": members[key].role if key in members else "", "me": key == me})
    people += [{"key": k, "name": k, "email": "", "last_seen": None, "role": m.role, "me": k == me} for k, m in members.items() if k not in seen]
    for p in people:
        p["effective"] = access.ROLES[p["role"] or (workspace.default_role if on else access.ADMIN)][0]
    return render(request, "records/access.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "message": message, "error": error,
        "people": people, "on": on, "my_role": access.ROLES[role][0], "is_admin": access.MANAGE in access.ROLES[role][2], "me": me,
        "roles": [(k, *access.ROLES[k][:2]) for k in access.ASSIGNABLE], "defaults": [(k, access.ROLES[k][0]) for k in access.DEFAULTS],
        "default_role": workspace.default_role, "action": pages.page_url(request, framed, "records:access"),
    })


frame_access = pages.framed(access_page)
standalone_access = pages.standalone(access_page)
