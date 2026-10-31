from django.contrib import admin

from apps.finance.models import Deposit, Expense, ExpenseCategory, ExtraBill, ExtraBillShare


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "include_in_meal_rate", "is_active", "sort_order"]


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ["date", "category", "amount", "purchaser", "payment_source", "status"]
    list_filter = ["account", "category", "status", "payment_source"]


@admin.register(Deposit)
class DepositAdmin(admin.ModelAdmin):
    list_display = ["date", "member", "kind", "amount", "method"]
    list_filter = ["account", "kind", "method"]


class ExtraBillShareInline(admin.TabularInline):
    model = ExtraBillShare
    extra = 0


@admin.register(ExtraBill)
class ExtraBillAdmin(admin.ModelAdmin):
    list_display = ["date", "title", "amount", "payment_source", "include_in_meal_rate"]
    inlines = [ExtraBillShareInline]
