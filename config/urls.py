from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from entities import views as entity_views

urlpatterns = [
    path("", entity_views.home, name="home"),
    path("healthz", lambda request: JsonResponse({"ok": True}), name="healthz"),
    path("campfire/", include("campfire_integration.urls")),
    path("fo/", include("entities.urls")),
    path("admin/", admin.site.urls),
]
