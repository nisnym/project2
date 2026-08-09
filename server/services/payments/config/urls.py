from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from platform_common.urls import platform_urlpatterns

urlpatterns = [
    *platform_urlpatterns(),
    path("api/schema", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
    path("", include("payments.urls")),
]
