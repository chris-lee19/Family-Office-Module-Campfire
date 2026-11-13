from dataclasses import dataclass
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
from investments.demo import seed_alder_investments
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

    @property
    def attention_count(self) -> int:
        return len(self.held) + len(self.portfolio.stale) + len(self.calls)


def family_investments(family: Family, as_of: date) -> FamilyInvestments:
    p = build_portfolio(family, as_of)
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
    )


def demo_url(request: HttpRequest, workspace: Workspace, framed: bool) -> str:
    """Where the "Load the demo family" button posts, when demo data is allowed and not loaded yet."""
    if not settings.ALLOW_DEMO_DATA or Family.objects.filter(workspace=workspace, code="alder").exists():
        return ""
    return pages.page_url(request, framed, "investments:load_demo")


def investments_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    as_of = timezone.localdate()
    families = [family_investments(f, as_of) for f in Family.objects.filter(workspace=workspace, is_active=True)]
    return render(request, "investments/overview.html", {
        "workspace": workspace, "families": families, "as_of": as_of, "framed": framed,
        "demo_url": demo_url(request, workspace, framed), "review_url": pages.page_url(request, framed, "records:review"),
    })


def load_demo_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not settings.ALLOW_DEMO_DATA:
        raise Http404
    with transaction.atomic():
        Workspace.objects.select_for_update().get(pk=workspace.pk)  # one load at a time per workspace
        if not Family.objects.filter(workspace=workspace, code="alder").exists():
            seed_alder_investments(seed_alder(workspace), timezone.localdate())
    return redirect(pages.page_url(request, framed, "investments:investments"))


frame_investments = pages.framed(investments_page)
standalone_investments = pages.standalone(investments_page)
frame_load_demo = pages.framed(load_demo_page)
standalone_load_demo = pages.standalone(load_demo_page)
