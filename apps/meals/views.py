from collections import defaultdict
from datetime import date as date_cls

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_filters import rest_framework as filters
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.activity import log_activity
from apps.core.cache import cache, cached_response, month_tag
from apps.core.models import MessSettings
from apps.core.money import ZERO
from apps.core.permissions import IsManager, IsManagerOrReadOnly, MemberVisibilityMixin, is_manager
from apps.meals.models import MealRecord, MealType, WeeklyMenu
from apps.meals.permissions import meal_edit_denial
from apps.meals.serializers import BulkMealSerializer, MealRecordSerializer, WeeklyMenuSerializer
from apps.months.models import MonthlyAccount, MonthMember
from apps.reports.services import _num, member_day_grid

MEAL_TYPES = [m.value for m in MealType]


class MealFilter(filters.FilterSet):
    month = filters.NumberFilter(field_name="account_id")
    date_from = filters.DateFilter(field_name="date", lookup_expr="gte")
    date_to = filters.DateFilter(field_name="date", lookup_expr="lte")

    class Meta:
        model = MealRecord
        fields = ["month", "member", "date", "meal_type"]


def _edit_settings(request) -> MessSettings | None:
    """Settings for meal-edit checks; managers are never restricted by them, so skip the read."""
    return None if is_manager(request.user) else MessSettings.for_request(request)


def _account_from_query(request) -> MonthlyAccount:
    month_id = request.query_params.get("month")
    if not month_id:
        raise ValidationError({"month": "Query parameter 'month' is required."})
    return get_object_or_404(MonthlyAccount, pk=month_id)


