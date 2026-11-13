from django.urls import path

from investments import import_views, views

app_name = "investments"

urlpatterns = [
    path("frame/investments", views.frame_investments, name="frame_investments"),
    path("investments", views.standalone_investments, name="investments"),
    path("frame/demo", views.frame_load_demo, name="frame_load_demo"),
    path("demo", views.standalone_load_demo, name="load_demo"),
    path("frame/imports", import_views.frame_imports, name="frame_imports"),
    path("imports", import_views.standalone_imports, name="imports"),
    path("frame/imports/<int:batch_id>", import_views.frame_import_detail, name="frame_import_detail"),
    path("imports/<int:batch_id>", import_views.standalone_import_detail, name="import_detail"),
    path("frame/imports/sample/<str:kind>", import_views.frame_import_sample, name="frame_import_sample"),
    path("imports/sample/<str:kind>", import_views.standalone_import_sample, name="import_sample"),
]
