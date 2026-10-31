from rest_framework import serializers

from apps.members.models import Member
from apps.months.models import MonthlyAccount, MonthMember


class MonthlyAccountSerializer(serializers.ModelSerializer):
    label = serializers.CharField(read_only=True)
    start_date = serializers.DateField(read_only=True)
    end_date = serializers.DateField(read_only=True)
    member_count = serializers.IntegerField(read_only=True)
    closed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = MonthlyAccount
        fields = ["id", "year", "month", "label", "status", "fixed_cost_split", "notes", "start_date",
                  "end_date", "member_count", "closed_at", "closed_by_name", "created_at"]
        read_only_fields = ["id", "year", "month", "status", "closed_at", "created_at"]

    def get_closed_by_name(self, obj) -> str | None:
        return (obj.closed_by.get_full_name() or obj.closed_by.username) if obj.closed_by else None

    def validate(self, attrs):
        if self.instance and self.instance.is_closed and "fixed_cost_split" in attrs \
                and attrs["fixed_cost_split"] != self.instance.fixed_cost_split:
            raise serializers.ValidationError("Reopen the month before changing its settings.")
        return attrs


class StartMonthSerializer(serializers.Serializer):
    year = serializers.IntegerField(min_value=2000, max_value=2100)
    month = serializers.IntegerField(min_value=1, max_value=12)
    copy_from = serializers.PrimaryKeyRelatedField(queryset=MonthlyAccount.objects.all(), required=False,
                                                   allow_null=True)
    carry_forward = serializers.BooleanField(default=True)
    member_ids = serializers.ListField(child=serializers.IntegerField(), required=False, allow_null=True)
    fixed_cost_split = serializers.ChoiceField(choices=MonthlyAccount.FixedCostSplit.choices, required=False)

    def validate(self, attrs):
        if MonthlyAccount.objects.filter(year=attrs["year"], month=attrs["month"]).exists():
            raise serializers.ValidationError({"month": "An account for this month already exists."})
        return attrs


class MonthMemberSerializer(serializers.ModelSerializer):
    member_name = serializers.CharField(source="member.name", read_only=True)
    member_status = serializers.CharField(source="member.status", read_only=True)
    member = serializers.PrimaryKeyRelatedField(queryset=Member.objects.all())

    class Meta:
        model = MonthMember
        fields = ["id", "account", "member", "member_name", "member_status", "joined_on", "left_on",
                  "opening_balance", "closing_balance"]
        read_only_fields = ["id", "account", "closing_balance"]
        extra_kwargs = {"joined_on": {"required": False}}

    def validate(self, attrs):
        account = self.context["account"] if self.instance is None else self.instance.account
        if account.is_closed:
            raise serializers.ValidationError(f"{account.label} is closed. Reopen it to make changes.")
        if self.instance is None:
            if MonthMember.objects.filter(account=account, member=attrs["member"]).exists():
                raise serializers.ValidationError({"member": "Already a member of this month."})
            attrs.setdefault("joined_on", account.start_date)
        elif "member" in attrs and attrs["member"] != self.instance.member:
            raise serializers.ValidationError({"member": "Cannot change the member of an enrolment."})
        joined = attrs.get("joined_on", getattr(self.instance, "joined_on", None))
        left = attrs.get("left_on", getattr(self.instance, "left_on", None))
        if joined and not account.contains(joined):
            raise serializers.ValidationError({"joined_on": f"Must be within {account.label}."})
        if left and not account.contains(left):
            raise serializers.ValidationError({"left_on": f"Must be within {account.label}."})
        if joined and left and left < joined:
            raise serializers.ValidationError({"left_on": "Leave date cannot be before join date."})
        return attrs
