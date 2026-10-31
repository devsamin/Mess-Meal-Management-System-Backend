from django.conf import settings
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

from apps.core.models import TimeStampedModel


class Member(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"

    name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20, blank=True)
    photo = models.ImageField(upload_to="members/", null=True, blank=True)
    joined_on = models.DateField(default=timezone.localdate)
    left_on = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="member"
    )
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(Lower("name"), name="unique_member_name_ci"),
        ]

    def __str__(self):
        return self.name

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE
