from django.urls import path

from . import views

urlpatterns = [
    path("internal/holds", views.place_hold),
    path("internal/holds/<uuid:hold_id>/capture", views.capture_hold),
    path("internal/holds/<uuid:hold_id>/release", views.release_hold),
    path("internal/journal-entries", views.post_journal_entry),
    path("internal/journal-entries/<uuid:entry_id>/reverse", views.reverse_journal_entry),
    path("internal/balances/<uuid:account_ref>", views.get_balance),
    path("internal/trial-balance", views.trial_balance),
]
