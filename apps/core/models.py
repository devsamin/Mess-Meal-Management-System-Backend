from django.conf import settings
from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class MessSettings(models.Model):
    """Single-row table holding mess-wide configuration."""

    name = models.CharField(max_length=120, default="Our Mess")
    members_can_view_all = models.BooleanField(
        default=True, help_text="If off, regular members only see their own records."
    )
    allow_member_meal_entry = models.BooleanField(default=True)
    allow_member_expense_submit = models.BooleanField(default=True)
    member_meal_edit_days = models.PositiveSmallIntegerField(
        default=0, help_text="How many past days a member may edit their own meals (0 = today only)."
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "mess settings"
        verbose_name_plural = "mess settings"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "MessSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    def for_request(cls, request) -> "MessSettings":
        """``load()`` memoised on the request, so one request reads the row once."""
        cached = getattr(request, "_mess_settings", None)
        if cached is None:
            cached = request._mess_settings = cls.load()
        return cached


class ActivityLog(models.Model):
    class Action(models.TextChoices):
        CREATE = "create", "Created"
        UPDATE = "update", "Updated"
        DELETE = "delete", "Deleted"
        APPROVE = "approve", "Approved"
        REJECT = "reject", "Rejected"
        CLOSE = "close", "Closed"
        REOPEN = "reopen", "Reopened"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    account = models.ForeignKey(
        "months.MonthlyAccount", null=True, blank=True, on_delete=models.CASCADE, related_name="activities"
    )
    action = models.CharField(max_length=16, choices=Action.choices)
    entity_type = models.CharField(max_length=40)
    entity_id = models.PositiveBigIntegerField(null=True, blank=True)
    summary = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["account", "-created_at"])]

    def __str__(self):
        return self.summary
