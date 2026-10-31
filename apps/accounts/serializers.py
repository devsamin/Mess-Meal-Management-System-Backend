from django.contrib.auth import password_validation
from rest_framework import serializers

from apps.accounts.models import User
from apps.core.models import MessSettings


class MemberBriefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class MeSerializer(serializers.ModelSerializer):
    is_manager = serializers.BooleanField(read_only=True)
    member = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "email", "role", "is_manager",
                  "preferred_language", "member", "permissions"]
        read_only_fields = ["id", "username", "role", "is_manager", "member", "permissions"]

    def get_member(self, user) -> dict | None:
        member = getattr(user, "member", None)
        return {"id": member.pk, "name": member.name} if member else None

    def get_permissions(self, user) -> dict:
        s = MessSettings.load()
        manager = user.is_manager
        has_member = getattr(user, "member", None) is not None
        return {
            "manage": manager,
            "enter_own_meals": manager or (s.allow_member_meal_entry and has_member),
            "submit_expenses": manager or (s.allow_member_expense_submit and has_member),
            "view_all": manager or s.members_can_view_all,
            "meal_edit_days": None if manager else s.member_meal_edit_days,
        }


class UserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False, min_length=6)
    member_id = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "email", "role", "is_active",
                  "last_login", "date_joined", "password", "member_id"]
        read_only_fields = ["id", "last_login", "date_joined", "member_id"]

    def get_member_id(self, user) -> int | None:
        member = getattr(user, "member", None)
        return member.pk if member else None

    def validate_password(self, value):
        password_validation.validate_password(value)
        return value

    def validate(self, attrs):
        if self.instance is None and not attrs.get("password"):
            raise serializers.ValidationError({"password": "Password is required for new users."})
        request = self.context.get("request")
        if self.instance and request and self.instance == request.user:
            if attrs.get("role", self.instance.role) != User.Role.MANAGER or attrs.get("is_active") is False:
                raise serializers.ValidationError("You cannot demote or deactivate yourself.")
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for k, v in validated_data.items():
            setattr(instance, k, v)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField()
    new_password = serializers.CharField(min_length=6)

    def validate_current_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        password_validation.validate_password(value, self.context["request"].user)
        return value
