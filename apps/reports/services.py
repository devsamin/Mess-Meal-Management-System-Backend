"""Centralized financial calculations for a monthly account.

Every number the API returns about meal rates, costs and balances comes from
this module. Views, exports and the frontend only display the result.

Rules (see docs/ARCHITECTURE.md §4):

    meal_pool    = approved expenses in meal rate + extra bills in meal rate
    meal_rate    = meal_pool / total_meals
    meal_cost    = member meals × meal_rate (largest-remainder rounding, sums to meal_pool)
    shared_cost  = expenses NOT in meal rate, split equally or by days present
    bill_charges = member's share of extra bills NOT in meal rate
    credit       = opening balance + deposits − refunds + money paid personally
    balance      = credit − meal_cost − shared_cost − bill_charges
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Sum

from apps.core.money import ZERO, allocate, round_money
from apps.finance.models import Deposit, Expense, ExtraBill, ExtraBillShare, PaymentSource
from apps.meals.models import MealRecord, MealType
from apps.members.models import Member
from apps.months.models import MonthlyAccount, MonthMember

RATE_PLACES = Decimal("0.000001")


@dataclass
class MemberBalance:
    member_id: int
    name: str
    joined_on: date | None
    left_on: date | None
    days_present: int
    meals: Decimal = ZERO
    opening_balance: Decimal = ZERO
    deposits: Decimal = ZERO
    refunds: Decimal = ZERO
    personal_expenses: Decimal = ZERO
    personal_bills: Decimal = ZERO
    meal_cost: Decimal = ZERO
    shared_cost: Decimal = ZERO
    bill_charges: Decimal = ZERO

    @property
    def net_deposits(self) -> Decimal:
        return self.deposits - self.refunds

    @property
    def personal_paid(self) -> Decimal:
        return self.personal_expenses + self.personal_bills

    @property
    def total_credit(self) -> Decimal:
        return self.opening_balance + self.net_deposits + self.personal_paid

    @property
    def total_charges(self) -> Decimal:
        return self.meal_cost + self.shared_cost + self.bill_charges

    @property
    def balance(self) -> Decimal:
        return self.total_credit - self.total_charges

    @property
    def status(self) -> str:
        if self.balance > 0:
            return "receivable"  # the mess owes this member
        if self.balance < 0:
            return "payable"  # the member must pay
        return "settled"


@dataclass
class MonthSummary:
    account_id: int
    label: str
    year: int
    month: int
    status: str
    fixed_cost_split: str
    total_meals: Decimal = ZERO
    meal_expenses: Decimal = ZERO  # approved expenses counted in meal rate
    meal_bills: Decimal = ZERO  # extra bills counted in meal rate
    shared_expenses: Decimal = ZERO  # approved expenses split outside the meal rate
    split_bills: Decimal = ZERO  # extra bills split among selected members
    total_deposits: Decimal = ZERO
    total_refunds: Decimal = ZERO
    personal_contributions: Decimal = ZERO
    fund_spent: Decimal = ZERO
    opening_balance_total: Decimal = ZERO
    pending_expense_count: int = 0
    pending_expense_amount: Decimal = ZERO
    meal_rate_exact: Decimal = ZERO
    members: list[MemberBalance] = field(default_factory=list)
    category_totals: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def meal_pool(self) -> Decimal:
        return self.meal_expenses + self.meal_bills

    @property
    def total_expenses(self) -> Decimal:
        return self.meal_expenses + self.shared_expenses

    @property
    def total_extra_bills(self) -> Decimal:
        return self.meal_bills + self.split_bills

    @property
    def total_spending(self) -> Decimal:
        return self.total_expenses + self.total_extra_bills

    @property
    def net_deposits(self) -> Decimal:
        return self.total_deposits - self.total_refunds

    @property
    def meal_rate(self) -> Decimal:
        return round_money(self.meal_rate_exact)

    @property
    def cash_in_hand(self) -> Decimal:
        return self.opening_balance_total + self.net_deposits - self.fund_spent

    @property
    def total_balance(self) -> Decimal:
        return sum((m.balance for m in self.members), ZERO)

    @property
    def total_receivable(self) -> Decimal:
        return sum((m.balance for m in self.members if m.balance > 0), ZERO)

    @property
    def total_payable(self) -> Decimal:
        return -sum((m.balance for m in self.members if m.balance < 0), ZERO)


def _sum_by(qs, key: str, value: str = "amount") -> dict:
    return {row[key]: row["total"] or ZERO for row in qs.values(key).annotate(total=Sum(value)).order_by()}


def calculate_month(account: MonthlyAccount) -> MonthSummary:
    summary = MonthSummary(
        account_id=account.pk,
        label=account.label,
        year=account.year,
        month=account.month,
        status=account.status,
        fixed_cost_split=account.fixed_cost_split,
    )

    # --- Members taking part in this month -------------------------------
    lines: dict[int, MemberBalance] = {}
    # Through the reverse manager so ``mm.account`` is pre-filled (days_present reads it).
    for mm in account.memberships.select_related("member"):
        lines[mm.member_id] = MemberBalance(
            member_id=mm.member_id,
            name=mm.member.name,
            joined_on=mm.joined_on,
            left_on=mm.left_on,
            days_present=mm.days_present,
            opening_balance=mm.opening_balance,
        )

    def line(member_id: int) -> MemberBalance:
        # Safety net: money attached to someone without a membership row is never dropped.
        if member_id not in lines:
            m = Member.objects.get(pk=member_id)
            lines[member_id] = MemberBalance(m.pk, m.name, None, None, account.days_in_month)
            summary.warnings.append(f"{m.name} has records but is not a member of {account.label}.")
        return lines[member_id]

    # --- Meals -----------------------------------------------------------
    for member_id, total in _sum_by(MealRecord.objects.filter(account=account), "member_id", "quantity").items():
        line(member_id).meals = total
    summary.total_meals = sum((m.meals for m in lines.values()), ZERO)

    # --- Expenses ----------------------------------------------------------
    approved_rows = []
    for row in (
        Expense.objects.filter(account=account)
        .values("status", "include_in_meal_rate", "payment_source", "category_id", "category__name", "category__code")
        .annotate(total=Sum("amount"), n=Count("id"))
        .order_by()
    ):
        total = row["total"] or ZERO
        if row["status"] == Expense.Status.PENDING:
            summary.pending_expense_count += row["n"]
            summary.pending_expense_amount += total
        elif row["status"] == Expense.Status.APPROVED:
            approved_rows.append((row, total))

    fund_expenses = ZERO
    categories: dict[tuple, dict] = {}
    for row, total in approved_rows:
        if row["include_in_meal_rate"]:
            summary.meal_expenses += total
        else:
            summary.shared_expenses += total
        if row["payment_source"] == PaymentSource.FUND:
            fund_expenses += total
        entry = categories.setdefault(
            (row["category_id"], row["include_in_meal_rate"]),
            {
                "category_id": row["category_id"],
                "category": row["category__name"],
                "code": row["category__code"],
                "in_meal_rate": row["include_in_meal_rate"],
                "amount": ZERO,
                "count": 0,
            },
        )
        entry["amount"] += total
        entry["count"] += row["n"]
    summary.category_totals = sorted(categories.values(), key=lambda c: c["amount"], reverse=True)

    approved_personal = Expense.objects.filter(
        account=account, status=Expense.Status.APPROVED, payment_source=PaymentSource.PERSONAL
    )
    for member_id, total in _sum_by(approved_personal, "purchaser_id").items():
        line(member_id).personal_expenses = total

    # --- Extra bills ---------------------------------------------------------
    fund_bills = ZERO
    personal_bills: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for row in (
        ExtraBill.objects.filter(account=account)
        .values("include_in_meal_rate", "payment_source", "paid_by_id")
        .annotate(total=Sum("amount"))
        .order_by()
    ):
        total = row["total"] or ZERO
        if row["include_in_meal_rate"]:
            summary.meal_bills += total
        else:
            summary.split_bills += total
        if row["payment_source"] == PaymentSource.FUND:
            fund_bills += total
        else:
            personal_bills[row["paid_by_id"]] += total
    for member_id, total in personal_bills.items():
        line(member_id).personal_bills = total

    split_bills = ExtraBill.objects.filter(account=account, include_in_meal_rate=False)
    shares = ExtraBillShare.objects.filter(bill__in=split_bills)
    for member_id, total in _sum_by(shares, "member_id").items():
        line(member_id).bill_charges = total
    # Bills saved without any shares fall back to the shared (equal / by-days) pool.
    unshared_bills = split_bills.exclude(pk__in=shares.values("bill_id")).aggregate(t=Sum("amount"))["t"] or ZERO
    if unshared_bills:
        summary.warnings.append("Some extra bills have no responsible members; they are split among everyone.")

    # --- Deposits -----------------------------------------------------------
    for row in (
        Deposit.objects.filter(account=account).values("member_id", "kind").annotate(total=Sum("amount")).order_by()
    ):
        total = row["total"] or ZERO
        if row["kind"] == Deposit.Kind.DEPOSIT:
            line(row["member_id"]).deposits = total
        elif row["kind"] == Deposit.Kind.REFUND:
            line(row["member_id"]).refunds = total
    summary.total_deposits = sum((m.deposits for m in lines.values()), ZERO)
    summary.total_refunds = sum((m.refunds for m in lines.values()), ZERO)

    # --- Allocation ---------------------------------------------------------
    shared_pool = summary.shared_expenses + unshared_bills
    if summary.total_meals > 0:
        summary.meal_rate_exact = (summary.meal_pool / summary.total_meals).quantize(RATE_PLACES)
        meal_costs = allocate(summary.meal_pool, {mid: m.meals for mid, m in lines.items()})
        for mid, cost in meal_costs.items():
            lines[mid].meal_cost = cost
    elif summary.meal_pool > 0:
        summary.warnings.append("No meals recorded yet, so meal expenses are split like shared costs for now.")
        shared_pool += summary.meal_pool

    if shared_pool > 0 and lines:
        if account.fixed_cost_split == MonthlyAccount.FixedCostSplit.BY_DAYS:
            weights = {mid: Decimal(m.days_present) for mid, m in lines.items()}
        else:
            weights = {mid: Decimal(1) for mid in lines}
        for mid, cost in allocate(shared_pool, weights).items():
            lines[mid].shared_cost = cost

    summary.personal_contributions = sum((m.personal_paid for m in lines.values()), ZERO)
    summary.fund_spent = fund_expenses + fund_bills
    summary.opening_balance_total = sum((m.opening_balance for m in lines.values()), ZERO)
    summary.members = sorted(lines.values(), key=lambda m: m.name.lower())

    if summary.cash_in_hand < 0:
        summary.warnings.append("The common fund has spent more than it received (negative cash in hand).")
    return summary


# ---------------------------------------------------------------------------
# Serialisation helpers (Decimal → string so JSON keeps exact values)
# ---------------------------------------------------------------------------


def _s(value) -> str:
    return str(round_money(value))


def _num(value) -> str:
    """Plain number without trailing zeros or exponent: 40.0 -> "40", 1.5 -> "1.5"."""
    if not value:
        return "0"
    return format(value.normalize(), "f")


def member_to_dict(m: MemberBalance) -> dict:
    return {
        "member_id": m.member_id,
        "name": m.name,
        "joined_on": m.joined_on.isoformat() if m.joined_on else None,
        "left_on": m.left_on.isoformat() if m.left_on else None,
        "days_present": m.days_present,
        "meals": _num(m.meals),
        "opening_balance": _s(m.opening_balance),
        "deposits": _s(m.deposits),
        "refunds": _s(m.refunds),
        "net_deposits": _s(m.net_deposits),
        "personal_expenses": _s(m.personal_expenses),
        "personal_bills": _s(m.personal_bills),
        "personal_paid": _s(m.personal_paid),
        "total_credit": _s(m.total_credit),
        "meal_cost": _s(m.meal_cost),
        "shared_cost": _s(m.shared_cost),
        "bill_charges": _s(m.bill_charges),
        "total_charges": _s(m.total_charges),
        "balance": _s(m.balance),
        "status": m.status,
    }


def summary_to_dict(s: MonthSummary) -> dict:
    return {
        "account_id": s.account_id,
        "label": s.label,
        "year": s.year,
        "month": s.month,
        "status": s.status,
        "fixed_cost_split": s.fixed_cost_split,
        "total_meals": _num(s.total_meals),
        "meal_rate": _s(s.meal_rate),
        "meal_rate_exact": str(s.meal_rate_exact),
        "meal_pool": _s(s.meal_pool),
        "meal_expenses": _s(s.meal_expenses),
        "meal_bills": _s(s.meal_bills),
        "shared_expenses": _s(s.shared_expenses),
        "split_bills": _s(s.split_bills),
        "total_expenses": _s(s.total_expenses),
        "total_extra_bills": _s(s.total_extra_bills),
        "total_spending": _s(s.total_spending),
        "total_deposits": _s(s.total_deposits),
        "total_refunds": _s(s.total_refunds),
        "net_deposits": _s(s.net_deposits),
        "personal_contributions": _s(s.personal_contributions),
        "fund_spent": _s(s.fund_spent),
        "opening_balance_total": _s(s.opening_balance_total),
        "cash_in_hand": _s(s.cash_in_hand),
        "total_balance": _s(s.total_balance),
        "total_receivable": _s(s.total_receivable),
        "total_payable": _s(s.total_payable),
        "balance_check_ok": round_money(s.total_balance) == round_money(s.cash_in_hand),
        "pending_expense_count": s.pending_expense_count,
        "pending_expense_amount": _s(s.pending_expense_amount),
        "category_totals": [
            {**c, "amount": _s(c["amount"])} for c in s.category_totals
        ],
        "members": [member_to_dict(m) for m in s.members],
        "warnings": s.warnings,
    }


# ---------------------------------------------------------------------------
# Meal tables used by the dashboard, the monthly grid and reports
# ---------------------------------------------------------------------------


def month_days(account: MonthlyAccount) -> list[date]:
    return [account.start_date + timedelta(days=i) for i in range(account.days_in_month)]


def daily_meal_totals(account: MonthlyAccount) -> list[dict]:
    """One row per day: totals per meal type and overall."""
    rows = {d: {"date": d.isoformat(), "lunch": ZERO, "dinner": ZERO, "total": ZERO}
            for d in month_days(account)}
    qs = (
        MealRecord.objects.filter(account=account)
        .values("date", "meal_type")
        .annotate(total=Sum("quantity"))
        .order_by()
    )
    for r in qs:
        if r["meal_type"] not in ("lunch", "dinner"):
            continue
        row = rows[r["date"]]
        row[r["meal_type"]] += r["total"]
        row["total"] += r["total"]
    return [{k: (_num(v) if isinstance(v, Decimal) else v) for k, v in row.items()} for row in rows.values()]


def member_day_grid(account: MonthlyAccount) -> dict:
    """Member × day matrix of meal totals (like the original spreadsheet)."""
    days = month_days(account)
    members = list(
        MonthMember.objects.filter(account=account).select_related("member").order_by("member__name")
    )
    cells: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for r in (
        MealRecord.objects.filter(account=account)
        .values("member_id", "date")
        .annotate(total=Sum("quantity"))
        .order_by()
    ):
        cells[r["member_id"]][r["date"].isoformat()] += r["total"]

    rows = []
    for mm in members:
        per_day = cells.get(mm.member_id, {})
        rows.append(
            {
                "member_id": mm.member_id,
                "name": mm.member.name,
                "days": {d.isoformat(): _num(per_day.get(d.isoformat(), ZERO)) for d in days},
                "total": _num(sum(per_day.values(), ZERO)),
            }
        )
    day_totals = {
        d.isoformat(): _num(sum((cells[m.member_id].get(d.isoformat(), ZERO) for m in members), ZERO))
        for d in days
    }
    return {"days": [d.isoformat() for d in days], "rows": rows, "day_totals": day_totals}


def build_report(account: MonthlyAccount) -> dict:
    """Complete month report — also what is frozen into ``final_report`` on close."""
    summary = calculate_month(account)
    expenses = (
        Expense.objects.filter(account=account, status=Expense.Status.APPROVED)
        .select_related("category", "purchaser")
        .order_by("date", "id")
    )
    deposits = Deposit.objects.filter(account=account).select_related("member").order_by("date", "id")
    bills = ExtraBill.objects.filter(account=account).prefetch_related("shares__member").select_related("paid_by")
    return {
        "summary": summary_to_dict(summary),
        "daily_meals": daily_meal_totals(account),
        "meal_grid": member_day_grid(account),
        "expenses": [
            {
                "id": e.pk,
                "date": e.date.isoformat(),
                "category": e.category.name,
                "description": e.description,
                "purchaser": e.purchaser.name if e.purchaser else None,
                "payment_source": e.payment_source,
                "in_meal_rate": e.include_in_meal_rate,
                "amount": _s(e.amount),
            }
            for e in expenses
        ],
        "deposits": [
            {
                "id": d.pk,
                "date": d.date.isoformat(),
                "member": d.member.name,
                "kind": d.kind,
                "method": d.method,
                "note": d.note,
                "amount": _s(d.amount),
            }
            for d in deposits
        ],
        "extra_bills": [
            {
                "id": b.pk,
                "date": b.date.isoformat(),
                "bill_ref": b.bill_ref,
                "title": b.title,
                "amount": _s(b.amount),
                "payment_source": b.payment_source,
                "paid_by": b.paid_by.name if b.paid_by else None,
                "in_meal_rate": b.include_in_meal_rate,
                "shares": [{"member": s.member.name, "amount": _s(s.amount)} for s in b.shares.all()],
            }
            for b in bills.order_by("date", "id")
        ],
    }


def summary_as_dict(account: MonthlyAccount) -> dict:
    """Frozen summary for closed months, live calculation for open ones."""
    if account.is_closed and account.final_report:
        return account.final_report["summary"]
    return summary_to_dict(calculate_month(account))


def report_as_dict(account: MonthlyAccount) -> dict:
    if account.is_closed and account.final_report:
        return account.final_report
    return build_report(account)


def current_balances(account: MonthlyAccount) -> dict[int, Decimal]:
    return {m.member_id: round_money(m.balance) for m in calculate_month(account).members}
