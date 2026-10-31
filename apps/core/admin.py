from django.contrib import admin

from apps.core.models import ActivityLog, MessSettings

admin.site.register(MessSettings)


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "actor", "action", "entity_type", "summary"]
    list_filter = ["action", "entity_type"]
