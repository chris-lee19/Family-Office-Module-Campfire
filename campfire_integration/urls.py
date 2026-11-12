from django.urls import path

from campfire_integration import views

app_name = "campfire"

urlpatterns = [
    path("connect", views.connect, name="connect"),
    path("callback", views.callback, name="callback"),
    path("manage", views.manage, name="manage"),
    path("logout", views.logout, name="logout"),
    path("events", views.receive_event, name="events"),
]
