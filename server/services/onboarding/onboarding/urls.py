from django.urls import path

from . import views

urlpatterns = [
    path("api/onboarding/applications", views.create_application),
    path("api/onboarding/applications/<uuid:application_id>", views.get_application),
    path("api/onboarding/applications/<uuid:application_id>/submit", views.submit_application),
    path("api/onboarding/applications/<uuid:application_id>/decision", views.ops_decision),
]
