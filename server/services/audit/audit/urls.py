from django.urls import path

from . import views

urlpatterns = [
    path("api/audit/logs", views.list_logs),
    path("api/audit/logs/<int:log_id>", views.log_detail),
    path("api/audit/trace/<str:correlation_id>", views.trace),
    path("api/audit/chain", views.chain_status),
    path("api/audit/stats", views.stats),
]
