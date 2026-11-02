from django.db import transaction
from django.db.models import Case, DecimalField, F, Sum, When
from django_filters import rest_framework as filters
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.core.activity import log_activity
from apps.core.models import MessSettings
from apps.core.permissions import IsManager, IsManagerOrReadOnly, MemberVisibilityMixin, is_manager, own_member
from apps.finance.models import Deposit, Expense, ExpenseCategory, ExtraBill
from apps.finance.serializers import (
    DepositSerializer,
    ExpenseCategorySerializer,
    ExpenseSerializer,
    ExtraBillSerializer,
)


class TotalsListMixin:
    """Adds ``total_amount`` of the *filtered* queryset to list responses."""

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        qs = self.filter_queryset(self.get_queryset())
        total = qs.aggregate(t=Sum("amount"))["t"] or 0
        if isinstance(response.data, dict):
            response.data["total_amount"] = f"{total:.2f}"
        return response


class ExpenseCategoryViewSet(viewsets.ModelViewSet):
    queryset = ExpenseCategory.objects.all()
    serializer_class = ExpenseCategorySerializer
    permission_classes = [IsManagerOrReadOnly]
    pagination_class = None
    filterset_fields = ["is_active", "include_in_meal_rate"]


class ExpenseFilter(filters.FilterSet):
    month = filters.NumberFilter(field_name="account_id")
    date_from = filters.DateFilter(field_name="date", lookup_expr="gte")
    date_to = filters.DateFilter(field_name="date", lookup_expr="lte")
    min_amount = filters.NumberFilter(field_name="amount", lookup_expr="gte")
    max_amount = filters.NumberFilter(field_name="amount", lookup_expr="lte")

    class Meta:
        model = Expense
        fields = ["month", "category", "purchaser", "payment_source", "status", "include_in_meal_rate"]


class ExpenseViewSet(MemberVisibilityMixin, TotalsListMixin, viewsets.ModelViewSet):
    serializer_class = ExpenseSerializer
    filterset_class = ExpenseFilter
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    search_fields = ["description", "purchaser__name", "category__name"]
    ordering_fields = ["date", "amount", "created_at"]
    member_lookup = "purchaser"

    def get_queryset(self):
        qs = Expense.objects.select_related("category", "purchaser", "created_by", "approved_by", "account")
        return self.restrict_to_own(qs)

    def perform_create(self, serializer):
        user = self.request.user
        if is_manager(user):
            expense = serializer.save(created_by=user, approved_by=user, status=Expense.Status.APPROVED)
        else:
            member = own_member(user)
            if not MessSettings.load().allow_member_expense_submit or member is None:
                raise PermissionDenied("Expense submission by members is turned off.")
            purchaser = serializer.validated_data.get("purchaser")
            if purchaser is not None and purchaser != member:
                raise PermissionDenied("You can only submit expenses you made yourself.")
            expense = serializer.save(created_by=user, purchaser=member, status=Expense.Status.PENDING)
        log_activity(user, "create", expense,
                     f"{'Added' if expense.status == 'approved' else 'Submitted'} {expense.category.name} "
                     f"expense ৳{expense.amount}" + (f" ({expense.description})" if expense.description else ""))

    def _check_owner_pending(self, expense):
        user = self.request.user
        if is_manager(user):
            return
        if expense.created_by_id != user.pk or expense.status != Expense.Status.PENDING:
            raise PermissionDenied("You can only change your own expenses while they are pending.")

    def perform_update(self, serializer):
        self._check_owner_pending(serializer.instance)
        if not is_manager(self.request.user):
            purchaser = serializer.validated_data.get("purchaser")
            if purchaser is not None and purchaser != own_member(self.request.user):
                raise PermissionDenied("You can only submit expenses you made yourself.")
        expense = serializer.save()
        log_activity(self.request.user, "update", expense, f"Edited {expense.category.name} expense ৳{expense.amount}")

    def perform_destroy(self, instance):
        self._check_owner_pending(instance)
        instance.account.assert_open()
        log_activity(self.request.user, "delete", None,
                     f"Deleted {instance.category.name} expense ৳{instance.amount} ({instance.date})",
                     account=instance.account, entity_type="expense")
        if instance.receipt:
            instance.receipt.delete(save=False)
        instance.delete()

    def _set_status(self, request, status, action_name):
        expense = self.get_object()
        expense.account.assert_open()
        expense.status = status
        expense.approved_by = request.user if status == Expense.Status.APPROVED else None
        expense.save(update_fields=["status", "approved_by", "updated_at"])
        verb = "Approved" if status == Expense.Status.APPROVED else "Rejected"
        log_activity(request.user, action_name, expense,
                     f"{verb} ৳{expense.amount} {expense.category.name} expense"
                     + (f" by {expense.purchaser.name}" if expense.purchaser else ""))
        return Response(self.get_serializer(expense).data)

    @action(detail=True, methods=["post"], permission_classes=[IsManager])
    def approve(self, request, pk=None):
        return self._set_status(request, Expense.Status.APPROVED, "approve")

    @action(detail=True, methods=["post"], permission_classes=[IsManager])
    def reject(self, request, pk=None):
        return self._set_status(request, Expense.Status.REJECTED, "reject")


