from django.contrib import admin

from apps.members.models import Member


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    list_display = ["name", "phone", "status", "joined_on", "user"]
    list_filter = ["status"]
    search_fields = ["name", "phone"]