class MealRecordViewSet(MemberVisibilityMixin, viewsets.ModelViewSet):
    serializer_class = MealRecordSerializer
    filterset_class = MealFilter
    pagination_class = None

    def get_queryset(self):
        return self.restrict_to_own(MealRecord.objects.select_related("member", "account"))

    def _check(self, member, day, account):
        reason = meal_edit_denial(self.request.user, member, day, account, _edit_settings(self.request))
        if reason:
            raise PermissionDenied(reason)

    def perform_create(self, serializer):
        v = serializer.validated_data
        self._check(v["member"], v["date"], v["account"])
        serializer.save(created_by=self.request.user, updated_by=self.request.user)

    def perform_update(self, serializer):
        inst = serializer.instance
        self._check(inst.member, inst.date, inst.account)
        v = serializer.validated_data
        self._check(v.get("member", inst.member), v.get("date", inst.date), v.get("account", inst.account))
        serializer.save(updated_by=self.request.user)

    def perform_destroy(self, instance):
        self._check(instance.member, instance.date, instance.account)
        instance.delete()

    @extend_schema(
        parameters=[OpenApiParameter("month", int, required=True), OpenApiParameter("date", str, required=True)]
    )
    @action(detail=False, methods=["get"])
    def day(self, request):
        """Everything needed for the daily entry screen: members × lunch/dinner."""
        account = _account_from_query(request)
        try:
            day = date_cls.fromisoformat(request.query_params.get("date") or timezone.localdate().isoformat())
        except ValueError:
            raise ValidationError({"date": "Use YYYY-MM-DD."})
        if not account.contains(day):
            raise ValidationError({"date": f"Date must be within {account.label}."})

        memberships = self.restrict_to_own(
            MonthMember.objects.filter(account=account).select_related("member").order_by("member__name")
        )
        settings = _edit_settings(request)
        cells = defaultdict(dict)
        for r in MealRecord.objects.filter(account=account, date=day):
            cells[r.member_id][r.meal_type] = r.quantity

        totals = {t: ZERO for t in MEAL_TYPES}
        rows = []
        for mm in memberships:
            meals = {t: cells[mm.member_id].get(t, ZERO) for t in MEAL_TYPES}
            for t in MEAL_TYPES:
                totals[t] += meals[t]
            rows.append({
                "member_id": mm.member_id,
                "name": mm.member.name,
                "present": mm.is_present_on(day),
                "editable": meal_edit_denial(request.user, mm.member, day, account, settings) is None,
                "meals": {t: _num(v) for t, v in meals.items()},
                "total": _num(sum(meals.values(), ZERO)),
            })
        menu = {m.meal_type: m.items for m in WeeklyMenu.objects.filter(weekday=day.weekday())}
        return Response({
            "account": account.pk,
            "date": day.isoformat(),
            "is_closed": account.is_closed,
            "menu": menu,
            "rows": rows,
            "totals": {**{t: _num(v) for t, v in totals.items()}, "total": _num(sum(totals.values(), ZERO))},
        })

    @extend_schema(request=BulkMealSerializer, responses={200: dict})
    @action(detail=False, methods=["post"])
    def bulk(self, request):
        """Create/update/delete many meal cells at once. Quantity 0 removes the entry."""
        serializer = BulkMealSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = serializer.validated_data["account"]
        entries = serializer.validated_data["entries"]

        enrolled = set(MonthMember.objects.filter(account=account).values_list("member_id", flat=True))
        settings = _edit_settings(request)
        errors = []
        for i, e in enumerate(entries):
            if not account.contains(e["date"]):
                errors.append({"index": i, "detail": f"Date must be within {account.label}."})
            elif e["member"].pk not in enrolled:
                errors.append({"index": i, "detail": f"{e['member']} is not a member of {account.label}."})
            else:
                reason = meal_edit_denial(request.user, e["member"], e["date"], account, settings)
                if reason:
                    raise PermissionDenied(reason)
        if errors:
            raise ValidationError({"entries": errors})

        saved, deleted = self._apply_entries(account, entries, request.user)
        dates = sorted({e["date"] for e in entries})
        when = dates[0].isoformat() if len(dates) == 1 else f"{dates[0]} – {dates[-1]}"
        if not is_manager(request.user) or len(entries) > 1:
            log_activity(request.user, "update", None, f"Updated {len(entries)} meal entries for {when}",
                         account=account, entity_type="meal")
        return Response({"saved": saved, "deleted": deleted}, status=status.HTTP_200_OK)

    @staticmethod
    def _apply_entries(account, entries, user) -> tuple[int, int]:
        """Upsert/delete meal cells with a handful of queries instead of two or three per cell.

        Behaves like a sequential ``update_or_create`` per entry (later entries win);
        a quantity of 0 deletes the cell.
        """
        now = timezone.now()
        saved = deleted = 0
        with transaction.atomic():
            existing = {
                (r.member_id, r.date, r.meal_type): r
                for r in MealRecord.objects.filter(
                    member_id__in={e["member"].pk for e in entries},
                    date__in={e["date"] for e in entries},
                    meal_type__in={e["meal_type"] for e in entries},
                )
            }
            to_create: dict[tuple, MealRecord] = {}
            to_update: dict[int, MealRecord] = {}
            to_delete: set[int] = set()
            for e in entries:
                key = (e["member"].pk, e["date"], e["meal_type"])
                rec = existing.get(key)
                if e["quantity"] == 0:
                    if rec is not None and rec.account_id == account.pk:
                        if key in to_create:
                            del to_create[key]
                        else:
                            to_delete.add(rec.pk)
                            to_update.pop(rec.pk, None)
                        del existing[key]
                        deleted += 1
                    continue
                if rec is None:
                    rec = MealRecord(account=account, member=e["member"], date=e["date"], meal_type=e["meal_type"],
                                     created_by=user, updated_by=user)
                    to_create[key] = existing[key] = rec
                else:
                    rec.account = account
                    rec.updated_by = user
                    rec.updated_at = now
                    if key not in to_create:
                        to_update[rec.pk] = rec
                rec.quantity = e["quantity"]
                saved += 1
            if to_delete:
                MealRecord.objects.filter(pk__in=to_delete).delete()
            if to_update:
                MealRecord.objects.bulk_update(to_update.values(), ["account", "quantity", "updated_by", "updated_at"])
            if to_create:
                MealRecord.objects.bulk_create(to_create.values())
            # bulk_create / bulk_update / queryset.delete skip the model signals that normally invalidate.
            cache.invalidate_on_commit(month_tag(account.pk))
        return saved, deleted

    @extend_schema(parameters=[OpenApiParameter("month", int, required=True)])
    @action(detail=False, methods=["get"])
    def grid(self, request):
        """Member × day totals for the whole month (spreadsheet view)."""
        def compute():
            account = _account_from_query(request)
            data = member_day_grid(account)
            visible = set(
                self.restrict_to_own(MonthMember.objects.filter(account=account)).values_list("member_id", flat=True)
            )
            data["rows"] = [r for r in data["rows"] if r["member_id"] in visible]
            return data, account.is_closed

        raw = request.query_params.get("month", "")
        if not raw.isascii() or not raw.isdigit():
            return Response(compute()[0])  # missing/garbage month: let the normal validation answer, never cache
        # int() so "03" and "3" share one cache tag; writes invalidate by the integer id.
        return cached_response(request, "grid", int(raw), compute)


class WeeklyMenuViewSet(viewsets.ModelViewSet):
    queryset = WeeklyMenu.objects.all()
    serializer_class = WeeklyMenuSerializer
    permission_classes = [IsManagerOrReadOnly]
    pagination_class = None

    @extend_schema(request=WeeklyMenuSerializer(many=True), responses=WeeklyMenuSerializer(many=True))
    @action(detail=False, methods=["put"], permission_classes=[IsManager])
    def bulk(self, request):
        """Replace the menu for the given weekday/meal slots in one call."""
        items = request.data if isinstance(request.data, list) else []
        with transaction.atomic():
            for item in items:
                weekday, meal_type = item.get("weekday"), item.get("meal_type")
                if weekday is None or meal_type not in MEAL_TYPES or not 0 <= int(weekday) <= 6:
                    raise ValidationError("Each item needs weekday 0-6 and a valid meal_type.")
                text = (item.get("items") or "").strip()[:255]
                if text:
                    WeeklyMenu.objects.update_or_create(weekday=weekday, meal_type=meal_type,
                                                        defaults={"items": text})
                else:
                    WeeklyMenu.objects.filter(weekday=weekday, meal_type=meal_type).delete()
        return Response(WeeklyMenuSerializer(WeeklyMenu.objects.all(), many=True).data)
