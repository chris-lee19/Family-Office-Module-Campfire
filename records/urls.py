from django.urls import path

from records import views

app_name = "records"

urlpatterns = [
    path("frame/records", views.frame_home, name="frame_home"),
    path("records", views.standalone_home, name="home"),
    path("frame/records/<slug:kind>", views.frame_list, name="frame_list"),
    path("records/<slug:kind>", views.standalone_list, name="list"),
    path("frame/records/<slug:kind>/new", views.frame_new, name="frame_new"),
    path("records/<slug:kind>/new", views.standalone_new, name="new"),
    path("frame/records/<slug:kind>/<int:pk>", views.frame_edit, name="frame_edit"),
    path("records/<slug:kind>/<int:pk>", views.standalone_edit, name="edit"),
]
