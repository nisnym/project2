from django.urls import path

from . import views

urlpatterns = [
    path("api/notifications", views.list_notifications),
    path("api/notifications/<uuid:notification_id>/read", views.mark_read),
]
