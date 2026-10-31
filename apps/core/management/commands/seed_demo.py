"""Load realistic demo data: a closed September 2026 and an open October 2026.

    python manage.py seed_demo            # only if the database has no members yet
    python manage.py seed_demo --reset    # wipe meals/money/members/months first
"""

import random
from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.core.models import ActivityLog, MessSettings
from apps.finance.models import Deposit, Expense, ExpenseCategory, ExtraBill, ExtraBillShare
from apps.meals.models import MealRecord, WeeklyMenu
from apps.members.models import Member
from apps.months.models import MonthlyAccount, MonthMember
from apps.months.services import close_month, start_month

D = Decimal

MEMBERS = [
    # name, username, phone, appetite (avg meals/day)
    ("Rabiul", "rabiul", "01711000001", 1.6),
    ("Asad Vai", "asad", "01711000002", 1.9),
    ("Sajib Vai", "sajib", "01711000003", 1.3),
    ("Abdur Rahim", "rahim", "01711000004", 1.1),
    ("Samin", "samin", "01711000005", 1.3),
    ("Nazir", "nazir", "01711000006", 1.8),
    ("Rahat Vai", "rahat", "01711000007", 1.1),
]
LATE_JOINER = ("Tanvir", "tanvir", "01711000008", 1.7)

BAZAR_ITEMS = ["চাল, ডাল, তেল", "মাছ (রুই)", "মুরগি", "সবজি", "ডিম ৩০টি", "আলু, পেঁয়াজ", "গরুর মাংস",
               "মসলা", "চিনি, চা পাতা", "সবজি ও লেবু", "ইলিশ মাছ", "ডাল ও আটা"]

MENU = {
    5: {"lunch": "ভাত, গরুর মাংস, ছোলার ডাল, সালাদ", "dinner": "ভাত, ডাল, ভর্তা"},  # Saturday
    6: {"lunch": "ভাত, মুরগি, ডাল", "dinner": "ভাত, সবজি, ডিম"},
    0: {"lunch": "খিচুড়ি, ডিম ভাজা, সালাদ", "dinner": "ভাত, মাছ, ডাল"},
    1: {"lunch": "ভাত, মাছ, সবজি, সালাদ", "dinner": "ভাত, ডাল, আলু ভাজি"},
    2: {"lunch": "ভাত, আলু ভর্তা, ডাল, ডিম", "dinner": "ভাত, মুরগি"},
    3: {"lunch": "খিচুড়ি, মুরগি, সালাদ", "dinner": "ভাত, সবজি, ডাল"},
    4: {"lunch": "পোলাও, গরুর মাংস", "dinner": "ভাত, মাছ, ডাল"},  # Friday
}


