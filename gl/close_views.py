"""The Month-end close page: every booked entity's close for a month, step by step, with the next action in place."""

from datetime import date, timedelta

import requests
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.client import CampfireClient, CampfireError, NotConnected
from campfire_integration.models import Workspace
from gl.services import close as svc
from gl.services import posting

CAMPFIRE_ERRORS = (CampfireError, NotConnected, requests.RequestException)


def _month(value: str | None, default: date) -> date:
    try:
        y, m = (value or "").split("-")
        return date(int(y), int(m), 1)
    except ValueError:
        return default


def _handle(request: HttpRequest, workspace: Workspace, month: date) -> str:
    action = request.POST.get("action", "")
    actor, actor_id = getattr(request, "fo_actor", ""), getattr(request, "fo_actor_id", "")
    cf = int(request.POST.get("entity") or 0)
    if cf and cf not in svc.booked_entities(workspace):
        raise svc.CloseRefused("That entity isn't booked in Campfire.")
    if action == "data":
        svc.confirm_data(workspace, cf, month, actor)
        return "Data confirmed complete."
    if action == "post":
        sent = posting.send(workspace, through=svc.month_end(month), client=CampfireClient(workspace), created_by=actor,
                            created_by_id=actor_id, campfire_entity_id=cf)
        return f"Sent {len(sent)} entr{'ies' if len(sent) != 1 else 'y'} to Campfire." if sent else "Nothing to send."
    if action == "refresh":
        n = posting.refresh_statuses(workspace, CampfireClient(workspace))
        return f"{n} entr{'ies' if n != 1 else 'y'} found posted in Campfire."
    if action == "tieout":
        recs = svc.run_tieout(workspace, month, CampfireClient(workspace), actor)
        bad = sum(1 for r in recs if not r.tieout_ok)
        return "Everything ties." if not bad else f"{bad} entit{'ies' if bad != 1 else 'y'} don't tie; see the details."
    if action == "signoff":
        svc.sign_off(workspace, cf, month, actor, actor_id, request.POST.get("note", "").strip())
        return f"{month:%B %Y} signed off."
    if action == "reopen":
        svc.reopen(workspace, cf, month, actor, request.POST.get("reason", ""))
        return f"{month:%B %Y} reopened."
    raise svc.CloseRefused("Unknown action.")


def close_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    today = timezone.localdate()
    month = _month(request.GET.get("month") or request.POST.get("month"), svc.previous_month(today))
    message = error = ""
    if request.method == "POST":
        try:
            message = _handle(request, workspace, month)
        except svc.CloseRefused as e:
            error = str(e)
        except CAMPFIRE_ERRORS as e:
            error = f"Campfire request failed: {e}"
    elif request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])

    rows = svc.month_status(workspace, month)
    steps = [s for r in rows for s in r.steps]
    done = sum(1 for s in steps if s.done)
    progress = {"done": done, "total": len(steps), "pct": round(done * 100 / len(steps)) if steps else 0, "closed": sum(1 for r in rows if r.closed)}
    base = pages.page_url(request, framed, "gl:close")
    sep = "&" if "?" in base else "?"
    nav = {"prev": f"{base}{sep}month={svc.previous_month(month):%Y-%m}",
           "next": f"{base}{sep}month={svc.month_end(month) + timedelta(days=1):%Y-%m}"}
    return render(request, "gl/close.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "month": month,
        "month_end": svc.month_end(month), "rows": rows, "message": message, "error": error, "nav": nav,
        "is_future": month > svc.month_start(today), "action": base,
        "links": {"data": pages.page_url(request, framed, "investments:imports"), "review": pages.page_url(request, framed, "records:review"),
                  "gl": pages.page_url(request, framed, "gl:gl")},
        "all_closed": bool(rows) and all(r.closed for r in rows),
        "progress": progress,
    })


frame_close = pages.framed(close_page)
standalone_close = pages.standalone(close_page)
