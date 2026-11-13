from django.contrib import admin

from gl.models import AccountMapping, DimensionTag, GLSettings, JournalPosting, PostedTransaction


@admin.register(GLSettings)
class GLSettingsAdmin(admin.ModelAdmin):
    list_display = ("workspace", "post_from")


@admin.register(AccountMapping)
class AccountMappingAdmin(admin.ModelAdmin):
    """Narrower mappings (per Campfire entity, asset class or FO account) are set here; the page sets the defaults."""

    list_display = ("role", "campfire_account_label", "campfire_entity_id", "asset_class", "account", "workspace")
    list_filter = ("role",)


@admin.register(DimensionTag)
class DimensionTagAdmin(admin.ModelAdmin):
    list_display = ("dimension", "label", "key", "campfire_tag_id", "workspace")
    list_filter = ("dimension",)


class PostedTransactionInline(admin.TabularInline):
    model = PostedTransaction
    extra = 0
    can_delete = False
    readonly_fields = ("transaction",)


@admin.register(JournalPosting)
class JournalPostingAdmin(admin.ModelAdmin):
    """Read-only audit view: postings change only through the bridge (send, events, release)."""

    list_display = ("ref_number", "je_date", "entity_label", "total", "status", "campfire_journal_id", "created_at")
    list_filter = ("status",)
    inlines = [PostedTransactionInline]

    def get_readonly_fields(self, request, obj=None):  # type: ignore[no-untyped-def]
        return [f.name for f in JournalPosting._meta.fields]

    def has_add_permission(self, request) -> bool:  # type: ignore[no-untyped-def]
        return False
