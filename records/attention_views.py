"""Home: what needs attention across the app, most urgent first, with a button to the page that fixes each."""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.models import Workspace
from records import attention


def attention_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    items = attention.collect(workspace)
    rows = [(i, pages.page_url(request, framed, i.target, *i.args)) for i in items]
    counts = {s: sum(1 for i in items if i.severity == s) for s in (attention.URGENT, attention.TODO, attention.INFO)}
    from investments.views import demo_url

    return render(request, "records/attention.html", {
        "workspace": workspace, "framed": framed, "rows": rows, "counts": counts, "head": attention.headline(workspace),
        "today": timezone.localdate(), "demo_url": demo_url(request, workspace, framed),
        "links": {"investments": pages.page_url(request, framed, "investments:investments"), "calls": pages.page_url(request, framed, "gl:calls")},
    })


frame_attention = pages.framed(attention_page)
standalone_attention = pages.standalone(attention_page)
