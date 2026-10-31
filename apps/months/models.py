import calendar
from datetime import date

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.core.models import TimeStampedModel


class MonthlyAccount(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    class FixedCostSplit(models.TextChoices):
        EQUAL = "equal", "Equally among members"
        BY_DAYS = "by_days", "By days present"

    year = models.PositiveSmallIntegerField(validators=[MinValueValidator(2000), MaxValueValidator(2100)])
    month = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(12)])
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True)
    fixed_cost_split = models.CharField(
        max_length=10,
        choices=FixedCostSplit.choices,
        default=FixedCostSplit.EQUAL,
        help_text="How expenses that are not part of the meal rate are shared.",
    )
    notes = models.TextField(blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    final_report = models.JSONField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["-year", "-month"]
        constraints = [
            models.UniqueConstraint(fields=["year", "month"], name="unique_month_account"),
            models.CheckConstraint(condition=models.Q(month__gte=1, month__lte=12), name="month_range"),
        ]

    def __str__(self):
        return self.label

    @property
    def label(self) -> str:
        return f"{calendar.month_name[self.month]} {self.year}"

    @property
    def start_date(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def end_date(self) -> date:
        return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])

    @property
    def days_in_month(self) -> int:
        return calendar.monthrange(self.year, self.month)[1]

    @property
    def is_closed(self) -> bool:
        return self.status == self.Status.CLOSED

    def contains(self, day: date) -> bool:
        return self.start_date <= day <= self.end_date

    def assert_open(self):
        if self.is_closed:
            raise ValidationError(f"{self.label} is closed. Reopen it before making changes.")

    def assert_contains(self, day: date, field: str = "date"):
        if not self.contains(day):
            raise ValidationError({field: f"Date must be within {self.label}."})


class MonthMember(models.Model):
    """A member's participation in one month (supports joining/leaving mid-month)."""

    account = models.ForeignKey(MonthlyAccount, on_delete=models.CASCADE, related_name="memberships")
    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="memberships")
    joined_on = models.DateField()
    left_on = models.DateField(null=True, blank=True)
    opening_balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, help_text="Balance carried forward from the previous month."
    )
    closing_balance = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, editable=False)

    class Meta:
        ordering = ["member__name"]
        constraints = [
            models.UniqueConstraint(fields=["account", "member"], name="unique_member_per_month"),
            models.CheckConstraint(
                condition=models.Q(left_on__isnull=True) | models.Q(left_on__gte=models.F("joined_on")),
                name="month_member_left_after_join",
            ),
        ]

    def __str__(self):
        return f"{self.member} — {self.account}"

    def clean(self):
        if self.account_id:
            self.account.assert_contains(self.joined_on, "joined_on")
            if self.left_on:
                self.account.assert_contains(self.left_on, "left_on")
        if self.left_on and self.left_on < self.joined_on:
            raise ValidationError({"left_on": "Leave date cannot be before join date."})

    def is_present_on(self, day: date) -> bool:
        return self.joined_on <= day and (self.left_on is None or day <= self.left_on)

    @property
    def days_present(self) -> int:
        end = self.left_on or self.account.end_date
        return max((end - self.joined_on).days + 1, 0)
