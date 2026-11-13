import os

from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from entities import views as entity_views

urlpatterns = [
    path("", entity_views.home, name="home"),
    # The deployed commit (Render sets RENDER_GIT_COMMIT), so anyone can tell which version is live.
    path("healthz", lambda request: JsonResponse({"ok": True, "version": os.environ.get("RENDER_GIT_COMMIT", "")[:7]}), name="healthz"),
    path("campfire/", include("campfire_integration.urls")),
    path("fo/", include("entities.urls")),
    path("fo/", include("investments.urls")),
    path("fo/", include("gl.urls")),
    path("fo/", include("records.urls")),
    path("admin/", admin.site.urls),
]
