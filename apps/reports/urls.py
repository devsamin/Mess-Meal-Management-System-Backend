from django.urls import path

from apps.reports import views

urlpatterns = [
    path("months/<int:pk>/summary/", views.month_summary, name="month-summary"),
    path("months/<int:pk>/report/", views.month_report, name="month-report"),
    path("months/<int:pk>/dashboard/", views.month_dashboard, name="month-dashboard"),
    path("months/<int:pk>/export/", views.month_export, name="month-export"),
]
