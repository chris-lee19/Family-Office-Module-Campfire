"""Onboarding screens: download the workbook, upload it with an opening-balance date, review, import."""

from datetime import date

from django.http import Http404, HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.models import Workspace
from entities.models import Family
from gl.models import GLSettings
from investments.models import ImportBatch
from records import onboarding

MAX_BYTES = 10 * 1024 * 1024
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(content: bytes, name: str) -> HttpResponse:
    r = HttpResponse(content, content_type=XLSX)
    r["Content-Disposition"] = f'attachment; filename="{name}"'
    return r


def onboarding_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    error = ""
    families = list(Family.objects.filter(workspace=workspace))
    if request.method == "POST":
        upload = request.FILES.get("file")
        family_choice = request.POST.get("family", "")
        try:
            as_of = date.fromisoformat(request.POST.get("as_of", ""))
        except ValueError:
            as_of = None
        if upload is None or not upload.size:
            error = "Choose the workbook to upload."
        elif upload.size > MAX_BYTES:
            error = "The workbook is larger than 10 MB."
        elif as_of is None:
            error = "Enter the opening-balance date."
        elif as_of > timezone.localdate():
            error = "The opening-balance date can't be in the future."
        elif family_choice != "new" and not any(str(f.pk) == family_choice for f in families):
            error = "Choose the family, or a new one."
        else:
            batch = onboarding.stage(
                workspace, content=upload.read(), file_name=upload.name[:255], as_of=as_of,
                family_id=int(family_choice) if family_choice != "new" else None,
                family_name=request.POST.get("family_name", ""), family_code=request.POST.get("family_code", ""),
                set_gl_start=request.POST.get("set_gl_start") == "on", created_by=getattr(request, "fo_actor", ""),
            )
            return redirect(pages.page_url(request, framed, "records:onboarding_batch", batch.pk))
    elif request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])

    batches = list(ImportBatch.objects.filter(workspace=workspace, source=onboarding.SOURCE)[:10])
    for b in batches:
        b.url = pages.page_url(request, framed, "records:onboarding_batch", b.pk)  # type: ignore[attr-defined]
    return render(request, "records/onboarding.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "error": error,
        "families": [(f, pages.page_url(request, framed, "records:onboarding_export", f.pk)) for f in families],
        "template_url": pages.page_url(request, framed, "records:onboarding_template"),
        "action": pages.page_url(request, framed, "records:onboarding"), "home_url": pages.page_url(request, framed, "records:home"),
        "batches": batches, "today": timezone.localdate(), "gl_start": GLSettings.for_workspace(workspace).post_from,
        "sheets": onboarding.SHEETS,
    }, status=400 if error else 200)


def onboarding_batch_page(request: HttpRequest, workspace: Workspace, framed: bool, batch_id: int) -> HttpResponse:
    batch = get_object_or_404(ImportBatch, pk=batch_id, workspace=workspace, source=onboarding.SOURCE)
    message = ""
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "commit":
                batch = onboarding.commit(batch)
                message = "Imported." if batch.status == ImportBatch.Status.COMMITTED else "A check failed when importing; nothing was imported."
            elif action == "discard" and batch.status == ImportBatch.Status.VALIDATED:
                batch.status = ImportBatch.Status.DISCARDED
                batch.save(update_fields=["status"])
                message = "Discarded; nothing was imported."
        except onboarding.OnboardingFailed as e:
            message = str(e)
    elif request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    created, existing = batch.options.get("created", {}), batch.options.get("existing", {})
    order = ["Family"]
    for s in onboarding.SHEETS:
        order += [s.name, *({"Accounts": ["Opening cash"], "Commitments": ["Fund flows to date"]}.get(s.name, []))]
    counts = []
    for name in order:
        new = sum(v for k, v in created.items() if k.split(":")[0] == name)
        old = sum(v for k, v in existing.items() if k.split(":")[0] == name)
        if new or old:
            counts.append((name, new, old))
    issues = sorted(batch.issues.all(), key=lambda i: ({"error": 0, "warning": 1}.get(i.severity, 2), i.code, i.row_number or 0))
    return render(request, "records/onboarding_batch.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "batch": batch,
        "counts": counts, "issues": issues, "message": message, "as_of": batch.options.get("as_of"),
        "error_count": sum(1 for i in issues if i.severity == "error"),
        "action": pages.page_url(request, framed, "records:onboarding_batch", batch.pk),
        "back": pages.page_url(request, framed, "records:onboarding"),
        "investments_url": pages.page_url(request, framed, "investments:investments"),
    })


def template_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    return _xlsx(onboarding.build_workbook(), "family-onboarding-template.xlsx")


def export_page(request: HttpRequest, workspace: Workspace, framed: bool, family_id: int) -> HttpResponse:
    family = Family.objects.filter(workspace=workspace, pk=family_id).first()
    if family is None:
        raise Http404
    as_of = timezone.localdate()
    return _xlsx(onboarding.build_workbook(onboarding.family_rows(family, as_of)), f"{family.code}-{as_of.isoformat()}.xlsx")


frame_onboarding = pages.framed(onboarding_page)
standalone_onboarding = pages.standalone(onboarding_page)
frame_onboarding_batch = pages.framed(onboarding_batch_page)
standalone_onboarding_batch = pages.standalone(onboarding_batch_page)
frame_template = pages.framed(template_page)
standalone_template = pages.standalone(template_page)
frame_export = pages.framed(export_page)
standalone_export = pages.standalone(export_page)