class Command(BaseCommand):
    help = "Seed realistic demo data (users, members, two months of meals and money)."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete existing mess data first.")
        parser.add_argument("--password", default="member123", help="Password for demo member logins.")
        parser.add_argument("--admin-password", default="admin12345", help="Password for the 'admin' manager.")

    @transaction.atomic
    def handle(self, *args, reset=False, password="member123", admin_password="admin12345", **opts):
        if reset:
            self._wipe()
        elif Member.objects.exists():
            raise CommandError("Members already exist. Use --reset to replace them with demo data.")

        self.rng = random.Random(42)
        settings = MessSettings.load()
        settings.name = "Mirpur Bachelor Mess"
        settings.save()

        admin, _ = User.objects.get_or_create(username="admin")
        admin.first_name, admin.role, admin.is_staff, admin.is_superuser = "Abdur Rahim", "manager", True, True
        admin.set_password(admin_password)
        admin.save()

        members = []
        for name, username, phone, _ in MEMBERS:
            user = admin if username == "rahim" else self._user(username, name, password)
            members.append(Member.objects.create(name=name, phone=phone, joined_on=date(2026, 1, 1), user=user))
        tanvir_user = self._user(LATE_JOINER[1], LATE_JOINER[0], password)
        tanvir = Member.objects.create(name=LATE_JOINER[0], phone=LATE_JOINER[2], joined_on=date(2026, 10, 10),
                                       user=tanvir_user)
        appetite = {m.pk: a for m, (*_, a) in zip(members, MEMBERS)}
        appetite[tanvir.pk] = LATE_JOINER[3]

        for weekday, meals in MENU.items():
            for meal_type, items in meals.items():
                WeeklyMenu.objects.update_or_create(weekday=weekday, meal_type=meal_type, defaults={"items": items})

        manager = members[3]  # Abdur Rahim does most of the bazar

        # ---- September 2026 (closed) ------------------------------------------
        sep = start_month(year=2026, month=9, user=admin, member_ids=[m.pk for m in members])
        self._fill_month(sep, members, appetite, manager, admin, last_day=30)
        close_month(sep, user=admin)

        # ---- October 2026 (open, data until today) -----------------------------
        octo = start_month(year=2026, month=10, user=admin, copy_from=sep, carry_forward=True)
        MonthMember.objects.create(account=octo, member=tanvir, joined_on=date(2026, 10, 10))
        today = timezone.localdate()
        last_day = today.day if (today.year, today.month) == (2026, 10) else 31
        self._fill_month(octo, members + [tanvir], appetite, manager, admin, last_day=last_day, current=True)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {Member.objects.count()} members, {MealRecord.objects.count()} meal entries, "
            f"{Expense.objects.count()} expenses, {Deposit.objects.count()} deposits.\n"
            f"Manager login: admin / {admin_password}   Member logins: rabiul, asad, sajib, samin, nazir, "
            f"rahat, tanvir / {password}"
        ))

    # ------------------------------------------------------------------
    def _wipe(self):
        ActivityLog.objects.all().delete()
        ExtraBillShare.objects.all().delete()
        ExtraBill.objects.all().delete()
        Expense.objects.all().delete()
        Deposit.objects.all().delete()
        MealRecord.objects.all().delete()
        MonthMember.objects.all().delete()
        MonthlyAccount.objects.all().delete()
        WeeklyMenu.objects.all().delete()
        users = list(Member.objects.exclude(user=None).values_list("user_id", flat=True))
        Member.objects.all().delete()
        User.objects.filter(pk__in=users, is_superuser=False).delete()

    def _user(self, username, name, password):
        user, _ = User.objects.get_or_create(username=username, defaults={"first_name": name, "role": "member"})
        user.set_password(password)
        user.is_active = True
        user.save()
        return user

    def _fill_month(self, account, members, appetite, manager, admin, last_day, current=False):
        rng = self.rng
        cat = {c.code: c for c in ExpenseCategory.objects.all()}
        enrolled = {mm.member_id: mm for mm in account.memberships.all()}

        meals = []
        for day in range(1, last_day + 1):
            d = date(account.year, account.month, day)
            for m in members:
                mm = enrolled.get(m.pk)
                if not mm or not mm.is_present_on(d):
                    continue
                if rng.random() < 0.08:  # away for the day
                    continue
                target = appetite[m.pk] + rng.uniform(-0.4, 0.4)
                meals.append(MealRecord(account=account, member=m, date=d, meal_type="lunch",
                                        quantity=D("1") if target > 0.6 else D("0.5"), created_by=admin))
                if target > 1.2:
                    meals.append(MealRecord(account=account, member=m, date=d, meal_type="dinner",
                                            quantity=D("2") if target > 2.1 else D("1"), created_by=admin))
        MealRecord.objects.bulk_create(meals)

        # Bazar every 1–3 days, mostly by the manager from the fund; sometimes members pay themselves.
        day = 1
        while day <= last_day:
            d = date(account.year, account.month, day)
            amount = D(rng.choice([480, 620, 780, 940, 1120, 1260, 1450, 1680, 1850, 2100]))
            personal = rng.random() < 0.2
            buyer = rng.choice(members[:7]) if personal else manager
            Expense.objects.create(
                account=account, date=d, category=cat["bazar"], amount=amount,
                description=rng.choice(BAZAR_ITEMS), purchaser=buyer,
                payment_source="personal" if personal else "fund", include_in_meal_rate=True,
                status="approved", created_by=admin, approved_by=admin,
            )
            day += rng.choice([1, 2, 2, 3])
        Expense.objects.create(account=account, date=date(account.year, account.month, min(5, last_day)),
                               category=cat["gas"], amount=D("1450"), description="গ্যাস সিলিন্ডার (১২ কেজি)",
                               purchaser=manager, include_in_meal_rate=True, created_by=admin, approved_by=admin)
        Expense.objects.create(account=account, date=date(account.year, account.month, min(10, last_day)),
                               category=cat["internet"], amount=D("1050"), description="WiFi bill",
                               purchaser=manager, include_in_meal_rate=False, created_by=admin, approved_by=admin)
        Expense.objects.create(account=account, date=date(account.year, account.month, min(20, last_day)),
                               category=cat["cleaning"], amount=D("700"), description="বুয়ার বেতন (cleaner)",
                               purchaser=manager, include_in_meal_rate=False, created_by=admin, approved_by=admin)
        if current:
            Expense.objects.create(account=account, date=date(account.year, account.month, last_day),
                                   category=cat["bazar"], amount=D("365"), description="সবজি ও ডিম",
                                   purchaser=members[4], payment_source="personal", include_in_meal_rate=True,
                                   status="pending", created_by=members[4].user)

        # Deposits: two or three instalments per member.
        for m in members:
            mm = enrolled.get(m.pk)
            if not mm:
                continue
            first = max(mm.joined_on.day, 1)
            for i, amount in enumerate(rng.sample([500, 1000, 1000, 1500, 2000], k=rng.choice([2, 3]))):
                day = min(first + i * 9 + rng.randint(0, 3), last_day)
                Deposit.objects.create(account=account, member=m, date=date(account.year, account.month, day),
                                       amount=D(amount), method=rng.choice(["cash", "cash", "bkash", "nagad"]),
                                       received_by=admin)

        # Electricity bill split equally; a repair paid by one member split among two.
        bill = ExtraBill.objects.create(account=account, date=date(account.year, account.month, min(12, last_day)),
                                        bill_ref=f"DESCO-{account.month:02d}{account.year}", title="Electricity bill",
                                        amount=D("2380"), payment_source="fund", split_mode="equal",
                                        created_by=admin)
        from apps.core.money import allocate

        shares = allocate(bill.amount, {mid: D(1) for mid in enrolled})
        ExtraBillShare.objects.bulk_create(
            [ExtraBillShare(bill=bill, member_id=mid, amount=a) for mid, a in shares.items()])
        repair = ExtraBill.objects.create(account=account, date=date(account.year, account.month, min(18, last_day)),
                                          bill_ref="", title="Fan repair (room 2)", amount=D("450"),
                                          payment_source="personal", paid_by=members[1], split_mode="custom",
                                          created_by=admin)
        ExtraBillShare.objects.bulk_create([ExtraBillShare(bill=repair, member=members[1], amount=D("250")),
                                            ExtraBillShare(bill=repair, member=members[5], amount=D("200"))])
