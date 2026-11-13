"""Records: set up and maintain a family's data (people, entities, ownership, accounts, holdings, activity).

Every change runs in one database transaction with the record type's checks (the ledger must still replay,
ownership must not loop); a failed check undoes the change and shows why. Every change is written to the audit
trail with the values before and after.
"""

from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Model, ProtectedError, Q, RestrictedError
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import redirect, render

from campfire_integration import pages
from campfire_integration.models import Workspace
from entities.models import Family
from records.models import AuditEntry
from records.registry import GROUPS, KINDS, Kind

LIST_LIMIT = 300
SKIP_FIELDS = {"id", "workspace", "created_at", "updated_at"}


def _kind(slug: str) -> Kind:
    if slug not in KINDS:
        raise Http404
    return KINDS[slug]


def _display(obj: Model, name: str) -> str:
    getter = getattr(obj, f"get_{name}_display", None)
    value = getter() if callable(getter) else getattr(obj, name, None)
    return "" if value is None else str(value)


def _snapshot(obj: Model) -> dict[str, str]:
    return {f.verbose_name: _display(obj, f.name) for f in obj._meta.concrete_fields if f.name not in SKIP_FIELDS}


def _audit(workspace: Workspace, request: HttpRequest, kind: Kind, action: str, obj: Model, before: dict[str, str], after: dict[str, str]) -> None:
    changes = {k: [before.get(k, ""), after.get(k, "")] for k in sorted(set(before) | set(after)) if before.get(k, "") != after.get(k, "")}
    AuditEntry.objects.create(workspace=workspace, actor=getattr(request, "fo_actor", ""), action=action, kind=kind.slug,
                              object_id=str(obj.pk), object_repr=str(obj)[:255], changes=changes)


def _frame_ctx(request: HttpRequest, framed: bool) -> dict[str, Any]:
    return {"framed": framed, "frame_token": getattr(request, "fo_frame_token", "")}


def home_page(request: HttpRequest, workspace: Workspace, framed: bool) -> HttpResponse:
    groups = [(g, [(k, k.queryset(workspace).count(), pages.page_url(request, framed, "records:list", k.slug),
                    pages.page_url(request, framed, "records:new", k.slug)) for k in KINDS.values() if k.group == g]) for g in GROUPS]
    recent = AuditEntry.objects.filter(workspace=workspace)[:15]
    return render(request, "records/home.html", {"workspace": workspace, "groups": groups, "recent": recent, "kinds": KINDS,
                                                 "onboarding_url": pages.page_url(request, framed, "records:onboarding"),
                                                 **_frame_ctx(request, framed)})


def list_page(request: HttpRequest, workspace: Workspace, framed: bool, kind: str) -> HttpResponse:
    k = _kind(kind)
    qs = k.queryset(workspace)
    q = request.GET.get("q", "").strip()
    if q and k.search:
        cond = Q()
        for field in k.search:
            cond |= Q(**{f"{field}__icontains": q})
        qs = qs.filter(cond)
    families = list(Family.objects.filter(workspace=workspace)) if k.family_path else []
    family = request.GET.get("family", "")
    if family.isdigit() and k.family_path:
        qs = qs.filter(**{f"{k.family_path}__pk": int(family)})
    total = qs.count()
    rows = [(obj, [fn(obj) for _, fn in k.columns], pages.page_url(request, framed, "records:edit", k.slug, obj.pk)) for obj in qs[:LIST_LIMIT]]
    return render(request, "records/list.html", {
        "workspace": workspace, "kind": k, "rows": rows, "total": total, "limit": LIST_LIMIT, "q": q, "families": families,
        "family": family, "new_url": pages.page_url(request, framed, "records:new", k.slug),
        "list_url": pages.page_url(request, framed, "records:list", k.slug), "home_url": pages.page_url(request, framed, "records:home"),
        **_frame_ctx(request, framed),
    })


