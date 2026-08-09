from django.urls import path

from . import views

urlpatterns = [
    path("internal/kyc/cases", views.create_case),
    path("internal/kyc/cases/<uuid:case_id>", views.case_status),
]
