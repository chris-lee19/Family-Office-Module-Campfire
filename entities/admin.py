from django.contrib import admin

from entities.models import Branch, Entity, EntityRole, Family, OwnershipInterest, Person


class OwnershipInline(admin.TabularInline):
    model = OwnershipInterest
    fk_name = "owned"
    extra = 0


class RoleInline(admin.TabularInline):
    model = EntityRole
    extra = 0


@admin.register(Family)
class FamilyAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "workspace", "is_active")


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("name", "family", "parent")
    list_filter = ("family",)


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ("display_name", "family", "branch", "generation", "is_principal")
    list_filter = ("family", "generation")


@admin.register(Entity)
class EntityAdmin(admin.ModelAdmin):
    list_display = ("name", "family", "entity_type", "tax_classification", "status", "campfire_entity_id")
    list_filter = ("family", "entity_type", "status")
    search_fields = ("name", "short_name")
    inlines = [OwnershipInline, RoleInline]
