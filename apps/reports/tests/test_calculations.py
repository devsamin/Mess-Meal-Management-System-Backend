from datetime import date
from decimal import Decimal

from django.test import TestCase

from apps.core.money import allocate
from apps.reports.services import calculate_month, summary_to_dict
from apps.reports.tests.factories import add_meals, bill, deposit, expense, make_member, make_month

D = Decimal


class AllocateTests(TestCase):
    def test_parts_always_sum_to_total(self):
        parts = allocate(D("100"), {"a": D(1), "b": D(1), "c": D(1)})
        self.assertEqual(sum(parts.values()), D("100.00"))
        self.assertEqual(sorted(parts.values()), [D("33.33"), D("33.33"), D("33.34")])

    def test_proportional_and_zero_weights(self):
        parts = allocate(D("90"), {"a": D(2), "b": D(1), "c": D(0)})
        self.assertEqual(parts, {"a": D("60.00"), "b": D("30.00"), "c": D("0.00")})

    def test_all_zero_weights(self):
        self.assertEqual(allocate(D("50"), {"a": D(0)}), {"a": D("0.00")})


class SpreadsheetParityTests(TestCase):
    """Reproduces the April sheet: 280.5 meals, ৳14,720 bazar from the fund, deposits ৳15,280."""

    MEMBERS = {
        # name: (meals, deposits, sheet balance)
        "Rabiul": ("46.5", [660, 500, 1040, 300], "59.79"),
        "Asad Vai": ("55", [2000, 500], "-386.27"),
        "Sajib Vai": ("35", [1000, 500, 500], "163.28"),
        "Abdur Rahim": ("28", [1730, 360, 140, 320], "1080.62"),
        "Samin": ("34", [1000, 80, 500], "-204.24"),
        "Nazir": ("53", [1600, 410, 1000], "228.68"),
        "Rahat Vai": ("29", [640, 500], "-381.85"),
    }
    # Some values in the screenshot are hard to read; the last one is set so the list totals ৳14,720 as on the sheet.
    EXPENSES = [1730, 410, 220, 260, 410, 30, 450, 200, 180, 2440, 410, 240, 460, 40, 270, 340, 1220, 630, 60,
                215, 100, 140, 940, 70, 70, 110, 1280, 180, 330, 415, 590, 280]

    def setUp(self):
        self.account = make_month(2026, 4)
        self.members = {}
        for name, (meals, deposits, _) in self.MEMBERS.items():
            m = make_member(name, self.account)
            self.members[name] = m
            add_meals(self.account, m, meals)
            for amount in deposits:
                deposit(self.account, m, amount)
        manager = self.members["Abdur Rahim"]
        for amount in self.EXPENSES:
            expense(self.account, amount, purchaser=manager)  # bought by the manager with fund money

    def test_totals_and_meal_rate(self):
        s = calculate_month(self.account)
        self.assertEqual(sum(self.EXPENSES), 14720)
        self.assertEqual(s.total_meals, D("280.5"))
        self.assertEqual(s.meal_pool, D("14720"))
        self.assertEqual(s.meal_rate, D("52.48"))
        self.assertEqual(s.net_deposits, D("15280"))
        self.assertEqual(s.cash_in_hand, D("560"))

    def test_balances_match_sheet_within_a_paisa(self):
        s = calculate_month(self.account)
        by_name = {m.name: m for m in s.members}
        for name, (_, _, sheet_balance) in self.MEMBERS.items():
            self.assertAlmostEqual(by_name[name].balance, D(sheet_balance), delta=D("0.01"), msg=name)

    def test_balances_add_up_exactly_to_cash_in_hand(self):
        s = calculate_month(self.account)
        self.assertEqual(s.total_balance, s.cash_in_hand)
        self.assertTrue(summary_to_dict(s)["balance_check_ok"])


