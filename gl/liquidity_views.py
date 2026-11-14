"""The Liquidity and Cash Flow Forecast page: cash by month, entity by entity, and what the forecast is made of."""

import csv
from datetime import timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import requests
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.client import CampfireClient, CampfireError, NotConnected
from campfire_integration.models import Workspace
from gl.models import PlannedCashFlow
from gl.services import liquidity as svc

CAMPFIRE_ERRORS = (CampfireError, NotConnected, requests.RequestException)
ZERO = Decimal(0)
SOON_DAYS = 60


def _client(workspace: Workspace) -> CampfireClient:
    return CampfireClient(workspace)


def _int(v: Any) -> int:
    return int(v) if str(v or "").isdigit() else 0


def _with(base: str, params: dict[str, Any]) -> str:
    query = urlencode({k: v for k, v in params.items() if v not in ("", None, 0)})
    return f"{base}{'&' if '?' in base else '?'}{query}" if query else base


def _handle(request: HttpRequest, workspace: Workspace, names: dict[int, str]) -> str:
    action, actor = request.POST.get("action", ""), getattr(request, "fo_actor", "")
    if action == "add":
        item = svc.add(workspace, request.POST, names, actor)
        return f"Added to the forecast: {item.label}, {item.get_frequency_display().lower()}, {item.entity_label}."
    if action == "delete":
        item = svc.remove(workspace, _int(request.POST.get("item")), actor)
        return f"Removed from the forecast: {item.label} ({item.entity_label})."
    if action == "add_suggested":
        picked = [p for p in request.POST.getlist("pick") if p.isdigit()]
        if not picked:
            raise svc.LiquidityError("Tick the items to add.")
        with transaction.atomic():
            for n in picked:
                svc.add(workspace, request.POST, names, actor, prefix=f"s{n}-")
        return f"Added {len(picked)} recurring item{'s' if len(picked) != 1 else ''} from the books to the forecast."
    raise svc.LiquidityError("That action isn't available here.")


def chart(fc: svc.Forecast) -> dict[str, Any]:
    """Month-end cash as columns on one zero baseline: heights as a share of the tallest column each side of it."""
    top, bottom = max([*fc.closing, ZERO]), min([*fc.closing, ZERO])
    spread = top - bottom
    if not spread:
        return {}
    low = fc.closing.index(min(fc.closing))
    cols = []
    for n, (month, value) in enumerate(zip(fc.months, fc.closing)):
        opening = fc.opening if n == 0 else fc.closing[n - 1]
        cols.append({
            "month": month, "value": value, "negative": value < 0, "opening": opening, "money_in": fc.money_in[n], "money_out": fc.money_out[n],
            "height": float((abs(value) / (top if value >= 0 else -bottom) * 100).quantize(Decimal("0.1"))) if value else 0,
            "label": n in (0, low, len(fc.closing) - 1),  # the first, the lowest and the last: the rest are in the table
            "edge": "left" if n < 2 else "right" if n > len(fc.closing) - 3 else "",
        })
    return {"cols": cols, "up": float((top / spread * 100).quantize(Decimal("0.1"))), "down": float((-bottom / spread * 100).quantize(Decimal("0.1")))}


def _csv(fc: svc.Forecast, workspace: Workspace) -> HttpResponse:
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="liquidity-and-cash-flow-forecast-{fc.today.isoformat()}.csv"'
    out = csv.writer(response)
    amounts = lambda values: [f"{v:.2f}" for v in values]  # noqa: E731
    out.writerow(["Liquidity and Cash Flow Forecast", str(workspace), fc.entity or "All entities", f"as of {fc.today}"])
    out.writerow(["", *[m.label for m in fc.months], "Total"])
    out.writerow(["Cash at the start", *amounts([fc.opening, *fc.closing[:-1]]), ""])
    for title, lines, totals in (("Money in", fc.inflows, fc.money_in), ("Money out", fc.outflows, fc.money_out)):
        for line in lines:
            out.writerow([line.label, *amounts(line.values), f"{line.total:.2f}"])
        out.writerow([f"Total {title.lower()}", *amounts(totals), f"{sum(totals, ZERO):.2f}"])
    out.writerow(["Cash at the end", *amounts(fc.closing), ""])
    return response


