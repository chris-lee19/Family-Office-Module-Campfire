from django.urls import path

from records import attention_views
from records import access_views
from records import onboarding_views as ob
from records import review_views
from records import views

app_name = "records"

urlpatterns = [
    path("frame/records", views.frame_home, name="frame_home"),
    path("records", views.standalone_home, name="home"),
    path("frame/home", attention_views.frame_attention, name="frame_attention"),
    path("home", attention_views.standalone_attention, name="attention"),
    path("frame/people", access_views.frame_access, name="frame_access"),
    path("people", access_views.standalone_access, name="access"),
    path("frame/review", review_views.frame_review, name="frame_review"),
    path("review", review_views.standalone_review, name="review"),
    # Onboarding (before the <kind> patterns, which would otherwise match "onboarding").
    path("frame/records/onboarding", ob.frame_onboarding, name="frame_onboarding"),
    path("records/onboarding", ob.standalone_onboarding, name="onboarding"),
    path("frame/records/onboarding/<int:batch_id>", ob.frame_onboarding_batch, name="frame_onboarding_batch"),
    path("records/onboarding/<int:batch_id>", ob.standalone_onboarding_batch, name="onboarding_batch"),
    path("frame/records/onboarding/template.xlsx", ob.frame_template, name="frame_onboarding_template"),
    path("records/onboarding/template.xlsx", ob.standalone_template, name="onboarding_template"),
    path("frame/records/onboarding/export/<int:family_id>.xlsx", ob.frame_export, name="frame_onboarding_export"),
    path("records/onboarding/export/<int:family_id>.xlsx", ob.standalone_export, name="onboarding_export"),
    path("frame/records/<slug:kind>", views.frame_list, name="frame_list"),
    path("records/<slug:kind>", views.standalone_list, name="list"),
    path("frame/records/<slug:kind>/new", views.frame_new, name="frame_new"),
    path("records/<slug:kind>/new", views.standalone_new, name="new"),
    path("frame/records/<slug:kind>/<int:pk>", views.frame_edit, name="frame_edit"),
    path("records/<slug:kind>/<int:pk>", views.standalone_edit, name="edit"),
]
