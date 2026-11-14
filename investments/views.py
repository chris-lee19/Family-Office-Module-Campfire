from dataclasses import dataclass, field
from typing import Any
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.models import Workspace
from entities.demo import seed_alder
from entities.models import Family
from investments.demo import seed_alder_investments, seed_price_history
from investments.models import CapitalCall, Valuation
from investments.services.portfolio import Portfolio, build_portfolio
from investments.services.privates import CommitmentSummary, irr
from investments.services.valuation import held_valuations


@dataclass
class Breakdown:
    label: str
    value: Decimal
    pct: Decimal


def breakdown(bucket: dict[str, Decimal], total: Decimal) -> list[Breakdown]:
    rows = [Breakdown(k, v, (v / total * 100).quantize(Decimal("0.1")) if total else Decimal(0)) for k, v in bucket.items()]
    return sorted(rows, key=lambda r: -r.value)


@dataclass
class FamilyInvestments:
    family: Family
    portfolio: Portfolio
    held: list[Valuation]
    calls: list[CapitalCall]
    privates: list[tuple[CommitmentSummary, Decimal | None]]  # (summary, IRR)
    by_person: list[Breakdown]
    by_entity: list[Breakdown]
    by_asset_class: list[Breakdown]
    by_liquidity: list[Breakdown]
    by_tax_treatment: list[Breakdown]
    trend: Any = None  # the value at each of the last twelve month ends, drawn as a line
    net_worth: Any = None  # gl.services.networth.NetWorth
    net_by_person: list[Breakdown] = field(default_factory=list)

    @property
    def attention_count(self) -> int:
        return len(self.held) + len(self.portfolio.stale) + len(self.calls)


def family_investments(family: Family, as_of: date) -> FamilyInvestments:
    from gl.services.networth import build_net_worth

    nw = build_net_worth(family, as_of)
    p = nw.portfolio
    calls = list(
        CapitalCall.objects.filter(commitment__account__family=family)
        .exclude(status__in=[CapitalCall.Status.PAID, CapitalCall.Status.CANCELLED])
        .select_related("commitment__instrument", "commitment__account")
    )
    return FamilyInvestments(
        family=family,
        portfolio=p,
        held=held_valuations(family),
        calls=calls,
        privates=[(c, irr(c.commitment, as_of, c.nav)) for c in p.commitments],
        by_person=breakdown(p.by_person, p.total),
        by_entity=breakdown(p.by_entity, p.total),
        by_asset_class=breakdown(p.by_asset_class, p.total),
        by_liquidity=breakdown(p.by_liquidity, p.total),
        by_tax_treatment=breakdown(p.by_tax_treatment, p.total),
        net_worth=nw,
        net_by_person=breakdown(nw.by_person, nw.net) if nw.lines else [],
    )


def value_trend(family: Family, as_of: date, months: int = 12) -> dict[str, Any]:
    """The investments' value at each month end, from the investment records as they stood then."""
    from gl.services import charts
    from gl.services.liquidity import add_months, month_end
    from investments.services.portfolio import build_portfolio

    first = add_months(as_of.replace(day=1), -(months - 1))
    points = []
    for n in range(months):
        when = min(month_end(add_months(first, n)), as_of)
        total = build_portfolio(family, when).total
        if total or points:  # start where the records start
            label = charts.month_label(when, not points)
            points.append(({**label, "label": f"{when:%b %-d, %Y}" if when == as_of else label["label"]}, [total]))
    return charts.trend(points, ["Investments"]) if len(points) > 1 else {}


def demo_url(request: HttpRequest, workspace: Workspace, framed: bool) -> str:
    """Where the "Load the demo family" button posts, when demo data is allowed and not loaded yet."""
    if not settings.ALLOW_DEMO_DATA or Family.objects.filter(workspace=workspace, code="alder").exists():
        return ""
    return pages.page_url(request, framed, "investments:load_demo")


def investments_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    as_of = timezone.localdate()
    message = ""
    if request.method == "POST" and request.POST.get("action") == "refresh_balances":
        import requests

        from campfire_integration.client import CampfireClient, CampfireError, NotConnected
        from gl.services.networth import refresh_balances

        try:
            n = len(refresh_balances(workspace, CampfireClient(workspace), as_of))
            message = f"Read balances for {n} Campfire entit{'ies' if n != 1 else 'y'}."
        except (CampfireError, NotConnected, requests.RequestException) as e:
            message = f"Couldn't read balances from Campfire ({type(e).__name__})."
    elif request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    families = [family_investments(f, as_of) for f in Family.objects.filter(workspace=workspace, is_active=True)]
    for fi in families:
        fi.trend = value_trend(fi.family, as_of)
    return render(request, "investments/overview.html", {
        "workspace": workspace, "families": families, "as_of": as_of, "framed": framed,
        "demo_url": demo_url(request, workspace, framed), "review_url": pages.page_url(request, framed, "records:review"),
        "message": message, "action": pages.page_url(request, framed, "investments:investments"),
        "frame_token": getattr(request, "fo_frame_token", ""), "calls_url": pages.page_url(request, framed, "gl:calls"),
    })


def load_demo_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not settings.ALLOW_DEMO_DATA:
        raise Http404
    with transaction.atomic():
        Workspace.objects.select_for_update().get(pk=workspace.pk)  # one load at a time per workspace
        if not Family.objects.filter(workspace=workspace, code="alder").exists():
            family = seed_alder(workspace)
            seed_alder_investments(family, timezone.localdate())
            seed_price_history(family, timezone.localdate())  # so returns spread over the months they were earned in
    return redirect(pages.page_url(request, framed, "investments:investments"))


frame_investments = pages.framed(investments_page)
standalone_investments = pages.standalone(investments_page)
frame_load_demo = pages.framed(load_demo_page)
standalone_load_demo = pages.standalone(load_demo_page)
