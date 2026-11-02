from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from apps.core.models import TimeStampedModel

POSITIVE = [MinValueValidator(Decimal("0.01"))]


class PaymentSource(models.TextChoices):
    FUND = "fund", "Common fund"
    PERSONAL = "personal", "Paid personally"


class ExpenseCategory(models.Model):
    name = models.CharField(max_length=60, unique=True)
    code = models.SlugField(max_length=30, unique=True)
    include_in_meal_rate = models.BooleanField(
        default=False, help_text="Default for new expenses in this category."
    )
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "expense categories"

    def __str__(self):
        return self.name


class Expense(TimeStampedModel):
    class Status(models.TextChoices):
        APPROVED = "approved", "Approved"
        PENDING = "pending", "Pending approval"
        REJECTED = "rejected", "Rejected"

    account = models.ForeignKey("months.MonthlyAccount", on_delete=models.CASCADE, related_name="expenses")
    date = models.DateField()
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name="expenses")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=POSITIVE)
    description = models.CharField(max_length=255, blank=True)
    purchaser = models.ForeignKey(
        "members.Member",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="purchases",
        help_text="Who did the shopping / paid the bill.",
    )
    payment_source = models.CharField(max_length=10, choices=PaymentSource.choices, default=PaymentSource.FUND)
    include_in_meal_rate = models.BooleanField(default=True)
    receipt = models.ImageField(upload_to="receipts/%Y/%m/", null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.APPROVED, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="expense_amount_positive"),
            models.CheckConstraint(
                condition=~models.Q(payment_source="personal") | models.Q(purchaser__isnull=False),
                name="personal_expense_needs_purchaser",
            ),
        ]
        indexes = [models.Index(fields=["account", "date"]), models.Index(fields=["account", "status"])]

    def __str__(self):
        return f"{self.date} {self.category} ৳{self.amount}"

    def clean(self):
        if self.payment_source == PaymentSource.PERSONAL and not self.purchaser_id:
            raise ValidationError({"purchaser": "Select who paid personally."})


class Deposit(TimeStampedModel):
    class Kind(models.TextChoices):
        DEPOSIT = "deposit", "Deposit"
        REFUND = "refund", "Refund / payout"

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        BKASH = "bkash", "bKash"
        NAGAD = "nagad", "Nagad"
        ROCKET = "rocket", "Rocket"
        BANK = "bank", "Bank transfer"
        OTHER = "other", "Other"

    account = models.ForeignKey("months.MonthlyAccount", on_delete=models.CASCADE, related_name="deposits")
    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="deposits")
    date = models.DateField()
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=POSITIVE)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.DEPOSIT)
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.CASH)
    note = models.CharField(max_length=255, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [models.CheckConstraint(condition=models.Q(amount__gt=0), name="deposit_amount_positive")]
        indexes = [models.Index(fields=["account", "member"]), models.Index(fields=["account", "date"])]

    def __str__(self):
        return f"{self.member} {self.kind} ৳{self.amount}"

    @property
    def signed_amount(self) -> Decimal:
        return self.amount if self.kind == self.Kind.DEPOSIT else -self.amount


class ExtraBill(TimeStampedModel):
    class SplitMode(models.TextChoices):
        EQUAL = "equal", "Equal split"
        CUSTOM = "custom", "Custom amounts"

    account = models.ForeignKey("months.MonthlyAccount", on_delete=models.CASCADE, related_name="extra_bills")
    date = models.DateField()
    bill_ref = models.CharField("Bill ID", max_length=60, blank=True)
    title = models.CharField(max_length=120)
    description = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=POSITIVE)
    payment_source = models.CharField(max_length=10, choices=PaymentSource.choices, default=PaymentSource.FUND)
    paid_by = models.ForeignKey(
        "members.Member", null=True, blank=True, on_delete=models.PROTECT, related_name="bills_paid"
    )
    include_in_meal_rate = models.BooleanField(default=False)
    split_mode = models.CharField(max_length=10, choices=SplitMode.choices, default=SplitMode.EQUAL)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="bill_amount_positive"),
            models.CheckConstraint(
                condition=~models.Q(payment_source="personal") | models.Q(paid_by__isnull=False),
                name="personal_bill_needs_payer",
            ),
        ]
        indexes = [models.Index(fields=["account", "date"])]

    def __str__(self):
        return f"{self.title} ৳{self.amount}"


class ExtraBillShare(models.Model):
    bill = models.ForeignKey(ExtraBill, on_delete=models.CASCADE, related_name="shares")
    member = models.ForeignKey("members.Member", on_delete=models.PROTECT, related_name="bill_shares")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0"))])

    class Meta:
        ordering = ["member__name"]
        constraints = [
            models.UniqueConstraint(fields=["bill", "member"], name="unique_bill_share"),
            models.CheckConstraint(condition=models.Q(amount__gte=0), name="bill_share_non_negative"),
        ]

    def __str__(self):
        return f"{self.member}: ৳{self.amount}"
