"""Roles: who may view, prepare, approve and manage in a workspace.

Every page goes through `pages.framed` / `pages.standalone`, which call `check` once the person is known. A GET
needs "view". A POST needs what its action is: reading from Campfire is "view", approving is "approve", setup and
controls are "manage", and everything else that changes something is "prepare". Unknown actions count as "prepare",
so a new write action is closed to viewers by default.

While a workspace has no memberships, roles are off: everyone who can open the app (they are already users of the
Campfire workspace) can do everything. The first admin turns roles on. After that, someone who isn't listed gets
the workspace's default role (viewer, or none).
"""

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from campfire_integration.models import CampfireUser, Membership, Workspace

VIEW, PREPARE, APPROVE, MANAGE = "view", "prepare", "approve", "manage"
ADMIN, CONTROLLER, ACCOUNTANT, APPROVER, VIEWER, NONE = "admin", "controller", "accountant", "approver", "viewer", "none"
ROLES: dict[str, tuple[str, str, frozenset[str]]] = {
    ADMIN: ("Admin", "Everything, plus setup, controls and people's roles", frozenset({VIEW, PREPARE, APPROVE, MANAGE})),
    CONTROLLER: ("Controller", "Enter and approve (never their own work when a second person is required)", frozenset({VIEW, PREPARE, APPROVE})),
    ACCOUNTANT: ("Accountant", "Enter and import: records, files, bills, entries, bank lines, capital calls", frozenset({VIEW, PREPARE})),
    APPROVER: ("Approver", "Approve or reject bills and entries, review marks and imports, sign off the close", frozenset({VIEW, APPROVE})),
    VIEWER: ("Viewer", "See everything, change nothing", frozenset({VIEW})),
    NONE: ("No access", "Cannot open the app", frozenset()),
}
ASSIGNABLE = (ADMIN, CONTROLLER, ACCOUNTANT, APPROVER, VIEWER, NONE)
DEFAULTS = (VIEWER, NONE)

# POST actions that only read from Campfire or refresh what is shown.
READ_ACTIONS = {"refresh", "tieout", "refresh_balances", "setup_preview", "sync"}
APPROVE_ACTIONS = {("bills", "approve"), ("bills", "reject"), ("journals", "approve"), ("journals", "reject"), ("review", "accept"),
                   ("review", "reject"), ("review", "correct"), ("review", "sign_off"), ("close", "signoff")}
MANAGE_ACTIONS = {("gl", "map"), ("gl", "link"), ("gl", "auto_link"), ("gl", "post_from"), ("gl", "fair_value_from"), ("gl", "setup_create"),
                  ("review", "policy"), ("close", "reopen"), ("statements", "tax_rate")}
MANAGE_PAGES = {"load_demo", "access"}


def role_of(workspace: Workspace, actor_id: str) -> tuple[str, bool]:
    """(role, whether roles are on) for a person in a workspace."""
    members = {m.user_key: m.role for m in Membership.objects.filter(workspace=workspace)}
    if not members:
        return ADMIN, False
    role = members.get(actor_id) if actor_id else None
    return (role if role in ROLES else workspace.default_role if workspace.default_role in ROLES else VIEWER), True


def page_name(request: HttpRequest) -> str:
    match = getattr(request, "resolver_match", None)
    name = (match.view_name if match else "").rpartition(":")[2]
    return name[len("frame_"):] if name.startswith("frame_") else name


def required(page: str, method: str, action: str) -> str:
    if method != "POST":
        return VIEW
    if page in MANAGE_PAGES or (page, action) in MANAGE_ACTIONS:
        return MANAGE
    if (page, action) in APPROVE_ACTIONS:
        return APPROVE
    if action in READ_ACTIONS:
        return VIEW
    return PREPARE


def check(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse | None:
    """Set the person's role on the request; answer with a refusal page when the role doesn't cover the request."""
    actor_id = getattr(request, "fo_actor_id", "")
    if actor_id.startswith("cfu:") and actor_id[4:].isdigit():  # remember them, so an admin can give them a role
        CampfireUser.objects.get_or_create(workspace=workspace, campfire_user_id=int(actor_id[4:]))
    role, on = role_of(workspace, actor_id)
    label, _, can = ROLES[role]
    request.fo_role, request.fo_role_label, request.fo_roles_on, request.fo_can = role, label, on, can  # type: ignore[attr-defined]
    need = required(page_name(request), request.method or "GET", request.POST.get("action", "") if request.method == "POST" else "")
    if need in can:
        return None
    if VIEW not in can:
        text = "You don't have access to this workspace's family office. Ask one of its admins to give you a role (People & roles)."
    else:
        wants = {PREPARE: "enter or change things", APPROVE: "approve or reject", MANAGE: "change setup, controls or roles"}[need]
        fits = {PREPARE: "Accountant or Controller", APPROVE: "Approver or Controller", MANAGE: "Admin"}[need]
        text = f"Your role is {label}: it can't {wants}. Ask one of the workspace's admins for the {fits} role (People & roles). Nothing was changed."
    context: dict[str, Any] = {"title": "Not allowed for your role", "message": text, "framed": framed, "back": request.META.get("HTTP_REFERER", "")}
    return render(request, "campfire/message.html", context, status=403)
