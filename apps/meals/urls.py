from rest_framework.routers import SimpleRouter

from apps.meals.views import MealRecordViewSet, WeeklyMenuViewSet

router = SimpleRouter()
router.register("meals", MealRecordViewSet, basename="meal")
router.register("menu", WeeklyMenuViewSet, basename="menu")

urlpatterns = router.urls
