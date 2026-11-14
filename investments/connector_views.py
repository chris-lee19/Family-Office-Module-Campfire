"""The Connectors page: what brings data in today, and the tools that can be asked for."""

from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import render

from campfire_integration import pages
from campfire_integration.models import Workspace
from investments import connectors as cat
from investments.models import Account, ConnectorRequest, ImportBatch
from records.models import AuditEntry


def connectors_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    message = error = ""
    actor = getattr(request, "fo_actor", "")
    if request.method == "POST":
        connector = cat.BY_KEY.get(request.POST.get("connector", ""))
        if connector is None or connector.status != cat.ON_REQUEST:
            error = "Choose a connector that can be requested."
        elif request.POST.get("action") == "withdraw":
            ConnectorRequest.objects.filter(workspace=workspace, connector=connector.key).delete()
            message = f"{connector.name}: request withdrawn."
        else:
            ConnectorRequest.objects.update_or_create(workspace=workspace, connector=connector.key, defaults={
                "note": request.POST.get("note", "").strip()[:255], "requested_by": actor})
            AuditEntry.objects.create(workspace=workspace, actor=actor, action=AuditEntry.Action.CREATED, kind="connectors", object_id=connector.key,
                                      object_repr=f"Connector requested: {connector.name}", changes={"requested": ["", connector.name]})
            message = f"{connector.name} requested. Until it is built, its export files load through Imports."
    requests = {r.connector: r for r in ConnectorRequest.objects.filter(workspace=workspace)}
    groups = []
    for group in cat.GROUPS:
        items = [{"c": c, "request": requests.get(c.key), "url": pages.page_url(request, framed, c.target) if c.target else ""}
                 for c in cat.CATALOG if c.group == group]
        groups.append((group, items))
    last_import = ImportBatch.objects.filter(workspace=workspace, status=ImportBatch.Status.COMMITTED).exclude(source="demo").order_by("-committed_at").first()
    custody = Account.objects.filter(family__workspace=workspace, tracks_cash=True)
    return render(request, "investments/connectors.html", {
        "workspace": workspace, "framed": framed, "frame_token": getattr(request, "fo_frame_token", ""), "message": message, "error": error,
        "groups": groups, "action": pages.page_url(request, framed, "investments:connectors"),
        "stats": {"working": sum(1 for c in cat.CATALOG if c.status != cat.ON_REQUEST), "requested": len(requests),
                  "last_import": last_import, "custody": custody.count(), "custody_numbered": custody.exclude(external_id="").count()},
    })


frame_connectors = pages.framed(connectors_page)
standalone_connectors = pages.standalone(connectors_page)
