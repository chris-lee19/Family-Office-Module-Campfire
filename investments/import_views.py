"""The Imports page: upload a custodian file, review what validation found, then import or discard it."""

from dataclasses import dataclass

from django.http import Http404, HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from campfire_integration import pages
from campfire_integration.models import Workspace
from entities.models import Family
from investments.demo import alder_activity_file, alder_price_file
from investments.importers.custodian import ImportFailed, Result, commit, discard, stage, validate
from investments.importers.formats import FormatError, available_formats, resolve_format
from investments.models import ImportBatch, ImportIssue, Price, Transaction

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
PREVIEW_ROWS = 200
B = ImportBatch.Status
SEVERITY_ORDER = {ImportIssue.Severity.ERROR: 0, ImportIssue.Severity.WARNING: 1, ImportIssue.Severity.INFO: 2}


@dataclass
class PreviewRow:
    line: int | None
    trade_date: object
    account: str
    kind: str
    security: str
    quantity: object
    amount: object
    fees: object


def _has_demo_family(workspace: Workspace) -> bool:
    return Family.objects.filter(workspace=workspace, code="alder").exists()


def imports_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    formats = available_formats(workspace)
    error = ""
    if request.method == "POST":
        upload = request.FILES.get("file")
        try:
            fmt = resolve_format(workspace, request.POST.get("format", ""))
        except FormatError as e:
            error = str(e)
        else:
            if upload is None or upload.size == 0:
                error = "Choose a file to upload."
            elif upload.size > MAX_UPLOAD_BYTES:
                error = f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
            else:
                batch = stage(
                    workspace, fmt=fmt, content=upload.read(), file_name=upload.name[:255],
                    created_by=getattr(request, "fo_actor", ""), created_by_id=getattr(request, "fo_actor_id", ""),
                    create_securities=request.POST.get("create_securities") == "on",
                )
                return redirect(pages.page_url(request, framed, "investments:import_detail", batch.pk))
    elif request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])

    batches = list(ImportBatch.objects.filter(workspace=workspace).exclude(source="demo").select_related("family")[:25])
    for b in batches:
        b.url = pages.page_url(request, framed, "investments:import_detail", b.pk)  # type: ignore[attr-defined]
    samples = []
    if _has_demo_family(workspace):
        samples = [
            ("Sample activity file", pages.page_url(request, framed, "investments:import_sample", "activity")),
            ("Sample price file", pages.page_url(request, framed, "investments:import_sample", "prices")),
        ]
    return render(request, "investments/imports.html", {
        "workspace": workspace, "framed": framed, "formats": formats, "batches": batches, "error": error,
        "samples": samples, "action": pages.page_url(request, framed, "investments:imports"),
        "frame_token": getattr(request, "fo_frame_token", ""),
    }, status=400 if error else 200)


def _preview(batch: ImportBatch) -> tuple[list[PreviewRow], int]:
    """What the batch will import (validated) or did import (committed)."""
    if batch.status == B.COMMITTED:
        if batch.kind == ImportBatch.Kind.PRICES:
            prices = Price.objects.filter(batch=batch).select_related("instrument").order_by("instrument__name", "as_of")
            rows = [PreviewRow(None, p.as_of, "", "Price", str(p.instrument), None, p.price, None) for p in prices[:PREVIEW_ROWS]]
            return rows, prices.count()
        txns = Transaction.objects.filter(batch=batch).select_related("account", "instrument").order_by("trade_date", "id")
        rows = [PreviewRow(None, t.trade_date, t.account.name, t.get_txn_type_display(), str(t.instrument or ""), t.quantity, t.amount, t.fees)
                for t in txns[:PREVIEW_ROWS]]
        return rows, txns.count()
    if batch.status != B.VALIDATED:
        return [], 0
    try:
        result: Result = validate(batch, resolve_format(batch.workspace, batch.file_format), write=False)
    except FormatError:
        return [], 0
    rows = [
        PreviewRow(t.row, t.trade_date, t.account.name, Transaction.Type(t.txn_type).label,
                   str(t.instrument or (t.new_security.name + " (new)" if t.new_security else "")), t.quantity, t.amount, t.fees)
        for t in result.txns[:PREVIEW_ROWS]
    ] + [PreviewRow(p.row, p.as_of, "", "Price", str(p.instrument), None, p.price, None) for p in result.prices[:PREVIEW_ROWS]]
    return rows, result.rows_new


def import_detail_page(request: HttpRequest, workspace: Workspace, framed: bool, batch_id: int) -> HttpResponse:
    batch = get_object_or_404(ImportBatch, pk=batch_id, workspace=workspace)
    message = ""
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "commit":
                batch = commit(batch)
                message = "Imported." if batch.status == B.COMMITTED else "Validation failed when importing; nothing was imported."
            elif action == "discard":
                batch = discard(batch)
                message = "Discarded; nothing was imported."
        except (ImportFailed, FormatError) as e:
            message = str(e)
    elif request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])

    issues = sorted(batch.issues.all(), key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), i.row_number or 0, i.pk))
    rows, row_count = _preview(batch)
    return render(request, "investments/import_detail.html", {
        "workspace": workspace, "framed": framed, "batch": batch, "issues": issues, "rows": rows, "row_count": row_count,
        "error_count": sum(1 for i in issues if i.severity == ImportIssue.Severity.ERROR),
        "message": message, "action": pages.page_url(request, framed, "investments:import_detail", batch.pk),
        "back": pages.page_url(request, framed, "investments:imports"), "frame_token": getattr(request, "fo_frame_token", ""),
        "preview_limit": PREVIEW_ROWS,
    })


def import_sample_page(request: HttpRequest, workspace: Workspace, framed: bool, kind: str) -> HttpResponse:
    if not _has_demo_family(workspace) or kind not in ("activity", "prices"):
        raise Http404
    today = timezone.localdate()
    content = alder_activity_file(today) if kind == "activity" else alder_price_file(today)
    response = HttpResponse(content, content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="alder-{kind}-{today.isoformat()}.csv"'
    return response


frame_imports = pages.framed(imports_page)
standalone_imports = pages.standalone(imports_page)
frame_import_detail = pages.framed(import_detail_page)
standalone_import_detail = pages.standalone(import_detail_page)
frame_import_sample = pages.framed(import_sample_page)
standalone_import_sample = pages.standalone(import_sample_page)
