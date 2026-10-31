from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from apps.core.models import TimeStampedModel


class MealType(models.TextChoices):
    LUNCH = "lunch", "Lunch"
    DINNER = "dinner", "Dinner"


class MealRecord(TimeStampedModel):
    account = models.ForeignKey("months.MonthlyAccount", on_delete=models.CASCADE, related_name="meals")
    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="meals")
    date = models.DateField()
    meal_type = models.CharField(max_length=10, choices=MealType.choices)
    quantity = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("10"))],
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["date", "member__name", "meal_type"]
        constraints = [
            models.UniqueConstraint(fields=["member", "date", "meal_type"], name="unique_meal_per_slot"),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=0) & models.Q(quantity__lte=10), name="meal_quantity_range"
            ),
        ]
        indexes = [models.Index(fields=["account", "date"]), models.Index(fields=["account", "member"])]

    def __str__(self):
        return f"{self.member} {self.date} {self.meal_type}: {self.quantity}"


class WeeklyMenu(models.Model):
    """What is usually cooked on each weekday (0 = Monday … 6 = Sunday)."""

    weekday = models.PositiveSmallIntegerField(validators=[MaxValueValidator(6)])
    meal_type = models.CharField(max_length=10, choices=MealType.choices)
    items = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["weekday", "meal_type"]
        constraints = [models.UniqueConstraint(fields=["weekday", "meal_type"], name="unique_menu_slot")]

    def __str__(self):
        return f"{self.get_weekday_display()} {self.meal_type}: {self.items}"

    def get_weekday_display(self):
        return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][self.weekday]
