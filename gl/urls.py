from django.urls import path

from gl import views

app_name = "gl"

urlpatterns = [
    path("frame/gl", views.frame_gl, name="frame_gl"),
    path("gl", views.standalone_gl, name="gl"),
]
