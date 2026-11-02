from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.decorators import api_view
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.cache import cached_response
from apps.core.models import ActivityLog
from apps.core.permissions import can_view_all, own_member
from apps.core.serializers import ActivityLogSerializer
from apps.finance.models import Expense
from apps.finance.serializers import ExpenseSerializer
from apps.months.models import MonthlyAccount
from apps.reports import exports
from apps.reports.services import daily_meal_totals, report_as_dict, summary_as_dict


def _visible(request, data: dict) -> dict:
    """Hide other members' rows from regular members when transparency is off."""
    if can_view_all(request):
        return data
    me = own_member(request.user)
    my_id = me.pk if me else None

    def keep(rows, key="member_id"):
        return [r for r in rows if r.get(key) == my_id]

    if "members" in data:
        data = {**data, "members": keep(data["members"])}
    if "summary" in data:
        data = {**data, "summary": _visible(request, data["summary"])}
    if "meal_grid" in data:
        data["meal_grid"] = {**data["meal_grid"], "rows": keep(data["meal_grid"]["rows"])}
    if "deposits" in data and me:
        data["deposits"] = [d for d in data["deposits"] if d["member"] == me.name]
    return data


@extend_schema(responses=dict)
@api_view(["GET"])
def month_summary(request, pk):
    """Meal rate, totals and every member's balance for the month."""
    def compute():
        account = get_object_or_404(MonthlyAccount, pk=pk)
        return _visible(request, summary_as_dict(account)), account.is_closed

    return cached_response(request, "summary", pk, compute)


@extend_schema(responses=dict)
@api_view(["GET"])
def month_report(request, pk):
    """Full report: summary, daily meals, member × day grid, expenses, deposits, bills."""
    def compute():
        account = get_object_or_404(MonthlyAccount, pk=pk)
        return _visible(request, report_as_dict(account)), account.is_closed

    return cached_response(request, "report", pk, compute)


@extend_schema(responses=dict)
@api_view(["GET"])
def month_dashboard(request, pk):
    today = timezone.localdate()  # part of the cache key: "today" rolls over at midnight
    return cached_response(request, "dashboard", pk, lambda: _dashboard(request, pk, today),
                           params={**request.query_params, "today": today.isoformat()})


def _dashboard(request, pk, today):
    account = get_object_or_404(MonthlyAccount, pk=pk)
    summary = _visible(request, summary_as_dict(account))
    daily = (account.final_report or {}).get("daily_meals") if account.is_closed else None
    daily = daily or daily_meal_totals(account)
    today_row = next((d for d in daily if d["date"] == today.isoformat()), None)

    recent_expenses = Expense.objects.filter(account=account).select_related(
        "category", "purchaser", "created_by", "approved_by")
    activity = ActivityLog.objects.filter(account=account).select_related("actor")
    if not can_view_all(request):
        me = own_member(request.user)
        recent_expenses = recent_expenses.filter(purchaser=me)
        activity = activity.filter(actor=request.user)

    me = own_member(request.user)
    my_line = next((m for m in summary["members"] if me and m["member_id"] == me.pk), None)
    data = {
        "summary": summary,
        "daily_meals": daily,
        "today": today_row,
        "today_date": today.isoformat(),
        "my_balance": my_line,
        "recent_expenses": ExpenseSerializer(recent_expenses.order_by("-date", "-id")[:6], many=True,
                                             context={"request": request}).data,
        "recent_activity": ActivityLogSerializer(activity[:10], many=True).data,
    }
    return data, account.is_closed


@extend_schema(parameters=[OpenApiParameter("file_format", str, enum=["csv", "xlsx"])], responses=bytes)
@api_view(["GET"])
def month_export(request, pk):
    account = get_object_or_404(MonthlyAccount, pk=pk)
    fmt = request.query_params.get("file_format", "xlsx")
    report = _visible(request, report_as_dict(account))
    name = f"mess-report-{account.year}-{account.month:02d}"
    if fmt == "csv":
        resp = HttpResponse(exports.report_csv(report), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{name}.csv"'
    elif fmt == "xlsx":
        resp = HttpResponse(
            exports.report_xlsx(report),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = f'attachment; filename="{name}.xlsx"'
    else:
        raise ValidationError({"file_format": "Use csv or xlsx."})
    return resp
