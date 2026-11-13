from django import template
from django.urls import NoReverseMatch, reverse

register = template.Library()

# Standalone sidebar: (group, label, url name). Inside Campfire, Campfire's own sidebar lists the same pages.
NAV = [
    ("", "Home", "home"),
    ("Family", "Investments", "investments:investments"),
    ("Family", "Entities & Ownership", "entities:entities"),
    ("Operations", "Capital calls", "gl:calls"),
    ("Operations", "Imports", "investments:imports"),
    ("Operations", "Review", "records:review"),
    ("Operations", "Month-end close", "gl:close"),
    ("Books", "General ledger", "gl:gl"),
    ("Data", "Records", "records:home"),
]


@register.inclusion_tag("includes/sidebar.html", takes_context=True)
def sidebar(context):  # type: ignore[no-untyped-def]
    request = context["request"]
    items, last_group = [], None
    for group, label, name in NAV:
        try:
            url = reverse(name)
        except NoReverseMatch:
            continue
        active = request.path == url if url == "/" else request.path.startswith(url)
        items.append({"group": group if group != last_group else "", "label": label, "url": url, "active": active})
        last_group = group
    return {"items": items, "request": request, "workspace": context.get("workspace"), "csrf_token": context.get("csrf_token")}
