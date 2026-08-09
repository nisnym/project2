from django.urls import path

from . import views

urlpatterns = [
    path("api/auth/register", views.register),
    path("api/auth/login", views.login),
    path("api/auth/refresh", views.refresh),
    path("api/auth/logout", views.logout),
    path("api/auth/me", views.me),
    path(".well-known/jwks.json", views.jwks),
]
