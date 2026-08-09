from django.urls import path

from . import views

urlpatterns = [
    # internal
    path("internal/screen", views.screen),
    path("internal/rules/refresh", views.refresh_caches),
    # analyst
    path("api/fraud/cases", views.list_cases),
    path("api/fraud/cases/<uuid:case_id>", views.case_detail),
    path("api/fraud/cases/<uuid:case_id>/approve", views.approve_case),
    path("api/fraud/cases/<uuid:case_id>/reject", views.reject_case),
    path("api/fraud/stats", views.case_stats),
    # administrator
    path("api/fraud/rules", views.list_rules),
    path("api/fraud/rules/dry-run", views.dry_run),
    path("api/fraud/rules/<uuid:rule_id>", views.update_rule),
    path("api/fraud/thresholds", views.thresholds),
]