class AccountingRuleTests(TestCase):
    def setUp(self):
        self.account = make_month(2026, 5)
        self.a = make_member("Alpha", self.account)
        self.b = make_member("Bravo", self.account)

    def line(self, summary, member):
        return next(m for m in summary.members if m.member_id == member.pk)

    def test_personal_bazar_is_credited_once_and_not_taken_from_fund(self):
        deposit(self.account, self.a, 1000)
        deposit(self.account, self.b, 1000)
        expense(self.account, 500, purchaser=self.a, source="personal")
        expense(self.account, 300, purchaser=self.b, source="fund")
        add_meals(self.account, self.a, 10)
        add_meals(self.account, self.b, 10)

        s = calculate_month(self.account)
        self.assertEqual(s.meal_rate, D("40.00"))
        a, b = self.line(s, self.a), self.line(s, self.b)
        self.assertEqual(a.personal_paid, D("500"))
        self.assertEqual(b.personal_paid, D("0"))  # fund purchase is NOT a personal credit
        self.assertEqual(a.balance, D("1100.00"))  # 1000 + 500 − 400
        self.assertEqual(b.balance, D("600.00"))  # 1000 − 400
        self.assertEqual(s.fund_spent, D("300"))
        self.assertEqual(s.cash_in_hand, D("1700"))
        self.assertEqual(s.total_balance, s.cash_in_hand)

    def test_non_meal_expenses_split_equally_outside_meal_rate(self):
        expense(self.account, 1000, code="electricity")  # not in meal rate by default
        expense(self.account, 600)  # bazar
        add_meals(self.account, self.a, 4)
        add_meals(self.account, self.b, 2)
        s = calculate_month(self.account)
        self.assertEqual(s.meal_rate, D("100.00"))
        self.assertEqual(self.line(s, self.a).shared_cost, D("500.00"))
        self.assertEqual(self.line(s, self.a).meal_cost, D("400.00"))
        self.assertEqual(self.line(s, self.b).meal_cost, D("200.00"))

    def test_expense_can_override_category_default(self):
        expense(self.account, 300, code="electricity", in_rate=True)
        add_meals(self.account, self.a, 3)
        s = calculate_month(self.account)
        self.assertEqual(s.meal_rate, D("100.00"))
        self.assertEqual(s.shared_expenses, D("0"))

    def test_split_by_days_present_for_mid_month_joiner(self):
        self.account.fixed_cost_split = "by_days"
        self.account.save()
        late = make_member("Charlie", self.account, joined_on=date(2026, 5, 17))  # 15 of 31 days
        expense(self.account, 770, code="internet")
        s = calculate_month(self.account)
        self.assertEqual(self.line(s, late).days_present, 15)
        # weights 31 : 31 : 15 → 310 / 310 / 150
        self.assertEqual(self.line(s, late).shared_cost, D("150.00"))
        self.assertEqual(self.line(s, self.a).shared_cost, D("310.00"))

    def test_extra_bill_custom_split_and_personal_payer(self):
        bill(self.account, 900, {self.a: 600, self.b: 300}, source="personal", paid_by=self.b)
        s = calculate_month(self.account)
        a, b = self.line(s, self.a), self.line(s, self.b)
        self.assertEqual(a.bill_charges, D("600"))
        self.assertEqual(b.bill_charges, D("300"))
        self.assertEqual(b.personal_paid, D("900"))
        self.assertEqual(a.balance, D("-600.00"))
        self.assertEqual(b.balance, D("600.00"))
        self.assertEqual(s.cash_in_hand, D("0"))

    def test_extra_bill_in_meal_rate(self):
        bill(self.account, 200, in_rate=True)
        expense(self.account, 800)
        add_meals(self.account, self.a, 5)
        add_meals(self.account, self.b, 5)
        s = calculate_month(self.account)
        self.assertEqual(s.meal_pool, D("1000"))
        self.assertEqual(s.meal_rate, D("100.00"))
        self.assertEqual(self.line(s, self.a).bill_charges, D("0"))

    def test_pending_and_rejected_expenses_are_ignored(self):
        expense(self.account, 500, status="pending", purchaser=self.a, source="personal")
        expense(self.account, 700, status="rejected")
        expense(self.account, 100)
        add_meals(self.account, self.a, 1)
        s = calculate_month(self.account)
        self.assertEqual(s.meal_pool, D("100"))
        self.assertEqual(s.pending_expense_count, 1)
        self.assertEqual(self.line(s, self.a).personal_paid, D("0"))

    def test_refunds_and_opening_balance(self):
        c = make_member("Charlie", self.account, opening=250)
        deposit(self.account, c, 1000)
        deposit(self.account, c, 400, kind="refund")
        s = calculate_month(self.account)
        line = self.line(s, c)
        self.assertEqual(line.net_deposits, D("600"))
        self.assertEqual(line.balance, D("850"))
        self.assertEqual(s.cash_in_hand, D("850"))

    def test_meal_costs_sum_exactly_to_pool_with_awkward_rate(self):
        expense(self.account, 1000)
        add_meals(self.account, self.a, 1)
        add_meals(self.account, self.b, 2)
        s = calculate_month(self.account)
        self.assertEqual(s.meal_rate, D("333.33"))
        self.assertEqual(sum(m.meal_cost for m in s.members), D("1000.00"))

    def test_no_meals_yet_splits_meal_pool_as_shared(self):
        expense(self.account, 500)
        s = calculate_month(self.account)
        self.assertEqual(s.meal_rate, D("0"))
        self.assertEqual(self.line(s, self.a).shared_cost, D("250.00"))
        self.assertTrue(s.warnings)
        self.assertEqual(s.total_balance, s.cash_in_hand)
