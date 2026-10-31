from django.db.models import Count
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.activity import log_activity
from apps.core.permissions import IsManager, IsManagerOrReadOnly
from apps.months import services
from apps.months.models import MonthlyAccount, MonthMember
from apps.months.serializers import MonthlyAccountSerializer, MonthMemberSerializer, StartMonthSerializer


class MonthlyAccountViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.UpdateModelMixin,
                            mixins.DestroyModelMixin, viewsets.GenericViewSet):
    serializer_class = MonthlyAccountSerializer
    permission_classes = [IsManagerOrReadOnly]
    filterset_fields = ["status", "year"]
    pagination_class = None

    def get_queryset(self):
        return (
            MonthlyAccount.objects.select_related("closed_by")
            .annotate(member_count=Count("memberships"))
            .order_by("-year", "-month")  # Meta.ordering is ignored on aggregated querysets
        )

    @extend_schema(request=StartMonthSerializer, responses=MonthlyAccountSerializer)
    def create(self, request):
        """Start a month. Optionally copy members/settings and carry balances forward."""
        serializer = StartMonthSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        copy_from = data.get("copy_from")
        if copy_from is None and data.get("member_ids") is None:
            copy_from = services.previous_account(data["year"], data["month"])
        account = services.start_month(
            year=data["year"],
            month=data["month"],
            user=request.user,
            copy_from=copy_from,
            carry_forward=data["carry_forward"],
            member_ids=data.get("member_ids"),
            fixed_cost_split=data.get("fixed_cost_split"),
        )
        return Response(self.get_serializer(self.get_queryset().get(pk=account.pk)).data,
                        status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        account = serializer.save()
        log_activity(self.request.user, "update", account, f"Updated settings of {account.label}", account=account)

    def perform_destroy(self, instance):
        if instance.meals.exists() or instance.expenses.exists() or instance.deposits.exists() \
                or instance.extra_bills.exists():
            raise ValidationError("Only an empty month (no meals, expenses, deposits or bills) can be deleted.")
        log_activity(self.request.user, "delete", None, f"Deleted empty month {instance.label}",
                     entity_type="monthlyaccount")
        instance.delete()

    @action(detail=False, methods=["get"])
    def current(self, request):
        """The month containing today (Asia/Dhaka), else the latest month."""
        today = timezone.localdate()
        qs = self.get_queryset()
        account = qs.filter(year=today.year, month=today.month).first() or qs.first()
        if account is None:
            return Response({"detail": "No month has been started yet."}, status=status.HTTP_404_NOT_FOUND)
        return Response(self.get_serializer(account).data)

    @action(detail=True, methods=["post"], permission_classes=[IsManager])
    def close(self, request, pk=None):
        account = services.close_month(self.get_object(), user=request.user)
        return Response(self.get_serializer(self.get_queryset().get(pk=account.pk)).data)

    @action(detail=True, methods=["post"], permission_classes=[IsManager])
    def reopen(self, request, pk=None):
        account = services.reopen_month(self.get_object(), user=request.user)
        return Response(self.get_serializer(self.get_queryset().get(pk=account.pk)).data)

    @extend_schema(request=MonthMemberSerializer, responses=MonthMemberSerializer(many=True))
    @action(detail=True, methods=["get", "post"], url_path="members",
            permission_classes=[IsManagerOrReadOnly])
    def members(self, request, pk=None):
        account = self.get_object()
        if request.method == "POST":
            serializer = MonthMemberSerializer(data=request.data, context={"account": account, "request": request})
            serializer.is_valid(raise_exception=True)
            mm = serializer.save(account=account)
            log_activity(request.user, "create", mm, f"Added {mm.member.name} to {account.label}", account=account)
            return Response(MonthMemberSerializer(mm).data, status=status.HTTP_201_CREATED)
        qs = account.memberships.select_related("member")
        return Response(MonthMemberSerializer(qs, many=True).data)


class MonthMemberViewSet(mixins.RetrieveModelMixin, mixins.UpdateModelMixin, mixins.DestroyModelMixin,
                         viewsets.GenericViewSet):
    queryset = MonthMember.objects.select_related("member", "account")
    serializer_class = MonthMemberSerializer
    permission_classes = [IsAuthenticated, IsManagerOrReadOnly]

    def perform_update(self, serializer):
        mm = serializer.save()
        log_activity(self.request.user, "update", mm, f"Updated {mm.member.name} in {mm.account.label}",
                     account=mm.account)

    def perform_destroy(self, instance):
        account, member = instance.account, instance.member
        if account.is_closed:
            raise ValidationError(f"{account.label} is closed.")
        has_records = (
            account.meals.filter(member=member).exists()
            or account.deposits.filter(member=member).exists()
            or account.expenses.filter(purchaser=member, payment_source="personal").exists()
            or account.extra_bills.filter(shares__member=member).exists()
        )
        if has_records:
            raise ValidationError(
                f"{member.name} already has meals or money recorded in {account.label}. "
                "Set a leave date instead of removing them."
            )
        instance.delete()
        log_activity(self.request.user, "delete", None, f"Removed {member.name} from {account.label}",
                     account=account, entity_type="monthmember")
