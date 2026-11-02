from unittest import mock

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.cache import cache
from apps.core.models import MessSettings
from apps.core.tests.fake_redis import FakeRedis
from apps.reports.tests.factories import add_meals, category, expense, make_member, make_month, make_user

CACHE_SETTINGS = dict(REDIS_CACHE_ENABLED=True, REDIS_KEY_PREFIX="t", UPSTASH_REDIS_REST_URL="https://example.invalid",
                      UPSTASH_REDIS_REST_TOKEN="super-secret-token", REDIS_TTL_OPEN=120, REDIS_TTL_CLOSED=600,
                      REDIS_BREAKER_SECONDS=30)


@override_settings(**CACHE_SETTINGS)
class CacheTestCase(TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        cache.reset_client()
        cache._client = self.redis
        self.addCleanup(cache.reset_client)
        self.account = make_month(2026, 4)
        self.manager_user = make_user("manager", User.Role.MANAGER)
        self.alpha_user = make_user("alpha")
        self.bravo_user = make_user("bravo")
        self.manager = make_member("Manager", self.account, user=self.manager_user)
        self.alpha = make_member("Alpha", self.account, user=self.alpha_user)
        self.bravo = make_member("Bravo", self.account, user=self.bravo_user)
        add_meals(self.account, self.alpha, 4)
        add_meals(self.account, self.bravo, 2)
        expense(self.account, 600, purchaser=self.manager)
        self.url = f"/api/v1/months/{self.account.pk}"
        self.settle()

    def settle(self):
        """Forget invalidations queued by fixture setup (TestCase never reaches a real commit)."""
        cache._pending.__dict__.clear()

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def get(self, user, path="summary"):
        return self.client_for(user).get(f"{self.url}/{path}/")

    def write(self, user, method, path, data=None):
        """Perform a write and run the on-commit invalidation (TestCase never commits)."""
        with self.captureOnCommitCallbacks(execute=True):
            return getattr(self.client_for(user), method)(path, data, format="json")

    def total_expenses(self, user=None):
        return self.get(user or self.manager_user).data["total_expenses"]


class HitMissTests(CacheTestCase):
    def test_second_read_is_served_from_redis_without_touching_the_database(self):
        first = self.get(self.manager_user)
        self.assertEqual(first["X-Cache"], "MISS")
        with self.assertNumQueries(0):
            second = self.get(self.manager_user)
        self.assertEqual(second["X-Cache"], "HIT")
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.content, second.content)

    def test_every_cached_endpoint_round_trips_identically(self):
        for path in ("summary", "report", "dashboard"):
            miss, hit = self.get(self.manager_user, path), self.get(self.manager_user, path)
            self.assertEqual((miss["X-Cache"], hit["X-Cache"]), ("MISS", "HIT"), path)
            self.assertEqual(miss.content, hit.content, path)
        grid = f"/api/v1/meals/grid/?month={self.account.pk}"
        a, b = self.client_for(self.manager_user).get(grid), self.client_for(self.manager_user).get(grid)
        self.assertEqual((a["X-Cache"], b["X-Cache"]), ("MISS", "HIT"))
        self.assertEqual(a.content, b.content)

    def test_grid_month_spelling_cannot_dodge_invalidation(self):
        c = self.client_for(self.manager_user)
        padded = f"/api/v1/meals/grid/?month=0{self.account.pk}"
        c.get(padded)
        self.assertEqual(c.get(padded)["X-Cache"], "HIT")
        self.write(self.manager_user, "post", "/api/v1/meals/bulk/", {
            "account": self.account.pk, "date": "2026-04-21",
            "entries": [{"member": self.alpha.pk, "meal_type": "dinner", "quantity": "1"}]})
        self.assertEqual(c.get(padded)["X-Cache"], "MISS")
        self.assertEqual(c.get("/api/v1/meals/grid/").status_code, 400)  # month is still required

    def test_errors_are_never_cached(self):
        r = self.client_for(self.manager_user).get("/api/v1/months/99999/summary/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.redis.store, {})

    def test_entries_expire_after_their_ttl(self):
        self.get(self.manager_user)
        self.redis.advance(119)
        self.assertEqual(self.get(self.manager_user)["X-Cache"], "HIT")
        self.redis.advance(2)
        self.assertEqual(self.get(self.manager_user)["X-Cache"], "MISS")

    def test_closed_months_use_the_longer_ttl(self):
        self.write(self.manager_user, "post", f"/api/v1/months/{self.account.pk}/close/")
        self.get(self.manager_user)
        key = next(k for k in self.redis.store if ":summary:" in k)
        self.assertEqual(self.redis.ttl(key), 600)

    def test_disabled_cache_adds_no_header_and_makes_no_redis_calls(self):
        with override_settings(REDIS_CACHE_ENABLED=False):
            r = self.get(self.manager_user)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("X-Cache", r)
        self.assertEqual(self.redis.calls, 0)


class InvalidationTests(CacheTestCase):
    def test_expense_create_update_delete_refresh_the_summary(self):
        before = self.total_expenses()
        self.assertEqual(self.get(self.manager_user)["X-Cache"], "HIT")

        r = self.write(self.manager_user, "post", "/api/v1/expenses/", {
            "account": self.account.pk, "date": "2026-04-03", "category": category().pk, "amount": "100",
            "purchaser": self.manager.pk, "payment_source": "fund"})
        self.assertEqual(r.status_code, 201, r.data)
        after_create = self.total_expenses()
        self.assertNotEqual(before, after_create)
        self.assertEqual(self.get(self.manager_user)["X-Cache"], "HIT")

        eid = r.data["id"]
        self.write(self.manager_user, "patch", f"/api/v1/expenses/{eid}/", {"amount": "150"})
        after_update = self.total_expenses()
        self.assertNotEqual(after_create, after_update)

        self.write(self.manager_user, "delete", f"/api/v1/expenses/{eid}/")
        self.assertEqual(self.total_expenses(), before)

    def test_deposit_crud_refreshes_reports(self):
        before = self.get(self.manager_user, "report").data["summary"]["total_deposits"]
        r = self.write(self.manager_user, "post", "/api/v1/deposits/", {
            "account": self.account.pk, "member": self.alpha.pk, "date": "2026-04-02", "amount": "500"})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertNotEqual(self.get(self.manager_user, "report").data["summary"]["total_deposits"], before)
        self.write(self.manager_user, "delete", f"/api/v1/deposits/{r.data['id']}/")
        self.assertEqual(self.get(self.manager_user, "report").data["summary"]["total_deposits"], before)

    def test_meal_bulk_save_invalidates_summary_and_grid(self):
        grid = f"/api/v1/meals/grid/?month={self.account.pk}"
        c = self.client_for(self.manager_user)
        meals_before = self.get(self.manager_user).data["total_meals"]
        c.get(grid)
        self.assertEqual(c.get(grid)["X-Cache"], "HIT")
        r = self.write(self.manager_user, "post", "/api/v1/meals/bulk/", {
            "account": self.account.pk, "date": "2026-04-20",
            "entries": [{"member": self.alpha.pk, "meal_type": "lunch", "quantity": "2"}]})
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(self.get(self.manager_user).data["total_meals"], meals_before)
        again = c.get(grid)
        self.assertEqual(again["X-Cache"], "MISS")
        alpha_row = next(row for row in again.data["rows"] if row["member_id"] == self.alpha.pk)
        self.assertEqual(alpha_row["days"]["2026-04-20"], "2")

    def test_approving_an_expense_and_closing_the_month_refresh(self):
        pending = expense(self.account, 300, purchaser=self.alpha, status="pending")
        self.assertEqual(self.get(self.manager_user).data["pending_expense_count"], 1)
        self.write(self.manager_user, "post", f"/api/v1/expenses/{pending.pk}/approve/")
        self.assertEqual(self.get(self.manager_user).data["pending_expense_count"], 0)
        self.assertEqual(self.get(self.manager_user)["X-Cache"], "HIT")
        self.write(self.manager_user, "post", f"{self.url}/close/")
        self.assertEqual(self.get(self.manager_user).data["status"], "closed")
        self.write(self.manager_user, "post", f"{self.url}/reopen/")
        self.assertEqual(self.get(self.manager_user).data["status"], "open")

    def test_changing_a_member_name_invalidates_every_month(self):
        def names():
            return {m["name"] for m in self.get(self.manager_user).data["members"]}

        self.assertIn("Alpha", names())
        self.write(self.manager_user, "patch", f"/api/v1/members/{self.alpha.pk}/", {"name": "Alpha Renamed"})
        self.assertIn("Alpha Renamed", names())

    def test_settings_change_takes_effect_immediately(self):
        self.get(self.alpha_user)
        self.write(self.manager_user, "patch", "/api/v1/settings/", {"members_can_view_all": False})
        r = self.get(self.alpha_user)
        self.assertEqual(r["X-Cache"], "MISS")
        self.assertEqual([m["name"] for m in r.data["members"]], ["Alpha"])

    def test_one_month_write_does_not_flush_another_month(self):
        other = make_month(2026, 5)
        make_member("Delta", other)
        self.settle()
        c = self.client_for(self.manager_user)
        c.get(f"/api/v1/months/{other.pk}/summary/")
        self.write(self.manager_user, "post", "/api/v1/deposits/", {
            "account": self.account.pk, "member": self.alpha.pk, "date": "2026-04-02", "amount": "5"})
        self.assertEqual(c.get(f"/api/v1/months/{other.pk}/summary/")["X-Cache"], "HIT")

    def test_login_updates_do_not_flush_the_cache(self):
        self.get(self.manager_user)
        with self.captureOnCommitCallbacks(execute=True):
            self.alpha_user.save(update_fields=["last_login"])
        self.assertEqual(self.get(self.manager_user)["X-Cache"], "HIT")

    def test_a_user_change_flushes_everything(self):
        self.get(self.manager_user)
        with self.captureOnCommitCallbacks(execute=True):
            self.alpha_user.first_name = "Al"
            self.alpha_user.save()
        self.assertEqual(self.get(self.manager_user)["X-Cache"], "MISS")

    def test_invalidation_waits_for_commit(self):
        self.get(self.manager_user)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            expense(self.account, 10, purchaser=self.manager)
        self.assertEqual(self.get(self.manager_user)["X-Cache"], "HIT")  # not yet committed
        for cb in callbacks:
            cb()
        self.assertEqual(self.get(self.manager_user)["X-Cache"], "MISS")


class IsolationTests(CacheTestCase):
    def setUp(self):
        super().setUp()
        s = MessSettings.load()
        s.members_can_view_all = False
        s.save()

    def test_each_user_gets_their_own_entry_and_never_anothers(self):
        manager = self.get(self.manager_user, "report")
        alpha = self.get(self.alpha_user, "report")
        bravo = self.get(self.bravo_user, "report")
        self.assertEqual({r["X-Cache"] for r in (manager, alpha, bravo)}, {"MISS"})
        self.assertEqual(len(manager.data["summary"]["members"]), 3)
        self.assertEqual([m["name"] for m in alpha.data["summary"]["members"]], ["Alpha"])
        self.assertEqual([m["name"] for m in bravo.data["summary"]["members"]], ["Bravo"])
        # Second round: every user hits their own copy.
        for user, first in ((self.manager_user, manager), (self.alpha_user, alpha), (self.bravo_user, bravo)):
            again = self.get(user, "report")
            self.assertEqual(again["X-Cache"], "HIT")
            self.assertEqual(again.content, first.content)
        keys = [k for k in self.redis.store if ":report:" in k]
        self.assertEqual(len(keys), 3)
        for user in (self.manager_user, self.alpha_user, self.bravo_user):
            self.assertEqual(len([k for k in keys if f":u{user.pk}:" in k]), 1)

    def test_a_mismatched_envelope_is_rejected(self):
        self.get(self.manager_user, "report")
        manager_key = next(k for k in self.redis.store if f":u{self.manager_user.pk}:report:" in k)
        alpha_key = manager_key.replace(f":u{self.manager_user.pk}:", f":u{self.alpha_user.pk}:")
        self.redis.store[alpha_key] = self.redis.store[manager_key]  # simulate a mix-up / poisoning
        r = self.get(self.alpha_user, "report")
        self.assertEqual(r["X-Cache"], "MISS")
        self.assertEqual([m["name"] for m in r.data["summary"]["members"]], ["Alpha"])

    def test_dashboard_is_per_user(self):
        a, b = self.get(self.alpha_user, "dashboard"), self.get(self.bravo_user, "dashboard")
        self.assertEqual(a.data["my_balance"]["name"], "Alpha")
        self.assertEqual(b.data["my_balance"]["name"], "Bravo")
        self.assertEqual(self.get(self.alpha_user, "dashboard").data["my_balance"]["name"], "Alpha")

    def test_credentials_never_appear_in_responses_or_cache(self):
        r = self.get(self.manager_user, "report")
        blob = r.content.decode() + str(dict(r.items())) + "".join(self.redis.store.values())
        self.assertNotIn("super-secret-token", blob)
        self.assertNotIn("example.invalid", blob)


class RedisOutageTests(CacheTestCase):
    def test_requests_still_work_and_the_breaker_stops_further_calls(self):
        self.redis.fail = True
        r = self.get(self.manager_user)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["total_meals"], "6")
        calls = self.redis.calls
        for _ in range(3):
            self.assertEqual(self.get(self.manager_user).status_code, 200)
        self.assertEqual(self.redis.calls, calls, "no Redis call while the breaker is open")

    def test_writes_succeed_while_redis_is_down(self):
        self.redis.fail = True
        r = self.write(self.manager_user, "post", "/api/v1/deposits/", {
            "account": self.account.pk, "member": self.alpha.pk, "date": "2026-04-02", "amount": "50"})
        self.assertEqual(r.status_code, 201)

    def test_nothing_stale_survives_an_outage(self):
        stale = self.get(self.manager_user).data["total_expenses"]  # cached
        self.redis.fail = True
        r = self.write(self.manager_user, "post", "/api/v1/expenses/", {
            "account": self.account.pk, "date": "2026-04-03", "category": category().pk, "amount": "400",
            "purchaser": self.manager.pk, "payment_source": "fund"})
        self.assertEqual(r.status_code, 201)  # the invalidation was lost
        self.redis.fail = False
        # Breaker still open: the database answers, fresh.
        self.assertNotEqual(self.get(self.manager_user).data["total_expenses"], stale)
        with mock.patch("apps.core.cache.time.monotonic", return_value=10 ** 9):
            fresh = self.get(self.manager_user)
        self.assertNotEqual(fresh.data["total_expenses"], stale)
        self.assertEqual(fresh["X-Cache"], "MISS")  # the flush after the outage dropped the old entry

    def test_recovers_after_the_breaker_window(self):
        self.redis.fail = True
        self.get(self.manager_user)
        self.redis.fail = False
        self.assertEqual(self.get(self.manager_user)["X-Cache"], "MISS")  # breaker still open, nothing cached
        with mock.patch("apps.core.cache.time.monotonic", return_value=10 ** 9):
            self.assertEqual(self.get(self.manager_user)["X-Cache"], "MISS")
            self.assertEqual(self.get(self.manager_user)["X-Cache"], "HIT")

    def test_corrupt_cache_values_are_treated_as_misses(self):
        self.get(self.manager_user)
        for k in list(self.redis.store):
            if ":summary:" in k:
                self.redis.store[k] = "{not json"
        r = self.get(self.manager_user)
        self.assertEqual((r.status_code, r["X-Cache"]), (200, "MISS"))


class ClientConfigurationTests(TestCase):
    @override_settings(**CACHE_SETTINGS, REDIS_TIMEOUT=1.5)
    def test_sdk_client_has_a_timeout_no_retries_and_no_telemetry(self):
        cache.reset_client()
        self.addCleanup(cache.reset_client)
        client = cache._get_client()
        self.assertEqual(client._http._client.timeout.read, 1.5)
        self.assertEqual(client._http._retries, 0)
        self.assertFalse(client._allow_telemetry)

    def test_cache_is_off_when_testing(self):
        self.assertFalse(cache.enabled)  # manage.py test never enables the real cache
