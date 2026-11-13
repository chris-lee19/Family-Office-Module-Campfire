from django.urls import path

from entities import views

app_name = "entities"

urlpatterns = [
    path("frame/entities", views.frame_entities, name="frame_entities"),
    path("entities", views.standalone_entities, name="entities"),
]
