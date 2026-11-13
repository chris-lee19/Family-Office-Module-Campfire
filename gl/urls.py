from django.urls import path

from gl import call_views, views

app_name = "gl"

urlpatterns = [
    path("frame/gl", views.frame_gl, name="frame_gl"),
    path("gl", views.standalone_gl, name="gl"),
    path("frame/calls", call_views.frame_calls, name="frame_calls"),
    path("calls", call_views.standalone_calls, name="calls"),
]
