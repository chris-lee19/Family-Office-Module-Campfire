from django.contrib import admin

from investments.models import CustodianFormat, ImportBatch, ImportIssue


@admin.register(CustodianFormat)
class CustodianFormatAdmin(admin.ModelAdmin):
    """Where an operator sets up a workspace's own file layout (a preset plus overrides)."""

    list_display = ("name", "code", "preset", "workspace", "is_active")
    list_filter = ("preset", "is_active")
    search_fields = ("name", "code")


class ImportIssueInline(admin.TabularInline):
    model = ImportIssue
    extra = 0
    can_delete = False
    readonly_fields = ("row_number", "severity", "code", "message")


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    """Read-only audit view; batches are created and committed through the importer, not edited here."""

    list_display = ("created_at", "workspace", "family", "source", "file_name", "file_format", "status", "rows_new", "rows_duplicate")
    list_filter = ("status", "source", "kind")
    search_fields = ("file_name", "reference", "file_sha256")
    exclude = ("content",)
    inlines = [ImportIssueInline]

    def get_readonly_fields(self, request, obj=None):  # type: ignore[no-untyped-def]
        return [f.name for f in ImportBatch._meta.fields if f.name != "content"]

    def has_add_permission(self, request) -> bool:  # type: ignore[no-untyped-def]
        return False