class DepositFilter(filters.FilterSet):
    month = filters.NumberFilter(field_name="account_id")
    date_from = filters.DateFilter(field_name="date", lookup_expr="gte")
    date_to = filters.DateFilter(field_name="date", lookup_expr="lte")

    class Meta:
        model = Deposit
        fields = ["month", "member", "kind", "method"]


class DepositViewSet(MemberVisibilityMixin, TotalsListMixin, viewsets.ModelViewSet):
    serializer_class = DepositSerializer
    permission_classes = [IsManagerOrReadOnly]
    filterset_class = DepositFilter
    search_fields = ["note", "member__name"]
    ordering_fields = ["date", "amount", "created_at"]

    def get_queryset(self):
        return self.restrict_to_own(Deposit.objects.select_related("member", "received_by", "account"))

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        qs = self.filter_queryset(self.get_queryset())
        signed = Case(
            When(kind=Deposit.Kind.DEPOSIT, then=F("amount")),
            When(kind=Deposit.Kind.REFUND, then=-F("amount")),
            default=0,
            output_field=DecimalField(max_digits=14, decimal_places=2),
        )
        net = qs.aggregate(t=Sum(signed))["t"] or 0
        response.data["total_amount"] = f"{net:.2f}"  # net of refunds
        return response

    def perform_create(self, serializer):
        d = serializer.save(received_by=self.request.user)
        log_activity(self.request.user, "create", d,
                     f"{'Deposit' if d.kind == 'deposit' else 'Refund'} ৳{d.amount} — {d.member.name}")

    def perform_update(self, serializer):
        d = serializer.save()
        log_activity(self.request.user, "update", d, f"Corrected {d.kind} of {d.member.name} to ৳{d.amount}")

    def perform_destroy(self, instance):
        instance.account.assert_open()
        log_activity(self.request.user, "delete", None,
                     f"Deleted {instance.kind} ৳{instance.amount} of {instance.member.name} ({instance.date})",
                     account=instance.account, entity_type="deposit")
        instance.delete()


class ExtraBillFilter(filters.FilterSet):
    month = filters.NumberFilter(field_name="account_id")
    member = filters.NumberFilter(field_name="shares__member")

    class Meta:
        model = ExtraBill
        fields = ["month", "payment_source", "paid_by", "include_in_meal_rate", "member"]


class ExtraBillViewSet(MemberVisibilityMixin, TotalsListMixin, viewsets.ModelViewSet):
    serializer_class = ExtraBillSerializer
    permission_classes = [IsManagerOrReadOnly]
    filterset_class = ExtraBillFilter
    search_fields = ["title", "bill_ref", "description"]
    ordering_fields = ["date", "amount"]
    member_lookup = "shares__member"

    def get_queryset(self):
        qs = ExtraBill.objects.select_related("paid_by", "account").prefetch_related("shares__member")
        return self.restrict_to_own(qs).distinct()

    def perform_create(self, serializer):
        bill = serializer.save(created_by=self.request.user)
        log_activity(self.request.user, "create", bill, f"Added extra bill “{bill.title}” ৳{bill.amount}")

    def perform_update(self, serializer):
        bill = serializer.save()
        log_activity(self.request.user, "update", bill, f"Edited extra bill “{bill.title}” ৳{bill.amount}")

    @transaction.atomic
    def perform_destroy(self, instance):
        instance.account.assert_open()
        log_activity(self.request.user, "delete", None, f"Deleted extra bill “{instance.title}” ৳{instance.amount}",
                     account=instance.account, entity_type="extrabill")
        instance.delete()

