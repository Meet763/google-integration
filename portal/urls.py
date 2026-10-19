from django.urls import path

from portal import views

urlpatterns = [
    path("", views.home, name="home"),
    path("oauth/start/", views.oauth_start, name="oauth_start"),
    path("oauth/callback/", views.oauth_callback, name="oauth_callback"),
    path("google/disable/", views.disable_google, name="disable_google"),
    path("disconnect/", views.disconnect, name="disconnect"),
    path("logout/", views.logout_view, name="logout"),
    path("schedule/", views.schedule, name="schedule"),
    path("events/new/", views.event_new, name="event_new"),
    path("meet/new/", views.meet_new, name="meet_new"),
    path("docs/new/", views.google_doc_new, name="google_doc_new"),
    path("sheets/new/", views.google_sheet_new, name="google_sheet_new"),
    path("tasks/", views.google_tasks, name="google_tasks"),
    path("forms/new/", views.google_form_new, name="google_form_new"),
    path("forms/lookup/", views.google_form_lookup, name="google_form_lookup"),
]
