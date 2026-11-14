from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.frames import session_workspace
from campfire_integration.models import Workspace
from entities.models import Entity, Family, Person
from entities.services import structure
from entities.services.ownership import OwnershipCycleError, effective_ownership, ownership_issues


@dataclass
class EntityRow:
    entity: Entity
    direct_owners: list[tuple[str, Decimal]]
    look_through: list[tuple[str, Decimal]]  # (person or "Unallocated", percentage)


@dataclass
class FamilyView:
    family: Family
    rows: list[EntityRow] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    chart: structure.Structure | None = None


def _percent(fraction: Decimal) -> Decimal:
    return (fraction * 100).quantize(Decimal("0.01"))


def family_views(workspace: Workspace, as_of: date) -> list[FamilyView]:
    views: list[FamilyView] = []
    for family in Family.objects.filter(workspace=workspace, is_active=True):
        view = FamilyView(family)
        people = {p.id: p.display_name for p in Person.objects.filter(family=family)}
        try:
            effective = effective_ownership(family, as_of)
        except OwnershipCycleError as e:
            view.issues.append(str(e))
            effective = {}
        view.issues += [f"{i.entity.name}: owners total {i.total_percentage.normalize()}% ({i.kind})" for i in ownership_issues(family, as_of)]
        entities = Entity.objects.filter(family=family).prefetch_related(
            "owner_interests__owner_entity", "owner_interests__owner_person"
        )
        for entity in entities:
            direct = [
                (str(i.owner), i.percentage.normalize())
                for i in entity.owner_interests.all()
                if i.effective_from <= as_of and (i.effective_to is None or i.effective_to >= as_of)
            ]
            look_through = sorted(
                ((people.get(pid, "Unallocated") if pid else "Unallocated / non-family", _percent(share))
                 for pid, share in effective.get(entity.id, {}).items() if share > 0),
                key=lambda pair: -pair[1],
            )
            view.rows.append(EntityRow(entity, direct, look_through))
        view.chart = structure.build(family, as_of)
        views.append(view)
    return views


def entities_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    as_of = timezone.localdate()
    from investments.views import demo_url

    views = family_views(workspace, as_of)
    rows = [r for v in views for r in v.rows]

    return render(
        request,
        "entities/overview.html",
        {"workspace": workspace, "families": views, "as_of": as_of, "framed": framed,
         "demo_url": demo_url(request, workspace, framed),
         "stats": {"entities": len(rows), "booked": sum(1 for r in rows if r.entity.is_booked),
                   "people": Person.objects.filter(family__workspace=workspace).count(), "issues": sum(len(v.issues) for v in views)},
         "links": {"new_entity": pages.page_url(request, framed, "records:new", "entities"),
                   "new_ownership": pages.page_url(request, framed, "records:new", "ownership"),
                   "gl": pages.page_url(request, framed, "gl:gl")},
         "edit_urls": {r.entity.pk: pages.page_url(request, framed, "records:edit", "entities", r.entity.pk) for r in rows},
         "node_urls": {n.key: pages.page_url(request, framed, "records:edit", "people" if n.person else "entities", n.pk)
                       for v in views if v.chart for n in v.chart.nodes}},
    )


def home(request: HttpRequest) -> HttpResponse:
    """Signed in: what needs attention. Otherwise: sign in with Campfire."""
    if session_workspace(request) is None:
        return render(request, "campfire/login.html")
    from records.attention_views import standalone_attention

    return standalone_attention(request)


frame_entities = pages.framed(entities_page)
standalone_entities = pages.standalone(entities_page)
