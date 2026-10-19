from django.urls import include, path

urlpatterns = [
    path("", include("portal.urls")),
]
