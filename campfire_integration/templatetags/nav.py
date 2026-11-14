"""The app shell: sidebar (standalone), top bar with search and quick actions (standalone and inside Campfire).

Links are built with `pages.page_url`, so inside Campfire they stay in the frame and carry the frame token.
"""

from typing import Any

from django import template
from django.urls import NoReverseMatch
from django.utils.safestring import mark_safe

from campfire_integration import pages

register = template.Library()

# (group, label, page name, icon, badge key)
NAV = [
    ("", "Home", "records:attention", "home", ""),
    ("Family", "Investments", "investments:investments", "chart", ""),
    ("Family", "Entities & Ownership", "entities:entities", "tree", ""),
    ("Family", "Partnerships & K-1s", "gl:partnerships", "partners", ""),
    ("Operations", "Capital calls", "gl:calls", "calls", "calls"),
    ("Operations", "Imports", "investments:imports", "upload", ""),
    ("Operations", "Review", "records:review", "check", "review"),
    ("Operations", "Month-end close", "gl:close", "calendar", ""),
    ("Books", "Cash", "gl:cash", "bank", ""),
    ("Books", "Liquidity forecast", "gl:liquidity", "trend", ""),
    ("Books", "Bills", "gl:bills", "bill", "bills"),
    ("Books", "Journal entries", "gl:journals", "journal", "journals"),
    ("Books", "Financial statements", "gl:statements", "report", ""),
    ("Books", "General ledger", "gl:gl", "book", ""),
    ("Data", "Connectors", "investments:connectors", "plug", ""),
    ("Data", "Records", "records:home", "database", ""),
    ("Data", "People & roles", "records:access", "people", ""),
]

# Quick actions: (label, hint, page name, args, fragment)
ACTIONS = [
    ("Write a journal entry", "For approval, then posted to the ledger", "gl:journals", (), "#enter"),
    ("Enter a bill", "For approval, then into the payables", "gl:bills", (), "#enter"),
    ("Add an expected receipt or payment", "A distribution, tax payment or other cash the forecast should count", "gl:liquidity", (), "#plan"),
    ("Upload a custodian file", "Transactions, prices or positions from the custodian", "investments:imports", (), ""),
    ("Record a capital call notice", "Then send it to Campfire as a bill", "records:new", ("capital-calls",), ""),
    ("Record a distribution", "Return of capital, income and gains from a fund", "gl:calls", (), "#distribution"),
    ("Add a NAV statement or appraisal", "A new mark for a fund, property or other holding", "records:new", ("valuations",), ""),
    ("Add a transaction", "A manual buy, sell, transfer, income or fee", "records:new", ("transactions",), ""),
    ("Onboard a family from a spreadsheet", "Structure, accounts and opening positions in one workbook", "records:onboarding", (), ""),
]

# 24x24 outline icons (stroke = currentColor).
ICONS = {
    "home": '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V20h14V9.5"/><path d="M10 20v-6h4v6"/>',
    "chart": '<path d="M4 20V10"/><path d="M10 20V4"/><path d="M16 20v-7"/><path d="M22 20H2"/>',
    "tree": '<rect x="9" y="3" width="6" height="5" rx="1"/><rect x="3" y="16" width="6" height="5" rx="1"/><rect x="15" y="16" width="6" height="5" rx="1"/><path d="M12 8v4M6 16v-4h12v4"/>',
    "calls": '<path d="M3 7h18v12H3z"/><path d="M3 11h18"/><path d="M7 15h4"/>',
    "upload": '<path d="M12 16V4"/><path d="m7 9 5-5 5 5"/><path d="M4 16v4h16v-4"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 3 3 5-6"/>',
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/><path d="m9 15 2 2 4-4"/>',
    "book": '<path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z"/><path d="M5 17a3 3 0 0 1 3-3h11"/>',
    "database": '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
    "bank": '<path d="M3 10 12 4l9 6"/><path d="M5 10v8M9.5 10v8M14.5 10v8M19 10v8"/><path d="M3 20h18"/>',
    "trend": '<path d="M3 17l5-5 4 3 8-8"/><path d="M15 7h5v5"/><path d="M3 21h18"/>',
    "bill": '<path d="M6 3h12v18l-3-2-3 2-3-2-3 2z"/><path d="M9 8h6M9 12h6"/>',
    "journal": '<path d="M5 4h14v16H5z"/><path d="M9 4v16"/><path d="M12 9h4M12 13h4"/>',
    "partners": '<circle cx="8" cy="8" r="3"/><circle cx="17" cy="9" r="2.5"/><path d="M2.5 20c.5-3.5 2.7-5.5 5.5-5.5s5 2 5.5 5.5"/><path d="M14.5 14.6c.8-.4 1.6-.6 2.5-.6 2.3 0 4 1.600 4.500 4.500"/>',
    "plug": '<path d="M9 3v5M15 3v5"/><path d="M6 8h12v3a6 6 0 0 1-12 0z"/><path d="M12 17v4"/>',
    "people": '<circle cx="12" cy="8" r="3.5"/><path d="M5 20c.6-4 3.300-6 7-6s6.400 2 7 6"/>',
    "report": '<path d="M6 3h9l4 4v14H6z"/><path d="M15 3v4h4"/><path d="M9 12h7M9 16h7M9 8h3"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "arrow": '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
}


@register.simple_tag
def icon(name: str, size: int = 18) -> str:
    return mark_safe(  # noqa: S308 - ICONS is a constant of this module
        f'<svg class="icon" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
        f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS.get(name, "")}</svg>')