def edit_page(request: HttpRequest, workspace: Workspace, framed: bool, kind: str, pk: int | None = None) -> HttpResponse:
    k = _kind(kind)
    obj = None
    if pk is not None:
        obj = k.queryset(workspace).filter(pk=pk).first()  # scoped: another workspace's id is simply not found
        if obj is None:
            raise Http404
    lock = k.locked(obj) if obj is not None else ""
    if obj is not None and not k.editable and not lock:
        lock = "Records of this type aren't edited. Delete it and add a corrected one."
    list_url = pages.page_url(request, framed, "records:list", k.slug)
    note = error = ""

    if request.method == "POST" and request.POST.get("action") == "delete":
        if obj is None or k.locked(obj) or k.undeletable(obj):
            error = (k.locked(obj) or f"Can't delete: {k.undeletable(obj)}") if obj is not None else "Nothing to delete."
        else:
            before = _snapshot(obj)
            try:
                with transaction.atomic():
                    type(obj).objects.get(pk=obj.pk).delete()  # a copy: Django clears the pk of what it deletes, even on rollback
                    if k.check:
                        k.check(obj)
                    _audit(workspace, request, k, AuditEntry.Action.DELETED, obj, before, {})
                return redirect(list_url)
            except (ProtectedError, RestrictedError) as e:
                blocking = e.protected_objects if isinstance(e, ProtectedError) else e.restricted_objects
                users = sorted({str(type(o)._meta.verbose_name_plural) for o in blocking})
                error = f"Can't delete: {', '.join(users) or 'other records'} still refer to it."
            except ValidationError as e:
                error = " ".join(e.messages)
        form = k.form(instance=obj, workspace=workspace)
    elif request.method == "POST":
        # Taken before validation, which writes the submitted values onto the instance.
        previous = type(obj).objects.get(pk=obj.pk) if obj is not None else None
        before = _snapshot(previous) if previous is not None else {}
        form = k.form(request.POST, instance=obj, workspace=workspace)
        if lock:
            error = lock
        elif form.is_valid():
            try:
                with transaction.atomic():
                    saved, note = k.save(form) if k.save else (form.save(), "")
                    if k.check:
                        k.check(saved)
                        if previous is not None:
                            k.check(previous)  # e.g. the account a transaction was moved away from
                    action = AuditEntry.Action.UPDATED if obj is not None else AuditEntry.Action.CREATED
                    _audit(workspace, request, k, action, saved, before, _snapshot(saved))
            except ValidationError as e:
                form.add_error(None, e)
            else:
                if note or request.POST.get("then") == "another":
                    target = pages.page_url(request, framed, "records:edit", k.slug, saved.pk) if note else pages.page_url(request, framed, "records:new", k.slug)
                    if note:
                        return render(request, "records/saved.html", {"workspace": workspace, "kind": k, "obj": saved, "note": note,
                                                                      "list_url": list_url, "view_url": target, **_frame_ctx(request, framed)})
                    return redirect(target)
                return redirect(list_url)
    elif request.method == "GET":
        initial = {key: value for key, value in request.GET.items() if key in k.form.base_fields}  # prefill from links
        form = k.form(instance=obj, workspace=workspace, initial=initial)
    else:
        return HttpResponseNotAllowed(["GET", "POST"])

    if lock:
        for field in form.fields.values():
            field.disabled = True
    history = AuditEntry.objects.filter(workspace=workspace, kind=k.slug, object_id=str(obj.pk))[:20] if obj is not None else []
    return render(request, "records/edit.html", {
        "workspace": workspace, "kind": k, "obj": obj, "form": form, "lock": lock, "error": error, "history": history,
        "action": pages.page_url(request, framed, "records:edit", k.slug, obj.pk) if obj is not None else pages.page_url(request, framed, "records:new", k.slug),
        "list_url": list_url, "can_delete": obj is not None and not k.locked(obj) and not k.undeletable(obj),
        "delete_blocked": k.undeletable(obj) if obj is not None and not k.locked(obj) else "", **_frame_ctx(request, framed),
    })


frame_home = pages.framed(home_page)
standalone_home = pages.standalone(home_page)
frame_list = pages.framed(list_page)
standalone_list = pages.standalone(list_page)
frame_edit = pages.framed(edit_page)
standalone_edit = pages.standalone(edit_page)
frame_new = pages.framed(edit_page)
standalone_new = pages.standalone(edit_page)
