from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from apps.accounts.views import ChangePasswordView, LoginView, MeView, UserViewSet
from apps.core.views import ActivityLogViewSet, MessSettingsView
from apps.finance.views import DepositViewSet, ExpenseCategoryViewSet, ExpenseViewSet, ExtraBillViewSet
from apps.meals.views import MealRecordViewSet, WeeklyMenuViewSet
from apps.members.views import MemberViewSet
from apps.months.views import MonthlyAccountViewSet, MonthMemberViewSet
from apps.reports import views as report_views

router = DefaultRouter()
router.register("users", UserViewSet, basename="user")
router.register("members", MemberViewSet, basename="member")
router.register("months", MonthlyAccountViewSet, basename="month")
router.register("month-members", MonthMemberViewSet, basename="month-member")
router.register("meals", MealRecordViewSet, basename="meal")
router.register("menu", WeeklyMenuViewSet, basename="menu")
router.register("expense-categories", ExpenseCategoryViewSet, basename="expense-category")
router.register("expenses", ExpenseViewSet, basename="expense")
router.register("deposits", DepositViewSet, basename="deposit")
router.register("extra-bills", ExtraBillViewSet, basename="extra-bill")
router.register("activity", ActivityLogViewSet, basename="activity")

api_v1 = [
    path("auth/login/", LoginView.as_view(), name="login"),
    path("auth/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("auth/me/", MeView.as_view(), name="me"),
    path("auth/change-password/", ChangePasswordView.as_view(), name="change-password"),
    path("settings/", MessSettingsView.as_view(), name="mess-settings"),
    path("months/<int:pk>/summary/", report_views.month_summary, name="month-summary"),
    path("months/<int:pk>/report/", report_views.month_report, name="month-report"),
    path("months/<int:pk>/dashboard/", report_views.month_dashboard, name="month-dashboard"),
    path("months/<int:pk>/export/", report_views.month_export, name="month-export"),
    path("", include(router.urls)),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include(api_v1)),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="api-docs"),
]

if settings.SERVE_MEDIA:
    urlpatterns += [re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT})]
