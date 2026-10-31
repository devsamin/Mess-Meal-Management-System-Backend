from datetime import date
from decimal import Decimal

from apps.accounts.models import User
from apps.finance.models import Deposit, Expense, ExpenseCategory, ExtraBill, ExtraBillShare
from apps.meals.models import MealRecord
from apps.members.models import Member
from apps.months.models import MonthlyAccount, MonthMember

D = Decimal


def make_month(year=2026, month=4, **kw) -> MonthlyAccount:
    return MonthlyAccount.objects.create(year=year, month=month, **kw)


def make_member(name, account=None, user=None, joined_on=None, opening=0, left_on=None) -> Member:
    member = Member.objects.create(name=name, user=user, joined_on=joined_on or date(2026, 1, 1))
    if account:
        MonthMember.objects.create(
            account=account, member=member, joined_on=joined_on or account.start_date,
            left_on=left_on, opening_balance=D(opening),
        )
    return member


def make_user(username, role=User.Role.MEMBER, password="pass12345!") -> User:
    user = User(username=username, role=role)
    user.set_password(password)
    user.save()
    return user


def category(code="bazar") -> ExpenseCategory:
    return ExpenseCategory.objects.get(code=code)


def add_meals(account, member, total, start_day=1):
    """Spread ``total`` meals over days as lunch+dinner (1 each), with a half dinner if needed."""
    total = D(str(total))
    day = start_day
    while total > 0:
        d = date(account.year, account.month, day)
        lunch = min(total, D(1))
        MealRecord.objects.create(account=account, member=member, date=d, meal_type="lunch", quantity=lunch)
        total -= lunch
        if total > 0:
            dinner = min(total, D(1))
            MealRecord.objects.create(account=account, member=member, date=d, meal_type="dinner", quantity=dinner)
            total -= dinner
        day += 1


def expense(account, amount, *, code="bazar", purchaser=None, source="fund", in_rate=None, status="approved",
            day=1):
    cat = category(code)
    return Expense.objects.create(
        account=account, date=date(account.year, account.month, day), category=cat, amount=D(str(amount)),
        purchaser=purchaser, payment_source=source,
        include_in_meal_rate=cat.include_in_meal_rate if in_rate is None else in_rate, status=status,
    )


def deposit(account, member, amount, kind="deposit", day=1):
    return Deposit.objects.create(account=account, member=member, date=date(account.year, account.month, day),
                                  amount=D(str(amount)), kind=kind)


def bill(account, amount, shares=None, *, in_rate=False, source="fund", paid_by=None, title="Bill"):
    b = ExtraBill.objects.create(account=account, date=account.start_date, title=title, amount=D(str(amount)),
                                 include_in_meal_rate=in_rate, payment_source=source, paid_by=paid_by,
                                 split_mode="custom" if shares else "equal")
    for member, amt in (shares or {}).items():
        ExtraBillShare.objects.create(bill=b, member=member, amount=D(str(amt)))
    return b
