from django.urls import path

from investments import views

app_name = "investments"

urlpatterns = [
    path("frame/investments", views.frame_investments, name="frame_investments"),
    path("investments", views.standalone_investments, name="investments"),
]
