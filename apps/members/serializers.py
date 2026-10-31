from django.contrib.auth import password_validation
from django.db import transaction
from rest_framework import serializers

from apps.accounts.models import User
from apps.members.models import Member


class MemberSerializer(serializers.ModelSerializer):
    username = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()
    # Optional: create / update a linked login in the same form.
    login_username = serializers.CharField(write_only=True, required=False, allow_blank=True)
    login_password = serializers.CharField(write_only=True, required=False, allow_blank=True)
    login_role = serializers.ChoiceField(choices=User.Role.choices, write_only=True, required=False)
    remove_photo = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = Member
        fields = ["id", "name", "phone", "photo", "joined_on", "left_on", "status", "notes", "user",
                  "username", "role", "created_at", "login_username", "login_password", "login_role",
                  "remove_photo"]
        read_only_fields = ["id", "status", "user", "username", "role", "created_at"]

    def get_username(self, obj) -> str | None:
        return obj.user.username if obj.user else None

    def get_role(self, obj) -> str | None:
        return obj.user.role if obj.user else None

    def validate_name(self, value):
        qs = Member.objects.filter(name__iexact=value.strip())
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A member with this name already exists.")
        return value.strip()

    def validate(self, attrs):
        username = (attrs.get("login_username") or "").strip()
        password = attrs.get("login_password") or ""
        current_user = self.instance.user if self.instance else None
        if username:
            clash = User.objects.filter(username__iexact=username)
            if current_user:
                clash = clash.exclude(pk=current_user.pk)
            if clash.exists():
                raise serializers.ValidationError({"login_username": "This username is taken."})
            if current_user is None and not password:
                raise serializers.ValidationError({"login_password": "Set a password for the new login."})
        if password:
            if not username and current_user is None:
                raise serializers.ValidationError({"login_username": "Enter a username for the login."})
            try:
                password_validation.validate_password(password)
            except Exception as e:  # django ValidationError
                raise serializers.ValidationError({"login_password": list(getattr(e, "messages", [str(e)]))})
        joined, left = attrs.get("joined_on"), attrs.get("left_on")
        if joined and left and left < joined:
            raise serializers.ValidationError({"left_on": "Leave date cannot be before join date."})
        return attrs

    def _sync_login(self, member, username, password, role):
        if not username and not password and not role:
            return
        user = member.user
        if user is None:
            user = User(username=username, first_name=member.name, role=role or User.Role.MEMBER)
        else:
            if username:
                user.username = username
            if role:
                user.role = role
        if password:
            user.set_password(password)
        user.save()
        if member.user_id != user.pk:
            member.user = user
            member.save(update_fields=["user"])

    @transaction.atomic
    def create(self, validated_data):
        username = validated_data.pop("login_username", "").strip()
        password = validated_data.pop("login_password", "")
        role = validated_data.pop("login_role", None)
        validated_data.pop("remove_photo", None)
        member = super().create(validated_data)
        if username:
            self._sync_login(member, username, password, role)
        return member

    @transaction.atomic
    def update(self, instance, validated_data):
        username = validated_data.pop("login_username", "").strip()
        password = validated_data.pop("login_password", "")
        role = validated_data.pop("login_role", None)
        if validated_data.pop("remove_photo", False):
            instance.photo.delete(save=False)
            instance.photo = None
        member = super().update(instance, validated_data)
        if username or password or (role and member.user):
            self._sync_login(member, username, password, role)
        return member
