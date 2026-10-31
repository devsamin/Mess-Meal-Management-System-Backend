from django.db import transaction
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.core.activity import log_activity
from apps.core.permissions import IsManagerOrReadOnly
from apps.members.models import Member
from apps.members.serializers import MemberSerializer
from apps.months.models import MonthlyAccount, MonthMember


class MemberViewSet(viewsets.ModelViewSet):
    queryset = Member.objects.select_related("user")
    serializer_class = MemberSerializer
    permission_classes = [IsManagerOrReadOnly]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    filterset_fields = ["status"]
    search_fields = ["name", "phone"]
    ordering_fields = ["name", "joined_on", "created_at"]
    pagination_class = None

    @transaction.atomic
    def perform_create(self, serializer):
        member = serializer.save()
        # Optionally enrol the new member in a month straight away (the UI sends the current month).
        account_id = self.request.data.get("add_to_month")
        if account_id:
            account = MonthlyAccount.objects.filter(pk=account_id, status=MonthlyAccount.Status.OPEN).first()
            if account:
                MonthMember.objects.get_or_create(
                    account=account,
                    member=member,
                    defaults={"joined_on": min(max(account.start_date, member.joined_on), account.end_date)},
                )
        log_activity(self.request.user, "create", member, f"Added member {member.name}")

    def perform_update(self, serializer):
        member = serializer.save()
        log_activity(self.request.user, "update", member, f"Updated member {member.name}")

    @transaction.atomic
    def perform_destroy(self, instance):
        name = instance.name
        user = instance.user
        # Empty enrolments in open months go too; any real history (meals, money) still blocks deletion.
        instance.memberships.filter(account__status=MonthlyAccount.Status.OPEN).delete()
        instance.delete()  # PROTECT on meals/expenses/deposits blocks deleting members with history
        if user and not user.is_manager:
            user.delete()
        log_activity(self.request.user, "delete", None, f"Removed member {name}", entity_type="member")

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        member = self.get_object()
        member.status = Member.Status.INACTIVE
        member.left_on = member.left_on or timezone.localdate()
        member.save(update_fields=["status", "left_on", "updated_at"])
        for mm in member.memberships.filter(account__status=MonthlyAccount.Status.OPEN, left_on__isnull=True):
            if mm.account.contains(member.left_on) and member.left_on >= mm.joined_on:
                mm.left_on = member.left_on
                mm.save(update_fields=["left_on"])
        if member.user:
            member.user.is_active = False
            member.user.save(update_fields=["is_active"])
        log_activity(request.user, "update", member, f"Deactivated member {member.name}")
        return Response(self.get_serializer(member).data)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        member = self.get_object()
        member.status = Member.Status.ACTIVE
        member.left_on = None
        member.save(update_fields=["status", "left_on", "updated_at"])
        if member.user:
            member.user.is_active = True
            member.user.save(update_fields=["is_active"])
        log_activity(request.user, "update", member, f"Reactivated member {member.name}")
        return Response(self.get_serializer(member).data)
