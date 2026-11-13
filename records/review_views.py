"""The Review page: decide on held marks, sign off imports that came in with warnings, see uploads left waiting."""

from decimal import Decimal, InvalidOperation

from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render

from campfire_integration import pages
from campfire_integration.models import Workspace
from records import review


def _int(value: str | None) -> int:
    return int(value) if (value or "").isdigit() else 0


def review_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    reviewer = review.Reviewer(getattr(request, "fo_actor", ""), getattr(request, "fo_actor_id", ""))
    message = error = ""
    if request.method == "POST":
        action, note = request.POST.get("action", ""), request.POST.get("note", "").strip()
        try:
            if action == "accept":
                v = review.accept(workspace, _int(request.POST.get("valuation")), reviewer, note)
                message = f"Accepted: {v.instrument} {v.as_of} at {v.value:,}."
            elif action == "reject":
                v = review.reject(workspace, _int(request.POST.get("valuation")), reviewer, note)
                message = f"Rejected: {v.instrument} {v.as_of}."
            elif action == "correct":
                try:
                    value = Decimal(request.POST.get("value", "").replace(",", "").strip())
                except InvalidOperation:
                    raise review.ReviewRefused("Enter the corrected value as a number.") from None
                old, new = review.correct(workspace, _int(request.POST.get("valuation")), value, reviewer, note)
                message = f"Recorded {new.value:,} for {new.instrument} {new.as_of}" + (
                    "; it is held for review too (still far from the expected value)." if new.status == new.Status.HELD else ".")
            elif action == "sign_off":
                b = review.sign_off(workspace, _int(request.POST.get("batch")), reviewer, note)
                message = f"Signed off {b.file_name or b.reference}."
            elif action == "policy":
                s = review.set_policy(workspace, request.POST.get("require_second_person") == "on", reviewer)
                message = "Preparer ≠ reviewer is on." if s.require_second_person else "Preparer ≠ reviewer is off: anyone may review their own entries."
        except review.ReviewRefused as e:
            error = str(e)
    elif request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])

    q = review.queue(workspace)
    links = {}
    for b in q.warned + q.waiting:
        name = "records:onboarding_batch" if b.source == "onboarding" else "investments:import_detail"
        links[b.pk] = pages.page_url(request, framed, name, b.pk)
    return render(request, "records/review.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "queue": q,
        "links": links, "message": message, "error": error, "reviewer": reviewer,
        "action": pages.page_url(request, framed, "records:review"),
    })


frame_review = pages.framed(review_page)
standalone_review = pages.standalone(review_page)
