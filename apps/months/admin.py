from django.contrib import admin

from apps.months.models import MonthlyAccount, MonthMember


class MonthMemberInline(admin.TabularInline):
    model = MonthMember
    extra = 0


@admin.register(MonthlyAccount)
class MonthlyAccountAdmin(admin.ModelAdmin):
    list_display = ["__str__", "status", "fixed_cost_split", "closed_at"]
    inlines = [MonthMemberInline]
