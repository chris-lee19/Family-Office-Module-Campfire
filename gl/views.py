"""The General ledger page: link entities and accounts, review what will post, send it, follow it, tie out."""

from datetime import date
from typing import Any

import requests
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.client import CampfireClient, CampfireError, NotConnected
from campfire_integration.models import Workspace
from entities.models import Entity
from gl.models import AccountMapping, GLSettings, JournalPosting, Role
from gl.services import posting as svc
from gl.services.setup import account_label, list_accounts, list_entities, setup_accounts, setup_dimensions
from gl.services.tieout import tie_out

CAMPFIRE_ERRORS = (CampfireError, NotConnected, requests.RequestException)


def _client(workspace: Workspace) -> CampfireClient:
    return CampfireClient(workspace)


def _int(value: Any) -> int | None:
    return int(value) if str(value or "").isdigit() else None


def _handle_post(request: HttpRequest, workspace: Workspace) -> tuple[str, Any]:
    """Run the action; returns (message, extra context)."""
    action = request.POST.get("action", "")
    client = _client(workspace)
    actor = getattr(request, "fo_actor", "")
    today = timezone.localdate()
    if action == "link":
        cf_entities = {int(e["id"]): e for e in list_entities(client)}
        changed = 0
        for entity in Entity.objects.filter(family__workspace=workspace):
            value = _int(request.POST.get(f"entity_{entity.pk}"))
            if value is not None and value not in cf_entities:
                continue
            if entity.campfire_entity_id != value:
                entity.campfire_entity_id = value
                entity.save(update_fields=["campfire_entity_id"])
                changed += 1
        return f"Saved {changed} entity link{'s' if changed != 1 else ''}.", None
    if action == "auto_link":
        by_name: dict[str, list[int]] = {}
        for e in list_entities(client):
            by_name.setdefault(" ".join(str(e.get("name", "")).lower().split()), []).append(int(e["id"]))
        linked = 0
        for entity in Entity.objects.filter(family__workspace=workspace, campfire_entity_id__isnull=True):
            ids = by_name.get(" ".join(entity.name.lower().split()), [])
            if len(ids) == 1:  # only an unambiguous match
                entity.campfire_entity_id = ids[0]
                entity.save(update_fields=["campfire_entity_id"])
                linked += 1
        return f"Linked {linked} entit{'ies' if linked != 1 else 'y'} to the Campfire entity with the same name.", None
    if action == "map":
        accounts = {int(a["id"]): a for a in list_accounts(client)}
        for role in Role:
            value = _int(request.POST.get(f"role_{role.value}"))
            default = AccountMapping.objects.filter(workspace=workspace, role=role, campfire_entity_id=None, asset_class=None, account=None)
            if value is None:
                default.delete()
            elif value in accounts:
                default.update_or_create(defaults={"campfire_account_id": value, "campfire_account_label": account_label(accounts[value])},
                                         workspace=workspace, role=role, campfire_entity_id=None, asset_class=None, account=None)
        return "Saved the account mapping.", None
    if action == "post_from":
        gl_settings = GLSettings.for_workspace(workspace)
        raw = request.POST.get("post_from", "").strip()
        try:
            gl_settings.post_from = date.fromisoformat(raw) if raw else None
        except ValueError:
            return "Enter the date as YYYY-MM-DD.", None
        gl_settings.save()
        return (f"Activity before {gl_settings.post_from} won't be posted." if gl_settings.post_from else "All activity will be posted."), None
    if action in ("setup_preview", "setup_create"):
        create = action == "setup_create"
        steps = setup_accounts(workspace, client, create=create) + setup_dimensions(workspace, client, create=create)
        return ("Set up the GL accounts and dimensions in Campfire." if create else ""), {"setup_steps": steps, "setup_is_preview": not create}
    if action == "send":
        sent = svc.send(workspace, through=today, client=client, created_by=actor, created_by_id=getattr(request, "fo_actor_id", ""))
        counts = {s: sum(1 for p in sent if p.status == s) for s in JournalPosting.Status}
        parts = [f"{n} {JournalPosting.Status(s).label.lower()}" for s, n in counts.items() if n]
        return (f"Sent {len(sent)} journal entr{'ies' if len(sent) != 1 else 'y'}: {', '.join(parts)}." if sent else "Nothing to send."), None
    if action == "refresh":
        n = svc.refresh_statuses(workspace, client)
        return f"{n} entr{'ies' if n != 1 else 'y'} found posted in Campfire.", None
    if action == "release":
        posting = JournalPosting.objects.filter(workspace=workspace, pk=_int(request.POST.get("posting"))).first()
        if posting and posting.status in (JournalPosting.Status.SUBMITTED, JournalPosting.Status.UNCERTAIN):
            svc.release(posting, f"Released by {actor or 'a user'}: not in Campfire (rejected or never received).")
            return f"Released {posting.ref_number}; its transactions will post with the next run.", None
        return "Only an entry awaiting approval or unanswered can be released.", None
    if action == "tieout":
        return "", {"tieout": tie_out(workspace, today, client)}
    return "Unknown action.", None


def gl_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    message, extra, error = "", {}, ""
    if request.method == "POST":
        try:
            message, result = _handle_post(request, workspace)
            extra = result or {}
        except CAMPFIRE_ERRORS as e:
            error = f"Campfire request failed: {e}"
    elif request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])

    today = timezone.localdate()
    preview = svc.build_preview(workspace, through=today)
    cf_entities: list[dict[str, Any]] = []
    cf_accounts: list[dict[str, Any]] = []
    campfire_error = ""
    try:
        client = _client(workspace)
        cf_entities, cf_accounts = list_entities(client), list_accounts(client)
    except CAMPFIRE_ERRORS as e:
        campfire_error = f"Couldn't load entities and accounts from Campfire ({type(e).__name__}). Linking and mapping need the workspace connected."

    mapped = {m.role: m for m in AccountMapping.objects.filter(workspace=workspace, campfire_entity_id=None, asset_class=None, account=None)}
    narrower = AccountMapping.objects.filter(workspace=workspace).exclude(campfire_entity_id=None, asset_class=None, account=None).count()
    roles = [(r, mapped.get(r.value)) for r in Role]
    entities = Entity.objects.filter(family__workspace=workspace).select_related("family").order_by("family__name", "name")
    postings = JournalPosting.objects.filter(workspace=workspace)[:30]
    return render(request, "gl/overview.html", {
        "workspace": workspace, "framed": framed, "today": today, "preview": preview, "roles": roles, "narrower": narrower,
        "entities": entities, "cf_entities": cf_entities, "cf_accounts": cf_accounts, "campfire_error": campfire_error,
        "postings": postings, "message": message, "error": error, "frame_token": getattr(request, "fo_frame_token", ""),
        "action": pages.page_url(request, framed, "gl:gl"), "unmapped": [r for r, m in roles if m is None],
        "cf_entity_names": {int(e["id"]): e.get("name", "") for e in cf_entities}, "gl_settings": GLSettings.for_workspace(workspace), **extra,
    })


frame_gl = pages.framed(gl_page)
standalone_gl = pages.standalone(gl_page)
