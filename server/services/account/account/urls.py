from django.urls import path

from . import views

urlpatterns = [
    # internal
    path("internal/validate-transfer", views.validate_transfer),
    path("internal/limits/release", views.release_limit),
    path("internal/accounts", views.create_account),
    # customer
    path("api/accounts", views.list_accounts),
    path("api/accounts/<uuid:account_id>", views.account_detail),
    path("api/beneficiaries", views.beneficiaries),
    path("api/beneficiaries/<uuid:beneficiary_id>", views.beneficiary_detail),
    # admin
    path("api/limit-policies", views.limit_policies),
]
