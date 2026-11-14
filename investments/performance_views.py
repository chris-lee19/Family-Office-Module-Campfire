"""The Investment Performance Report page: time-weighted returns for priced holdings, IRR and multiples for private funds."""

from datetime import date
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.models import Workspace
from entities.models import Family
from gl.services import charts
from investments.demo import needs_price_history, seed_price_history
from investments.services import performance as svc
from records.models import AuditEntry


def growth_chart(report: svc.Performance) -> dict[str, Any]:
    """Growth of 100 over the last year: one point at the start and one at each month end."""
    points = []
    for n, (when, level) in enumerate(report.growth):
        label = charts.month_label(when, n == 0)
        points.append(({**label, "label": f"{when:%b %-d, %Y}"}, [level]))
    return charts.trend(points, ["Growth of 100"], plain=True) if len(points) > 2 else {}


def reports(workspace: Workspace, as_of: date) -> list[dict[str, Any]]:
    out = []
    for family in Family.objects.filter(workspace=workspace, is_active=True):
        report = svc.build(family, as_of)
        out.append({"report": report, "growth": growth_chart(report), "thin": needs_price_history(family) and settings.ALLOW_DEMO_DATA})
    return out


def performance_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    today = timezone.localdate()
    message = ""
    if request.method == "POST":
        if request.POST.get("action") != "demo_prices" or not settings.ALLOW_DEMO_DATA:
            return HttpResponseNotAllowed(["GET"])
        added = sum(seed_price_history(f, today) for f in Family.objects.filter(workspace=workspace, code="alder"))
        AuditEntry.objects.create(workspace=workspace, actor=getattr(request, "fo_actor", ""), action=AuditEntry.Action.CREATED, kind="prices",
                                  object_id="demo", object_repr="Demo price history", changes={"month-end prices added": [0, added]})
        message = f"Added {added} month-end demo prices. Returns now spread over the months they were earned in."
    try:
        as_of = min(date.fromisoformat(request.GET.get("as_of", "")), today)
    except ValueError:
        as_of = today
    return render(request, "investments/performance.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "today": today, "as_of": as_of,
        "families": reports(workspace, as_of), "periods": svc.PERIODS, "message": message, "action": pages.page_url(request, framed, "investments:performance"),
        "investments_url": pages.page_url(request, framed, "investments:investments"), "calls_url": pages.page_url(request, framed, "gl:calls"),
        "print_sub": f"As of {as_of:%B %-d, %Y}",
    })


frame_performance = pages.framed(performance_page)
standalone_performance = pages.standalone(performance_page)
