from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.models import MessSettings
from apps.finance.models import Expense
from apps.meals.models import MealRecord
from apps.months.models import MonthlyAccount, MonthMember
from apps.reports.tests.factories import (
    add_meals, category, deposit, expense, make_member, make_month, make_user,
)

D = Decimal
TODAY = date(2026, 4, 15)


def patch_today():
    return mock.patch("django.utils.timezone.localdate", return_value=TODAY)


class ApiTestCase(TestCase):
    def setUp(self):
        self.account = make_month(2026, 4)
        self.manager_user = make_user("manager", User.Role.MANAGER)
        self.member_user = make_user("alpha")
        self.other_user = make_user("bravo")
        self.manager = make_member("Manager", self.account, user=self.manager_user)
        self.alpha = make_member("Alpha", self.account, user=self.member_user)
        self.bravo = make_member("Bravo", self.account, user=self.other_user)
        self.client = APIClient()

    def as_user(self, user):
        self.client.force_authenticate(user)
        return self.client


class AuthTests(ApiTestCase):
    def test_login_returns_jwt_and_me(self):
        r = self.client.post("/api/v1/auth/login/", {"username": "alpha", "password": "pass12345!"}, format="json")
        self.assertEqual(r.status_code, 200)
        token = r.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        me = self.client.get("/api/v1/auth/me/")
        self.assertEqual(me.data["member"]["name"], "Alpha")
        self.assertFalse(me.data["is_manager"])

    def test_anonymous_is_rejected(self):
        self.assertEqual(self.client.get(f"/api/v1/months/{self.account.pk}/summary/").status_code, 401)


