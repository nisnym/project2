from django.urls import path

from . import views

urlpatterns = [
    # money movement
    path("api/transfers", views.create_transfer),
    path("api/funding", views.create_funding),
    path("api/transactions/<uuid:txn_id>/cancel", views.cancel_transaction),
    # history
    path("api/transactions", views.list_transactions),
    path("api/transactions/summary", views.transaction_summary),
    path("api/transactions/<uuid:txn_id>", views.transaction_detail),
    # scheduled and recurring
    path("api/schedules", views.schedules),
    path("api/schedules/<uuid:schedule_id>", views.schedule_detail),
    # funding sources
    path("api/funding-sources", views.funding_sources),
    # staff
    path("api/staff/transactions/<str:txn_id>", views.staff_transaction_detail),
]
