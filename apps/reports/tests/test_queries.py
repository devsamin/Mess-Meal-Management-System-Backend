"""Guards against N+1 queries: the query count must not grow with the amount of data."""

from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.models import MessSettings
from apps.finance.models import Deposit, ExtraBill, ExtraBillShare
from apps.meals.models import MealRecord
from apps.reports.tests.factories import add_meals, expense, make_member, make_month, make_user

D = Decimal


class QueryCountTests(TestCase):
    def setUp(self):
        MessSettings.load()  # the first request would otherwise create the row and skew counts
        self.account = make_month(2026, 4)
        self.manager_user = make_user("manager", User.Role.MANAGER)
        self.member_user = make_user("alpha")
        self.manager = make_member("Manager", self.account, user=self.manager_user)
        self.alpha = make_member("Alpha", self.account, user=self.member_user)
        add_meals(self.account, self.alpha, 3)
        expense(self.account, 100, purchaser=self.manager)
        Deposit.objects.create(account=self.account, member=self.alpha, date="2026-04-02", amount=D("50"))
        # One record of every kind, so "before" already runs each query (empty results skip prefetches).
        bill = ExtraBill.objects.create(account=self.account, date="2026-04-04", title="wifi", amount=D("30"))
        ExtraBillShare.objects.create(bill=bill, member=self.alpha, amount=D("30"))

    def queries(self, user, url):
        c = APIClient()
        c.force_authenticate(user)
        with CaptureQueriesContext(connection) as ctx:
            r = c.get(url)
        self.assertEqual(r.status_code, 200, url)
        return len(ctx)

    def grow(self, n=12):
        for i in range(n):
            m = make_member(f"Extra {i}", self.account, user=make_user(f"extra{i}"))
            add_meals(self.account, m, 4)
            expense(self.account, 20 + i, purchaser=m, source="personal", code="gas")
            Deposit.objects.create(account=self.account, member=m, date="2026-04-03", amount=D("10"))
            bill = ExtraBill.objects.create(account=self.account, date="2026-04-04", title=f"b{i}", amount=D("30"))
            ExtraBillShare.objects.create(bill=bill, member=m, amount=D("30"))

    def test_month_endpoints_do_not_scale_with_members_or_records(self):
        m = self.account.pk
        urls = [
            f"/api/v1/months/{m}/summary/", f"/api/v1/months/{m}/report/", f"/api/v1/months/{m}/dashboard/",
            f"/api/v1/meals/grid/?month={m}", f"/api/v1/meals/day/?month={m}&date=2026-04-05",
            f"/api/v1/expenses/?month={m}", f"/api/v1/deposits/?month={m}", f"/api/v1/extra-bills/?month={m}",
            f"/api/v1/months/{m}/members/", f"/api/v1/activity/?account={m}",
        ]
        for user in (self.manager_user, self.member_user):
            before = {u: self.queries(user, u) for u in urls}
            if user is self.manager_user:
                self.grow()
            after = {u: self.queries(user, u) for u in urls}
            self.assertEqual(before, after, f"query counts changed with data size for {user.username}")

    def test_summary_stays_within_a_small_budget(self):
        self.grow()
        n = self.queries(self.manager_user, f"/api/v1/months/{self.account.pk}/summary/")
        self.assertLessEqual(n, 12, "summary used more queries than expected")

    def test_member_day_sheet_reads_settings_once(self):
        self.grow()
        c = APIClient()
        c.force_authenticate(self.member_user)
        with CaptureQueriesContext(connection) as ctx:
            c.get(f"/api/v1/meals/day/?month={self.account.pk}&date=2026-04-05")
        self.assertEqual(sum("core_messsettings" in q["sql"] for q in ctx), 1)


class BulkMealTests(TestCase):
    def setUp(self):
        self.account = make_month(2026, 4)
        self.manager_user = make_user("manager", User.Role.MANAGER)
        self.a = make_member("A", self.account)
        self.b = make_member("B", self.account)
        self.client = APIClient()
        self.client.force_authenticate(self.manager_user)

    def post(self, entries, day="2026-04-05"):
        return self.client.post("/api/v1/meals/bulk/", {"account": self.account.pk, "date": day, "entries": entries},
                                format="json")

    def qty(self, member, meal_type="lunch", day="2026-04-05"):
        rec = MealRecord.objects.filter(member=member, date=day, meal_type=meal_type).first()
        return rec.quantity if rec else None

    def test_mixed_create_update_delete_in_one_call(self):
        self.post([{"member": self.a.pk, "meal_type": "lunch", "quantity": "1"},
                   {"member": self.b.pk, "meal_type": "dinner", "quantity": "2"}])
        r = self.post([{"member": self.a.pk, "meal_type": "lunch", "quantity": "1.5"},   # update
                       {"member": self.b.pk, "meal_type": "dinner", "quantity": "0"},    # delete
                       {"member": self.b.pk, "meal_type": "lunch", "quantity": "1"},     # create
                       {"member": self.a.pk, "meal_type": "dinner", "quantity": "0"}])   # delete of nothing
        self.assertEqual(r.data, {"saved": 2, "deleted": 1})
        self.assertEqual(self.qty(self.a), D("1.5"))
        self.assertIsNone(self.qty(self.b, "dinner"))
        self.assertEqual(self.qty(self.b), D("1"))
        self.assertIsNone(self.qty(self.a, "dinner"))
        self.assertEqual(MealRecord.objects.count(), 2)

    def test_repeated_cell_in_one_payload_last_one_wins(self):
        r = self.post([{"member": self.a.pk, "meal_type": "lunch", "quantity": "1"},
                       {"member": self.a.pk, "meal_type": "lunch", "quantity": "2"}])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(MealRecord.objects.filter(member=self.a).count(), 1)
        self.assertEqual(self.qty(self.a), D("2"))

    def test_create_then_zero_in_one_payload_leaves_nothing(self):
        r = self.post([{"member": self.a.pk, "meal_type": "lunch", "quantity": "1"},
                       {"member": self.a.pk, "meal_type": "lunch", "quantity": "0"}])
        self.assertEqual(r.data, {"saved": 1, "deleted": 1})
        self.assertFalse(MealRecord.objects.exists())

    def test_update_records_who_changed_it_and_when(self):
        self.post([{"member": self.a.pk, "meal_type": "lunch", "quantity": "1"}])
        before = MealRecord.objects.get().updated_at
        self.post([{"member": self.a.pk, "meal_type": "lunch", "quantity": "2"}])
        rec = MealRecord.objects.get()
        self.assertGreater(rec.updated_at, before)
        self.assertEqual(rec.updated_by, self.manager_user)
        self.assertEqual(rec.created_by, self.manager_user)

    def test_bulk_save_uses_a_constant_number_of_queries(self):
        members = [make_member(f"M{i}", self.account) for i in range(20)]
        entries = [{"member": m.pk, "meal_type": t, "quantity": "1"} for m in members for t in ("lunch", "dinner")]
        with CaptureQueriesContext(connection) as ctx:
            r = self.post(entries)
        self.assertEqual(r.data["saved"], 40)
        self.assertLessEqual(len(ctx), 14)  # was ~4 per cell (160+) before batching

    def test_unknown_or_malformed_member_ids_are_still_rejected(self):
        for bad in (999999, "abc", None):
            r = self.post([{"member": bad, "meal_type": "lunch", "quantity": "1"}])
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn("member", r.data["entries"][0] if "entries" in r.data else r.data, bad)
        self.assertFalse(MealRecord.objects.exists())