class PermissionTests(ApiTestCase):
    def test_member_cannot_record_deposits_or_manage_members(self):
        c = self.as_user(self.member_user)
        r = c.post("/api/v1/deposits/", {"account": self.account.pk, "member": self.alpha.pk,
                                         "date": "2026-04-02", "amount": "500"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(c.post("/api/v1/members/", {"name": "Hacker"}, format="json").status_code, 403)
        self.assertEqual(c.post(f"/api/v1/months/{self.account.pk}/close/").status_code, 403)

    def test_manager_can_record_deposit(self):
        r = self.as_user(self.manager_user).post(
            "/api/v1/deposits/", {"account": self.account.pk, "member": self.alpha.pk, "date": "2026-04-02",
                                  "amount": "500.00", "method": "bkash"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def test_member_enters_only_own_meals_for_today(self):
        c = self.as_user(self.member_user)
        with patch_today():
            ok = c.post("/api/v1/meals/bulk/", {"account": self.account.pk, "date": "2026-04-15", "entries": [
                {"member": self.alpha.pk, "meal_type": "lunch", "quantity": "1"}]}, format="json")
            other = c.post("/api/v1/meals/bulk/", {"account": self.account.pk, "date": "2026-04-15", "entries": [
                {"member": self.bravo.pk, "meal_type": "lunch", "quantity": "1"}]}, format="json")
            past = c.post("/api/v1/meals/bulk/", {"account": self.account.pk, "date": "2026-04-10", "entries": [
                {"member": self.alpha.pk, "meal_type": "lunch", "quantity": "1"}]}, format="json")
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertEqual(other.status_code, 403)
        self.assertEqual(past.status_code, 403)

    def test_member_meal_entry_can_be_turned_off(self):
        s = MessSettings.load()
        s.allow_member_meal_entry = False
        s.save()
        with patch_today():
            r = self.as_user(self.member_user).post("/api/v1/meals/bulk/", {
                "account": self.account.pk, "date": "2026-04-15",
                "entries": [{"member": self.alpha.pk, "meal_type": "lunch", "quantity": "1"}]}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_member_expense_is_pending_until_approved(self):
        c = self.as_user(self.member_user)
        r = c.post("/api/v1/expenses/", {"account": self.account.pk, "date": "2026-04-03",
                                         "category": category().pk, "amount": "350", "payment_source": "personal",
                                         "description": "fish"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["status"], "pending")
        self.assertEqual(r.data["purchaser"], self.alpha.pk)  # forced to own member
        summary = c.get(f"/api/v1/months/{self.account.pk}/summary/").data
        self.assertEqual(summary["meal_pool"], "0.00")

        self.assertEqual(c.post(f"/api/v1/expenses/{r.data['id']}/approve/").status_code, 403)
        ok = self.as_user(self.manager_user).post(f"/api/v1/expenses/{r.data['id']}/approve/")
        self.assertEqual(ok.data["status"], "approved")
        summary = self.client.get(f"/api/v1/months/{self.account.pk}/summary/").data
        self.assertEqual(summary["meal_pool"], "350.00")

    def test_member_cannot_edit_approved_expense(self):
        e = expense(self.account, 100, purchaser=self.alpha, source="personal")
        e.created_by = self.member_user
        e.save()
        r = self.as_user(self.member_user).patch(f"/api/v1/expenses/{e.pk}/", {"amount": "999"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_transparency_off_hides_other_members(self):
        s = MessSettings.load()
        s.members_can_view_all = False
        s.save()
        deposit(self.account, self.bravo, 700)
        c = self.as_user(self.member_user)
        summary = c.get(f"/api/v1/months/{self.account.pk}/summary/").data
        self.assertEqual([m["name"] for m in summary["members"]], ["Alpha"])
        deposits = c.get("/api/v1/deposits/", {"month": self.account.pk}).data
        self.assertEqual(deposits["count"], 0)

    def test_closed_month_is_read_only(self):
        self.account.status = MonthlyAccount.Status.CLOSED
        self.account.save()
        r = self.as_user(self.manager_user).post("/api/v1/deposits/", {
            "account": self.account.pk, "member": self.alpha.pk, "date": "2026-04-02", "amount": "100"},
            format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("closed", str(r.data))


class MealEntryTests(ApiTestCase):
    def post_bulk(self, entries, day="2026-04-05"):
        return self.as_user(self.manager_user).post(
            "/api/v1/meals/bulk/", {"account": self.account.pk, "date": day, "entries": entries}, format="json")

    def test_bulk_upsert_never_duplicates(self):
        self.post_bulk([{"member": self.alpha.pk, "meal_type": "lunch", "quantity": "1"}])
        self.post_bulk([{"member": self.alpha.pk, "meal_type": "lunch", "quantity": "2"}])
        rows = MealRecord.objects.filter(member=self.alpha)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().quantity, D("2"))

    def test_zero_removes_entry_and_invalid_values_rejected(self):
        self.post_bulk([{"member": self.alpha.pk, "meal_type": "dinner", "quantity": "1"}])
        self.post_bulk([{"member": self.alpha.pk, "meal_type": "dinner", "quantity": "0"}])
        self.assertFalse(MealRecord.objects.exists())
        r = self.post_bulk([{"member": self.alpha.pk, "meal_type": "dinner", "quantity": "0.3"}])
        self.assertEqual(r.status_code, 400)

    def test_date_outside_month_rejected(self):
        r = self.post_bulk([{"member": self.alpha.pk, "meal_type": "lunch", "quantity": "1"}], day="2026-05-01")
        self.assertEqual(r.status_code, 400)

    def test_day_sheet_shows_all_members(self):
        self.post_bulk([{"member": self.alpha.pk, "meal_type": "lunch", "quantity": "1.5"}])
        r = self.client.get("/api/v1/meals/day/", {"month": self.account.pk, "date": "2026-04-05"})
        self.assertEqual(len(r.data["rows"]), 3)
        self.assertEqual(r.data["totals"]["lunch"], "1.5")


class MonthWorkflowTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        add_meals(self.account, self.alpha, 10)
        add_meals(self.account, self.bravo, 10)
        deposit(self.account, self.alpha, 1000)
        deposit(self.account, self.bravo, 200)
        expense(self.account, 800, purchaser=self.manager)

    def test_close_snapshots_and_start_next_month_carries_balances(self):
        c = self.as_user(self.manager_user)
        r = c.post(f"/api/v1/months/{self.account.pk}/close/")
        self.assertEqual(r.status_code, 200, r.data)
        self.account.refresh_from_db()
        self.assertTrue(self.account.is_closed)
        self.assertEqual(self.account.final_report["summary"]["meal_rate"], "40.00")
        alpha_mm = MonthMember.objects.get(account=self.account, member=self.alpha)
        self.assertEqual(alpha_mm.closing_balance, D("600.00"))

        # editing a closed month is blocked, but the report stays available
        expense(self.account, 1, purchaser=self.manager)  # bypass API: snapshot must not change
        self.assertEqual(c.get(f"/api/v1/months/{self.account.pk}/summary/").data["meal_pool"], "800.00")

        self.bravo.status = "inactive"
        self.bravo.save()
        r = c.post("/api/v1/months/", {"year": 2026, "month": 5, "carry_forward": True}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        may = MonthlyAccount.objects.get(year=2026, month=5)
        names = set(may.memberships.values_list("member__name", flat=True))
        self.assertEqual(names, {"Alpha", "Manager"})  # inactive Bravo not copied
        self.assertEqual(may.memberships.get(member=self.alpha).opening_balance, D("600.00"))
        self.assertEqual(c.get(f"/api/v1/months/{may.pk}/summary/").data["cash_in_hand"], "600.00")

    def test_cannot_close_with_pending_expenses_and_reopen_works(self):
        expense(self.account, 50, status="pending", purchaser=self.alpha, source="personal")
        c = self.as_user(self.manager_user)
        self.assertEqual(c.post(f"/api/v1/months/{self.account.pk}/close/").status_code, 400)
        Expense.objects.filter(status="pending").update(status="rejected")
        self.assertEqual(c.post(f"/api/v1/months/{self.account.pk}/close/").status_code, 200)
        r = c.post(f"/api/v1/months/{self.account.pk}/reopen/")
        self.assertEqual(r.data["status"], "open")
        self.account.refresh_from_db()
        self.assertIsNone(self.account.final_report)

    def test_duplicate_month_rejected(self):
        r = self.as_user(self.manager_user).post("/api/v1/months/", {"year": 2026, "month": 4}, format="json")
        self.assertEqual(r.status_code, 400)


class ReportTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        add_meals(self.account, self.alpha, 3)
        expense(self.account, 300, purchaser=self.alpha, source="personal")
        deposit(self.account, self.bravo, 100)

    def test_report_contains_all_sections(self):
        r = self.as_user(self.member_user).get(f"/api/v1/months/{self.account.pk}/report/")
        self.assertEqual(r.status_code, 200)
        for key in ("summary", "daily_meals", "meal_grid", "expenses", "deposits", "extra_bills"):
            self.assertIn(key, r.data)
        self.assertEqual(len(r.data["daily_meals"]), 30)
        alpha = next(m for m in r.data["summary"]["members"] if m["name"] == "Alpha")
        self.assertEqual(alpha["balance"], "0.00")  # paid 300 personally, ate 300

    def test_exports(self):
        c = self.as_user(self.manager_user)
        csv_resp = c.get(f"/api/v1/months/{self.account.pk}/export/", {"file_format": "csv"})
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn("Alpha", csv_resp.content.decode("utf-8-sig"))
        xlsx = c.get(f"/api/v1/months/{self.account.pk}/export/", {"file_format": "xlsx"})
        self.assertEqual(xlsx.status_code, 200)
        self.assertTrue(xlsx.content.startswith(b"PK"))

    def test_dashboard(self):
        with patch_today():
            r = self.as_user(self.member_user).get(f"/api/v1/months/{self.account.pk}/dashboard/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["my_balance"]["name"], "Alpha")
        self.assertEqual(r.data["today_date"], TODAY.isoformat())


class MemberLifecycleTests(ApiTestCase):
    def test_member_with_history_cannot_be_deleted_but_can_be_deactivated(self):
        add_meals(self.account, self.alpha, 1)
        c = self.as_user(self.manager_user)
        r = c.delete(f"/api/v1/members/{self.alpha.pk}/")
        self.assertEqual(r.status_code, 400)
        r = c.post(f"/api/v1/members/{self.alpha.pk}/deactivate/")
        self.assertEqual(r.data["status"], "inactive")
        self.member_user.refresh_from_db()
        self.assertFalse(self.member_user.is_active)

    def test_new_member_without_history_can_be_deleted(self):
        c = self.as_user(self.manager_user)
        r = c.post("/api/v1/members/", {"name": "Newbie", "add_to_month": self.account.pk,
                                        "login_username": "newbie", "login_password": "Strong-pass-1"},
                   format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["username"], "newbie")
        self.assertTrue(MonthMember.objects.filter(member_id=r.data["id"], account=self.account).exists())
        self.assertEqual(c.delete(f"/api/v1/members/{r.data['id']}/").status_code, 204)
        self.assertFalse(User.objects.filter(username="newbie").exists())


class ExtraBillApiTests(ApiTestCase):
    def test_equal_split_rounds_to_exact_total(self):
        r = self.as_user(self.manager_user).post("/api/v1/extra-bills/", {
            "account": self.account.pk, "date": "2026-04-08", "title": "Wifi router", "amount": "1000",
            "split_mode": "equal",
            "share_inputs": [{"member": m.pk} for m in (self.manager, self.alpha, self.bravo)],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        amounts = sorted(D(s["amount"]) for s in r.data["shares"])
        self.assertEqual(sum(amounts), D("1000"))
        self.assertEqual(amounts, [D("333.33"), D("333.33"), D("333.34")])

    def test_custom_split_must_match_amount(self):
        r = self.as_user(self.manager_user).post("/api/v1/extra-bills/", {
            "account": self.account.pk, "date": "2026-04-08", "title": "Repair", "amount": "500",
            "split_mode": "custom",
            "share_inputs": [{"member": self.alpha.pk, "amount": "200"}, {"member": self.bravo.pk, "amount": "200"}],
        }, format="json")
        self.assertEqual(r.status_code, 400)
