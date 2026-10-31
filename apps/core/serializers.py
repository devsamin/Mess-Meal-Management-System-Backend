from rest_framework import serializers

from apps.core.models import ActivityLog, MessSettings
from apps.months.models import MonthMember


class MessSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = MessSettings
        fields = ["name", "members_can_view_all", "allow_member_meal_entry",
                  "allow_member_expense_submit", "member_meal_edit_days", "updated_at"]
        read_only_fields = ["updated_at"]


class ActivityLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = ActivityLog
        fields = ["id", "account", "action", "entity_type", "entity_id", "summary", "actor_name", "created_at"]

    def get_actor_name(self, obj) -> str | None:
        if not obj.actor:
            return None
        return obj.actor.get_full_name() or obj.actor.username


class MonthRecordSerializerMixin:
    """Shared validation for anything that belongs to a monthly account.

    * the month must be open
    * the record date must be inside the month
    * referenced members must be enrolled in that month
    """

    date_field = "date"
    member_fields: tuple[str, ...] = ("member",)

    def validate_month_record(self, attrs):
        account = attrs.get("account") or getattr(self.instance, "account", None)
        if account is None:
            raise serializers.ValidationError({"account": "Month is required."})
        if self.instance is not None and self.instance.account.is_closed:
            raise serializers.ValidationError(f"{self.instance.account.label} is closed. Reopen it to make changes.")
        if account.is_closed:
            raise serializers.ValidationError(f"{account.label} is closed. Reopen it to make changes.")
        day = attrs.get(self.date_field) or getattr(self.instance, self.date_field, None)
        if day and not account.contains(day):
            raise serializers.ValidationError({self.date_field: f"Date must be within {account.label}."})
        for field in self.member_fields:
            member = attrs.get(field, getattr(self.instance, field, None) if self.instance else None)
            if member is not None and not MonthMember.objects.filter(account=account, member=member).exists():
                raise serializers.ValidationError({field: f"{member} is not a member of {account.label}."})
        return attrs
