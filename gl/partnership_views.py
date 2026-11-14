"""Partnerships & K-1s: capital accounts per partner, the K-1 figures to issue, and the K-1s the family is waiting for."""

from typing import Any
from urllib.parse import urlencode

import requests
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.client import CampfireClient, CampfireError, NotConnected
from campfire_integration.models import Workspace
from gl.services import partnership as svc
from investments.models import K1
from records.models import AuditEntry

CAMPFIRE_ERRORS = (CampfireError, NotConnected, requests.RequestException)
VIEWS = {"capital": "Capital account statements", "issue": "K-1s to issue", "receive": "K-1s to receive"}


def _client(workspace: Workspace) -> CampfireClient:
    return CampfireClient(workspace)


def partnerships_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    today = timezone.localdate()
    params = request.POST if request.method == "POST" else request.GET
    view = params.get("view") if params.get("view") in VIEWS else "capital"
    message = error = ""
    if request.method == "POST" and request.POST.get("action") == "k1":
        try:
            k1 = svc.record_k1(workspace, request.POST, getattr(request, "fo_actor", ""))
            AuditEntry.objects.create(workspace=workspace, actor=getattr(request, "fo_actor", ""), action=AuditEntry.Action.UPDATED, kind="k1s",
                                      object_id=str(k1.pk), object_repr=str(k1)[:255], changes={"status": ["", k1.get_status_display()]})
            message = f"{k1.instrument} K-1 for {k1.tax_year}: {k1.get_status_display().lower()}."
        except ValueError as e:
            error = str(e)

    entities = svc.partnerships(workspace)
    chosen = next((e for e in entities if str(e.pk) == params.get("entity")), entities[0] if entities else None)
    years = list(range(today.year, today.year - 5, -1))
    year = int(params.get("year")) if str(params.get("year") or "").isdigit() and int(params["year"]) in years else today.year
    k1_years = svc.tax_years(workspace, today)
    tax_year = int(params.get("tax_year")) if str(params.get("tax_year") or "").isdigit() and int(params["tax_year"]) in k1_years else (k1_years[0] if k1_years else today.year - 1)

    statement = None
    if view in ("capital", "issue") and chosen is not None:
        if chosen.campfire_entity_id is None:
            error = error or f"{chosen.name} isn't linked to its books in Campfire yet (General ledger → Setup), so there are no figures to split."
        else:
            try:
                statement = svc.capital_accounts(_client(workspace), chosen, year, today)
            except CAMPFIRE_ERRORS as e:
                error = error or f"Couldn't read {chosen.name}'s books from Campfire ({type(e).__name__})."
    expected = svc.expected_k1s(workspace, tax_year) if view == "receive" else []

    base = pages.page_url(request, framed, "gl:partnerships")
    current: dict[str, Any] = {"view": view, "entity": chosen.pk if chosen else "", "year": year, "tax_year": tax_year}
    link = lambda **changes: f"{base}{'&' if '?' in base else '?'}{urlencode({**current, **changes})}"  # noqa: E731
    return render(request, "gl/partnerships.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "today": today, "message": message, "error": error,
        "view": view, "views": [(k, label, link(view=k), k == view) for k, label in VIEWS.items()], "entities": entities, "chosen": chosen,
        "years": years, "year": year, "k1_years": k1_years, "tax_year": tax_year, "statement": statement, "expected": expected,
        "received": sum(1 for k in expected if k.received), "statuses": K1.Status.choices, "action": base.split("?")[0], "post_action": base,
    })


frame_partnerships = pages.framed(partnerships_page)
standalone_partnerships = pages.standalone(partnerships_page)
