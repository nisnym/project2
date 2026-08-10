from django.urls import path

from . import admin_views, views

urlpatterns = [
    path("api/auth/register", views.register),
    path("api/auth/login", views.login),
    path("api/auth/refresh", views.refresh),
    path("api/auth/logout", views.logout),
    path("api/auth/me", views.me),
    path("api/auth/change-password", views.change_password),
    # administration
    path("api/admin/users", admin_views.users),
    path("api/admin/users/<uuid:user_id>", admin_views.user_detail),
    path("api/admin/users/<uuid:user_id>/actions", admin_views.user_action),
    path("api/admin/change-requests", admin_views.change_requests),
    path("api/admin/change-requests/<uuid:change_id>/approve", admin_views.approve_change),
    path("api/admin/change-requests/<uuid:change_id>/reject", admin_views.reject_change),
    path("api/admin/change-requests/<uuid:change_id>/withdraw", admin_views.withdraw_change),
    path("api/admin/stats", admin_views.admin_stats),
    path(".well-known/jwks.json", views.jwks),
]
