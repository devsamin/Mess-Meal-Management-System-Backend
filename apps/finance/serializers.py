from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from apps.core.money import allocate
from apps.core.permissions import is_manager, own_member
from apps.core.serializers import MonthRecordSerializerMixin
from apps.finance.models import Deposit, Expense, ExpenseCategory, ExtraBill, ExtraBillShare, PaymentSource
from apps.members.models import Member
from apps.months.models import MonthMember


def _user_name(user):
    return (user.get_full_name() or user.username) if user else None


class ExpenseCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ExpenseCategory
        fields = ["id", "name", "code", "include_in_meal_rate", "is_active", "sort_order"]


class ExpenseSerializer(MonthRecordSerializerMixin, serializers.ModelSerializer):
    member_fields = ("purchaser",)

    category_name = serializers.CharField(source="category.name", read_only=True)
    purchaser_name = serializers.CharField(source="purchaser.name", read_only=True, default=None)
    created_by_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()
    include_in_meal_rate = serializers.BooleanField(required=False)
    remove_receipt = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = Expense
        fields = ["id", "account", "date", "category", "category_name", "amount", "description", "purchaser",
                  "purchaser_name", "payment_source", "include_in_meal_rate", "receipt", "remove_receipt",
                  "status", "created_by_name", "approved_by_name", "created_at", "updated_at"]
        read_only_fields = ["id", "status", "created_at", "updated_at"]

    def get_created_by_name(self, obj) -> str | None:
        return _user_name(obj.created_by)

    def get_approved_by_name(self, obj) -> str | None:
        return _user_name(obj.approved_by)

    def validate_receipt(self, value):
        if value and value.size > 5 * 1024 * 1024:
            raise serializers.ValidationError("Receipt image must be smaller than 5 MB.")
        return value

    def validate(self, attrs):
        request = self.context.get("request")
        if request and not is_manager(request.user) and self.instance is None:
            attrs["purchaser"] = own_member(request.user)  # members submit their own expenses only
        attrs = self.validate_month_record(attrs)
        source = attrs.get("payment_source", getattr(self.instance, "payment_source", PaymentSource.FUND))
        purchaser = attrs.get("purchaser", getattr(self.instance, "purchaser", None))
        if source == PaymentSource.PERSONAL and purchaser is None:
            raise serializers.ValidationError({"purchaser": "Select who paid personally."})
        if self.instance is None and "include_in_meal_rate" not in attrs:
            attrs["include_in_meal_rate"] = attrs["category"].include_in_meal_rate
        return attrs

    def update(self, instance, validated_data):
        if validated_data.pop("remove_receipt", False) and instance.receipt:
            instance.receipt.delete(save=False)
            instance.receipt = None
        return super().update(instance, validated_data)

    def create(self, validated_data):
        validated_data.pop("remove_receipt", None)
        return super().create(validated_data)


