from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.core.views import ActivityLogViewSet, MessSettingsView

router = SimpleRouter()
router.register("activity", ActivityLogViewSet, basename="activity")

urlpatterns = [
    path("settings/", MessSettingsView.as_view(), name="mess-settings"),
    path("", include(router.urls)),
]
