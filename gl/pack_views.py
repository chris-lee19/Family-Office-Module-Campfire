"""The Reporting pack page: choose the period and the sections, build the pack, print it."""

from typing import Any
from urllib.parse import urlencode

from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.client import CampfireClient
from campfire_integration.models import Workspace
from gl.services import pack as svc
from gl.services import statements as st
from investments.services.performance import PERIODS


def _client(workspace: Workspace) -> CampfireClient:
    return CampfireClient(workspace)


def pack_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    today = timezone.localdate()
    g = request.GET
    period = g.get("period") if g.get("period") in st.PERIODS else "ytd"
    start, end = st.period_dates(period, today, g.get("start", ""), g.get("end", ""))
    end = min(end, today)
    chosen = tuple(k for k in svc.KEYS if k in g.getlist("s")) or svc.KEYS
    built = svc.build(workspace, _client(workspace), start, end, today, chosen) if g.get("build") else None
    base = pages.page_url(request, framed, "gl:pack")
    return render(request, "gl/pack.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "today": today, "pack": built,
        "action": base.split("?")[0], "periods": st.PERIODS, "choices": {"period": period, "start": start.isoformat(), "end": end.isoformat()},
        "sections": [(k, title, about, k in chosen) for k, title, about, _ in svc.SECTIONS], "start": start, "end": end,
        "performance_periods": PERIODS, "as_of": end,
        "same_url": f"{base}{'&' if '?' in base else '?'}{urlencode([('period', period), ('start', start.isoformat()), ('end', end.isoformat())] + [('s', k) for k in chosen])}",
    })


frame_pack = pages.framed(pack_page)
standalone_pack = pages.standalone(pack_page)