def liquidity_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    today = timezone.localdate()
    q = request.POST if request.method == "POST" else request.GET
    horizon = _int(q.get("months")) if _int(q.get("months")) in svc.HORIZONS else 12
    pace = _int(q.get("pace")) if _int(q.get("pace")) in svc.PACES else 0
    entity_id = _int(q.get("entity")) or None
    base = pages.page_url(request, framed, "gl:liquidity")
    current = {"entity": entity_id, "months": horizon if horizon != 12 else "", "pace": pace}
    message = error = ""
    fc, names, suggestions = None, {}, None
    entered: dict[str, str] = {k: "" for k in ("entity", "label", "direction", "amount", "frequency", "start", "end")}
    try:
        client = _client(workspace)
        names = svc.entity_names(client)
        if request.method == "POST":
            try:
                message = _handle(request, workspace, names)
            except svc.LiquidityError as e:
                error = str(e)
                if request.POST.get("action") == "add":
                    entered = {k: request.POST.get(k, "") for k in entered}
        if request.GET.get("suggest") or (error and request.POST.get("action") == "add_suggested"):
            suggestions = svc.suggest(workspace, client, today)
        fc = svc.build(workspace, client, today, horizon, pace, entity_id if entity_id in names else None)
    except CAMPFIRE_ERRORS as e:
        error = error or f"Couldn't read the books from Campfire ({type(e).__name__}). Check that the workspace is connected."
    if fc is not None and request.method == "GET" and request.GET.get("format") == "csv":
        return _csv(fc, workspace)
    planned = list(PlannedCashFlow.objects.filter(workspace=workspace))
    return render(request, "gl/liquidity.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "fc": fc, "today": today,
        "message": message, "error": error, "names": sorted(names.items(), key=lambda kv: kv[1]), "planned": planned, "suggestions": suggestions,
        "chart": chart(fc) if fc else {}, "entered": entered, "reopen": bool(error) and request.POST.get("action") == "add",
        "choices": {"entity": entity_id or 0, "months": horizon, "pace": pace}, "horizons": svc.HORIZONS, "paces": svc.PACES,
        "action": base.split("?")[0], "post_action": _with(base, current), "csv_url": _with(base, {**current, "format": "csv"}),
        "suggest_url": _with(base, {**current, "suggest": 1}) + "#suggest", "directions": svc.D.choices, "frequencies": svc.F.choices,
        "cash_url": pages.page_url(request, framed, "gl:cash"), "bills_url": pages.page_url(request, framed, "gl:bills"),
        "calls_url": pages.page_url(request, framed, "gl:calls"), "journals_url": pages.page_url(request, framed, "gl:journals"),
        "tiers": _tiers(fc) if fc else [], **(_totals(fc) if fc else {}),
        "print_sub": f"{fc.entity or 'All entities'} · next {horizon} months · as of {today:%B %-d, %Y}" if fc else "",
    })


def _totals(fc: svc.Forecast) -> dict[str, Any]:
    soon = fc.due_soon
    cutoff = fc.today + timedelta(days=SOON_DAYS)
    return {"soon_days": SOON_DAYS, "soon": [f for f in fc.flows if f.when <= cutoff], "later": [f for f in fc.flows if f.when > cutoff][:600],
            "last_month": fc.months[-1].label, "due_soon_total": -sum((f.amount for f in soon), ZERO), "late": sum(1 for f in soon if f.late),
            "total_in": sum(fc.money_in, ZERO), "total_out": sum(fc.money_out, ZERO), "total_net": sum(fc.net, ZERO)}


class _Tier:
    def __init__(self, label: str, value: Decimal, total: Decimal) -> None:
        self.label, self.value = label, value
        self.pct = (value / total * 100).quantize(Decimal("0.1")) if total else ZERO


def _tiers(fc: svc.Forecast) -> list[_Tier]:
    total = sum((v for _, v in fc.tiers if v > 0), ZERO)
    return [_Tier(label, value, total) for label, value in fc.tiers if value]


frame_liquidity = pages.framed(liquidity_page)
standalone_liquidity = pages.standalone(liquidity_page)