class DepositSerializer(MonthRecordSerializerMixin, serializers.ModelSerializer):
    member_name = serializers.CharField(source="member.name", read_only=True)
    received_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Deposit
        fields = ["id", "account", "member", "member_name", "date", "amount", "kind", "method", "note",
                  "received_by_name", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_received_by_name(self, obj) -> str | None:
        return _user_name(obj.received_by)

    def validate(self, attrs):
        return self.validate_month_record(attrs)


class ExtraBillShareSerializer(serializers.ModelSerializer):
    member_name = serializers.CharField(source="member.name", read_only=True)

    class Meta:
        model = ExtraBillShare
        fields = ["member", "member_name", "amount"]


class ExtraBillShareInputSerializer(serializers.Serializer):
    member = serializers.PrimaryKeyRelatedField(queryset=Member.objects.all())
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"), required=False)


class ExtraBillSerializer(MonthRecordSerializerMixin, serializers.ModelSerializer):
    member_fields = ("paid_by",)

    shares = ExtraBillShareSerializer(many=True, read_only=True)
    share_inputs = ExtraBillShareInputSerializer(many=True, write_only=True, required=False)
    paid_by_name = serializers.CharField(source="paid_by.name", read_only=True, default=None)
    per_head = serializers.SerializerMethodField()

    class Meta:
        model = ExtraBill
        fields = ["id", "account", "date", "bill_ref", "title", "description", "amount", "payment_source",
                  "paid_by", "paid_by_name", "include_in_meal_rate", "split_mode", "shares", "share_inputs",
                  "per_head", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_per_head(self, obj) -> str | None:
        shares = obj.shares.all()
        if obj.split_mode == ExtraBill.SplitMode.EQUAL and shares:
            return str((obj.amount / len(shares)).quantize(Decimal("0.01")))
        return None

    def validate(self, attrs):
        attrs = self.validate_month_record(attrs)
        account = attrs.get("account") or self.instance.account
        source = attrs.get("payment_source", getattr(self.instance, "payment_source", PaymentSource.FUND))
        if source == PaymentSource.PERSONAL and not attrs.get("paid_by", getattr(self.instance, "paid_by", None)):
            raise serializers.ValidationError({"paid_by": "Select who paid this bill."})

        in_rate = attrs.get("include_in_meal_rate", getattr(self.instance, "include_in_meal_rate", False))
        mode = attrs.get("split_mode", getattr(self.instance, "split_mode", ExtraBill.SplitMode.EQUAL))
        amount = attrs.get("amount", getattr(self.instance, "amount", None))
        inputs = attrs.get("share_inputs")
        if in_rate:
            attrs["_shares"] = []  # shared through the meal rate instead
            return attrs
        if inputs is None and self.instance is not None and not {"amount", "split_mode", "include_in_meal_rate"} & attrs.keys():
            return attrs  # shares untouched
        if inputs is None and self.instance is not None:
            inputs = [{"member": s.member, "amount": s.amount} for s in self.instance.shares.all()]

        enrolled = {mm.member_id: mm.member for mm in MonthMember.objects.filter(account=account)
                    .select_related("member")}
        if not inputs:
            inputs = [{"member": m} for m in enrolled.values()]
            mode = attrs["split_mode"] = ExtraBill.SplitMode.EQUAL
        member_ids = [i["member"].pk for i in inputs]
        if len(set(member_ids)) != len(member_ids):
            raise serializers.ValidationError({"share_inputs": "Each member can appear only once."})
        outsiders = [i["member"].name for i in inputs if i["member"].pk not in enrolled]
        if outsiders:
            raise serializers.ValidationError(
                {"share_inputs": f"Not members of {account.label}: {', '.join(outsiders)}."})

        if mode == ExtraBill.SplitMode.EQUAL:
            split = allocate(amount, {i["member"].pk: Decimal(1) for i in inputs})
            attrs["_shares"] = [(i["member"], split[i["member"].pk]) for i in inputs]
        else:
            if any(i.get("amount") is None for i in inputs):
                raise serializers.ValidationError({"share_inputs": "Enter an amount for every member."})
            total = sum((i["amount"] for i in inputs), Decimal("0"))
            if total != amount:
                raise serializers.ValidationError(
                    {"share_inputs": f"Shares add up to ৳{total}, but the bill is ৳{amount}."})
            attrs["_shares"] = [(i["member"], i["amount"]) for i in inputs]
        return attrs

    def _save_shares(self, bill, shares):
        if shares is None:
            return
        bill.shares.all().delete()
        ExtraBillShare.objects.bulk_create(
            [ExtraBillShare(bill=bill, member=m, amount=a) for m, a in shares]
        )

    @transaction.atomic
    def create(self, validated_data):
        validated_data.pop("share_inputs", None)
        shares = validated_data.pop("_shares", None)
        bill = super().create(validated_data)
        self._save_shares(bill, shares)
        return bill

    @transaction.atomic
    def update(self, instance, validated_data):
        validated_data.pop("share_inputs", None)
        shares = validated_data.pop("_shares", None)
        bill = super().update(instance, validated_data)
        self._save_shares(bill, shares)
        return bill
