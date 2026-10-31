from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from apps.accounts.models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ["username", "first_name", "role", "is_active", "last_login"]
    list_filter = ["role", "is_active"]
    fieldsets = BaseUserAdmin.fieldsets + (("Mess", {"fields": ("role", "preferred_language")}),)
