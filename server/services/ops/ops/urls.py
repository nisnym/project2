from django.urls import path

from . import views

urlpatterns = [
    path("api/ops/queues", views.queues),
    path("api/ops/failures", views.failures),
    path("api/ops/failures/<uuid:case_id>/resolve", views.resolve_failure),
    path("api/ops/reports", views.create_report),
    path("api/ops/reports/<uuid:report_id>", views.get_report),
]
