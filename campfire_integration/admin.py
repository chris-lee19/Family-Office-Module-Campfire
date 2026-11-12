from django.contrib import admin

from campfire_integration.models import CampfireEvent, Workspace


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ("customer_id", "name", "connected_at", "disconnected_at", "token_expires_at")
    search_fields = ("name", "customer_id")
    exclude = ("access_token", "refresh_token")
    readonly_fields = ("customer_id", "scopes", "install_config", "connected_at", "disconnected_at", "token_expires_at")


@admin.register(CampfireEvent)
class CampfireEventAdmin(admin.ModelAdmin):
    list_display = ("received_at", "customer_id", "topic", "object_id", "processed_at")
    list_filter = ("topic",)
    readonly_fields = [f.name for f in CampfireEvent._meta.fields]
