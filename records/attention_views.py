"""Home: the day's dashboard. What needs a person (most urgent first, each with a button to the page that fixes it),
the headline numbers, the allocation, last month's close, the next capital calls and the latest changes."""

from decimal import Decimal

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.models import Workspace
from gl.services import close
from investments.models import CapitalCall
from investments.views import breakdown, demo_url
from records import attention
from records.models import AuditEntry


def attention_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    today = timezone.localdate()
    items = attention.collect(workspace)
    rows = [(i, pages.page_url(request, framed, i.target, *i.args)) for i in items]
    counts = {s: sum(1 for i in items if i.severity == s) for s in (attention.URGENT, attention.TODO, attention.INFO)}
    head = attention.headline(workspace)

    last_month = close.previous_month(today)
    close_rows = []
    for r in close.month_status(workspace, last_month):
        done = sum(1 for s in r.steps if s.done)
        close_rows.append({"label": r.label, "done": done, "total": len(r.steps), "pct": round(done * 100 / len(r.steps)) if r.steps else 0,
                           "closed": r.closed, "next": r.next_step.label if r.next_step else "", "problem": any(s.problem for s in r.steps)})
    calls = [{"call": c, "days": (c.due_date - today).days, "late": (today - c.due_date).days} for c in
             CapitalCall.objects.filter(commitment__account__family__workspace=workspace, status__in=CapitalCall.OPEN_STATUSES)
             .select_related("commitment__instrument", "commitment__account__entity").order_by("due_date")[:5]]
    url = lambda name, *args: pages.page_url(request, framed, name, *args)  # noqa: E731
    return render(request, "records/attention.html", {
        "workspace": workspace, "framed": framed, "rows": rows, "counts": counts, "head": head, "today": today,
        "demo_url": demo_url(request, workspace, framed),
        "allocation": breakdown(head.by_asset_class, head.total), "close_rows": close_rows, "close_month": last_month,
        "close_done": bool(close_rows) and all(r["closed"] for r in close_rows), "calls": calls,
        "calls_total": sum((c["call"].amount for c in calls), Decimal(0)),
        "recent": AuditEntry.objects.filter(workspace=workspace)[:6],
        "links": {"investments": url("investments:investments"), "calls": url("gl:calls"), "close": url("gl:close"), "records": url("records:home"),
                  "imports": url("investments:imports"), "new_call": url("records:new", "capital-calls"), "new_mark": url("records:new", "valuations")},
    })


frame_attention = pages.framed(attention_page)
standalone_attention = pages.standalone(attention_page)
