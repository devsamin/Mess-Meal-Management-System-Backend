from decimal import Decimal

from rest_framework import serializers

from apps.core.serializers import MonthRecordSerializerMixin
from apps.meals.models import MealRecord, MealType, WeeklyMenu
from apps.members.models import Member
from apps.months.models import MonthlyAccount

ALLOWED_STEP = Decimal("0.5")


def validate_quantity(value: Decimal) -> Decimal:
    if value < 0 or value > 10:
        raise serializers.ValidationError("Meal count must be between 0 and 10.")
    if value % ALLOWED_STEP != 0:
        raise serializers.ValidationError("Meal count must be a multiple of 0.5 (0, 0.5, 1, 1.5, 2 …).")
    return value


class MealRecordSerializer(MonthRecordSerializerMixin, serializers.ModelSerializer):
    member_name = serializers.CharField(source="member.name", read_only=True)

    class Meta:
        model = MealRecord
        fields = ["id", "account", "member", "member_name", "date", "meal_type", "quantity",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]
        validators = []  # uniqueness is handled as an upsert by the bulk endpoint; see validate()

    def validate_quantity(self, value):
        return validate_quantity(value)

    def validate(self, attrs):
        attrs = self.validate_month_record(attrs)
        member = attrs.get("member", getattr(self.instance, "member", None))
        day = attrs.get("date", getattr(self.instance, "date", None))
        meal_type = attrs.get("meal_type", getattr(self.instance, "meal_type", None))
        dupes = MealRecord.objects.filter(member=member, date=day, meal_type=meal_type)
        if self.instance:
            dupes = dupes.exclude(pk=self.instance.pk)
        if dupes.exists():
            raise serializers.ValidationError("A meal entry for this member, date and meal already exists.")
        return attrs


class BulkMealEntrySerializer(serializers.Serializer):
    member = serializers.PrimaryKeyRelatedField(queryset=Member.objects.all())
    meal_type = serializers.ChoiceField(choices=MealType.choices)
    quantity = serializers.DecimalField(max_digits=4, decimal_places=1)
    date = serializers.DateField(required=False)

    def validate_quantity(self, value):
        return validate_quantity(value)


class BulkMealSerializer(serializers.Serializer):
    account = serializers.PrimaryKeyRelatedField(queryset=MonthlyAccount.objects.all())
    date = serializers.DateField(required=False)
    entries = BulkMealEntrySerializer(many=True, allow_empty=False)

    def validate(self, attrs):
        for entry in attrs["entries"]:
            entry.setdefault("date", attrs.get("date"))
            if entry["date"] is None:
                raise serializers.ValidationError({"date": "Each entry needs a date."})
        if len(attrs["entries"]) > 3000:
            raise serializers.ValidationError("Too many entries in one request.")
        return attrs


class WeeklyMenuSerializer(serializers.ModelSerializer):
    class Meta:
        model = WeeklyMenu
        fields = ["id", "weekday", "meal_type", "items"]
