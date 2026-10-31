"""Monthly workflow: start a month, close (finalize) it, reopen it."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.activity import log_activity
from apps.members.models import Member
from apps.months.models import MonthlyAccount, MonthMember


def previous_account(account_or_year, month=None):
    if month is None:
        year, month = account_or_year.year, account_or_year.month
    else:
        year = account_or_year
    py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
    return MonthlyAccount.objects.filter(year=py, month=pm).first()


@transaction.atomic
def start_month(
    *,
    year: int,
    month: int,
    user=None,
    copy_from: MonthlyAccount | None = None,
    carry_forward: bool = True,
    member_ids: list[int] | None = None,
    fixed_cost_split: str | None = None,
) -> MonthlyAccount:
    """Create a month account and enrol members.

    * ``copy_from`` — copy its still-active members and its settings.
    * ``carry_forward`` — previous closing balances become opening balances.
    * ``member_ids`` — explicit list (used when there is nothing to copy from).
    """
    if MonthlyAccount.objects.filter(year=year, month=month).exists():
        raise ValidationError({"month": "An account for this month already exists."})

    account = MonthlyAccount.objects.create(
        year=year,
        month=month,
        fixed_cost_split=fixed_cost_split or (copy_from.fixed_cost_split if copy_from else "equal"),
    )

    balances = {}
    if copy_from and carry_forward:
        if copy_from.is_closed:
            balances = {
                mm.member_id: mm.closing_balance or 0
                for mm in copy_from.memberships.all()
            }
        else:
            from apps.reports.services import current_balances

            balances = current_balances(copy_from)

    if member_ids is not None:
        members = Member.objects.filter(pk__in=member_ids)
    elif copy_from:
        members = Member.objects.filter(
            memberships__account=copy_from, status=Member.Status.ACTIVE, memberships__left_on__isnull=True
        )
    else:
        members = Member.objects.filter(status=Member.Status.ACTIVE)

    MonthMember.objects.bulk_create(
        [
            MonthMember(
                account=account,
                member=m,
                joined_on=min(max(account.start_date, m.joined_on), account.end_date),
                opening_balance=balances.get(m.pk, 0),
            )
            for m in members.distinct()
        ]
    )
    log_activity(user, "create", account, f"Started {account.label}", account=account)
    return account


@transaction.atomic
def close_month(account: MonthlyAccount, user=None) -> MonthlyAccount:
    from apps.reports.services import build_report

    account = MonthlyAccount.objects.select_for_update().get(pk=account.pk)
    account.assert_open()
    if account.expenses.filter(status="pending").exists():
        raise ValidationError("Approve or reject all pending expenses before closing the month.")

    report = build_report(account)
    balances = {m["member_id"]: m["balance"] for m in report["summary"]["members"]}
    report["summary"]["status"] = MonthlyAccount.Status.CLOSED
    for mm in account.memberships.all():
        mm.closing_balance = balances.get(mm.member_id, 0)
        mm.save(update_fields=["closing_balance"])

    account.status = MonthlyAccount.Status.CLOSED
    account.closed_at = timezone.now()
    account.closed_by = user if getattr(user, "is_authenticated", False) else None
    account.final_report = report
    account.save(update_fields=["status", "closed_at", "closed_by", "final_report", "updated_at"])
    log_activity(user, "close", account, f"Closed and finalized {account.label}", account=account)
    return account


@transaction.atomic
def reopen_month(account: MonthlyAccount, user=None) -> MonthlyAccount:
    account = MonthlyAccount.objects.select_for_update().get(pk=account.pk)
    if not account.is_closed:
        raise ValidationError(f"{account.label} is already open.")
    account.status = MonthlyAccount.Status.OPEN
    account.closed_at = None
    account.closed_by = None
    account.final_report = None
    account.save(update_fields=["status", "closed_at", "closed_by", "final_report", "updated_at"])
    account.memberships.update(closing_balance=None)
    log_activity(user, "reopen", account, f"Reopened {account.label}", account=account)
    return account
