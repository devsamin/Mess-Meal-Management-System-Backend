from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        MANAGER = "manager", "Manager / Admin"
        MEMBER = "member", "Member"

    role = models.CharField(max_length=16, choices=Role.choices, default=Role.MEMBER)
    preferred_language = models.CharField(max_length=5, default="en")

    @property
    def is_manager(self) -> bool:
        return self.is_superuser or self.role == self.Role.MANAGER

    @property
    def member_profile(self):
        return getattr(self, "member", None)