def _badges(workspace: Any) -> dict[str, int]:
    from investments.models import CapitalCall, ImportBatch, Valuation

    review = (Valuation.objects.filter(account__family__workspace=workspace, status=Valuation.Status.HELD).count()
              + ImportBatch.objects.filter(workspace=workspace, status=ImportBatch.Status.VALIDATED).count())
    calls = CapitalCall.objects.filter(commitment__account__family__workspace=workspace, status__in=CapitalCall.OPEN_STATUSES).count()
    from gl.models import BillRequest, JournalRequest

    return {"review": review, "calls": calls, "bills": BillRequest.objects.filter(workspace=workspace, status=BillRequest.Status.PENDING).count(),
            "journals": JournalRequest.objects.filter(workspace=workspace, status=JournalRequest.Status.PENDING).count()}


def _search_index(request: Any, framed: bool, workspace: Any, nav: list[dict[str, Any]], actions: list[dict[str, Any]]) -> list[dict[str, str]]:
    """What the search box finds: pages, actions, record lists, and the workspace's entities, people, accounts and holdings."""
    from entities.models import Entity, Person
    from investments.models import Account, Instrument
    from records.registry import KINDS

    def bare(u: str) -> str:  # without the frame token: the page adds it, which keeps the index small
        path, _, rest = u.partition("?")
        return path + ("#" + rest.split("#", 1)[1] if "#" in rest else "")

    url = lambda name, *args: bare(pages.page_url(request, framed, name, *args))  # noqa: E731
    out = [{"t": n["label"], "s": "Page", "u": bare(n["url"]), "g": "Go to"} for n in nav]
    out += [{"t": a["label"], "s": a["hint"], "u": bare(a["url"]), "g": "Do"} for a in actions]
    out += [{"t": k.plural, "s": "Records", "u": url("records:list", k.slug), "g": "Records"} for k in KINDS.values()]
    cap = 300
    for e in Entity.objects.filter(family__workspace=workspace).select_related("family")[:cap]:
        out.append({"t": e.name, "s": f"Entity · {e.get_entity_type_display()} · {e.family}", "u": url("records:edit", "entities", e.pk), "g": "Entities"})
    for p in Person.objects.filter(family__workspace=workspace).select_related("family")[:cap]:
        out.append({"t": p.display_name, "s": f"Person · {p.family}", "u": url("records:edit", "people", p.pk), "g": "People"})
    for a in Account.objects.filter(family__workspace=workspace).select_related("family")[:cap]:
        out.append({"t": a.name, "s": f"Account · {a.family}", "u": url("records:edit", "accounts", a.pk), "g": "Accounts"})
    for i in Instrument.objects.filter(workspace=workspace)[:cap]:
        out.append({"t": i.name, "s": "Security / fund" + (f" · {i.ticker}" if getattr(i, "ticker", "") else ""), "u": url("records:edit", "securities", i.pk), "g": "Holdings"})
    return out


def shell_context(context: Any) -> dict[str, Any]:
    """Navigation, badges, quick actions and the search index for the current request (cached on the request)."""
    request = context["request"]
    cached = getattr(request, "_fo_shell", None)
    if cached is not None:
        return cached
    framed = bool(context.get("framed"))
    workspace = getattr(request, "fo_workspace", None)
    badges = _badges(workspace) if workspace is not None else {}
    nav, last_group, current = [], None, ""
    for group, label, name, icon_name, badge in NAV:
        try:
            url = pages.page_url(request, framed, name)
        except NoReverseMatch:
            continue
        path = url.split("?")[0]
        if not framed and name == "records:attention":
            url = path = "/"
        active = request.path == path or (path not in ("/", "/fo/records") and request.path.startswith(path + "/")) or (
            path == "/fo/records" and request.path.startswith("/fo/records"))
        if framed:
            active = request.path == path or request.path.startswith(path + "/")
        if active:
            current = label
        nav.append({"group": group if group != last_group else "", "label": label, "url": url, "icon": icon_name, "active": active,
                    "badge": badges.get(badge, 0)})
        last_group = group
    actions = []
    for label, hint, name, args, fragment in ACTIONS:
        try:
            actions.append({"label": label, "hint": hint, "url": pages.page_url(request, framed, name, *args) + fragment})
        except NoReverseMatch:
            continue
    user = request.session.get("campfire_user") or {} if not framed else {}
    who = user.get("name") or user.get("email") or getattr(request, "fo_actor", "") or ""
    shell = {
        "nav": nav, "actions": actions, "current": current, "framed": framed, "workspace": workspace, "who": who,
        "role": getattr(request, "fo_role_label", "") if getattr(request, "fo_roles_on", False) else "",
        "view_only": getattr(request, "fo_roles_on", False) and getattr(request, "fo_can", None) == frozenset({"view"}),
        "initials": "".join(w[0] for w in who.replace("@", " ").split()[:2]).upper() or "FO",
        "index": _search_index(request, framed, workspace, nav, actions) if workspace is not None else [],
        "request": request, "csrf_token": context.get("csrf_token"),
    }
    request._fo_shell = shell
    return shell


@register.inclusion_tag("includes/sidebar.html", takes_context=True)
def sidebar(context):  # type: ignore[no-untyped-def]
    return shell_context(context)


@register.inclusion_tag("includes/topbar.html", takes_context=True)
def topbar(context):  # type: ignore[no-untyped-def]
    return shell_context(context)
