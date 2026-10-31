from django.contrib import admin

from apps.meals.models import MealRecord, WeeklyMenu


@admin.register(MealRecord)
class MealRecordAdmin(admin.ModelAdmin):
    list_display = ["date", "member", "meal_type", "quantity"]
    list_filter = ["account", "meal_type"]


admin.site.register(WeeklyMenu)
