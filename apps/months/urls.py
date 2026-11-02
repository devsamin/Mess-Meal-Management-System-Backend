from rest_framework.routers import SimpleRouter

from apps.months.views import MonthlyAccountViewSet, MonthMemberViewSet

router = SimpleRouter()
router.register("months", MonthlyAccountViewSet, basename="month")
router.register("month-members", MonthMemberViewSet, basename="month-member")

urlpatterns = router.urls
