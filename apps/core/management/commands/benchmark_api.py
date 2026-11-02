"""Read-only API benchmark: database query count and latency per endpoint.

    python manage.py benchmark_api                      # table for the manager and a member
    python manage.py benchmark_api --runs 10 --json out.json
    python manage.py benchmark_api --snapshot before/   # save every response body for diffing

Only GET requests are sent, as users that already exist, so it is safe to run
against any database. Each endpoint is called ``--runs`` times: the first call
is reported separately ("cold"), the rest as a median ("warm"). With the Redis
cache enabled the warm calls show cache hits.
"""

import re
import statistics
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, reset_queries
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.months.models import MonthlyAccount


def endpoints(month_id: int, day: str) -> list[tuple[str, str]]:
    return [
        ("auth/me", "/api/v1/auth/me/"),
        ("settings", "/api/v1/settings/"),
        ("months", "/api/v1/months/"),
        ("members", "/api/v1/members/"),
        ("expense-categories", "/api/v1/expense-categories/"),
        ("month summary", f"/api/v1/months/{month_id}/summary/"),
        ("month report", f"/api/v1/months/{month_id}/report/"),
        ("month dashboard", f"/api/v1/months/{month_id}/dashboard/"),
        ("meals grid", f"/api/v1/meals/grid/?month={month_id}"),
        ("meals day", f"/api/v1/meals/day/?month={month_id}&date={day}"),
        ("expenses (page 1)", f"/api/v1/expenses/?month={month_id}&page_size=25&ordering=-date"),
        ("expenses (search)", f"/api/v1/expenses/?month={month_id}&search=rice&page_size=25"),
        ("deposits", f"/api/v1/deposits/?month={month_id}&page_size=25"),
        ("extra-bills", f"/api/v1/extra-bills/?month={month_id}&page_size=100"),
        ("activity", f"/api/v1/activity/?account={month_id}"),
        ("month members", f"/api/v1/months/{month_id}/members/"),
    ]


class Command(BaseCommand):
    help = "Measure query counts and latency of the main read endpoints (GET only)."

    def add_arguments(self, parser):
        parser.add_argument("--runs", type=int, default=5)
        parser.add_argument("--month", type=int, help="Month id (default: the open month with the most data).")
        parser.add_argument("--manager", help="Username of a manager (default: first manager).")
        parser.add_argument("--member", help="Username of a regular member (default: first member login).")
        parser.add_argument("--json", help="Write the results to this file.")
        parser.add_argument("--snapshot", help="Directory to save response bodies (for before/after diffs).")

    def handle(self, *args, runs, month, manager, member, json: str | None, snapshot, **opts):
        import json as jsonlib

        account = MonthlyAccount.objects.filter(pk=month).first() if month else (
            MonthlyAccount.objects.filter(status="open").order_by("-id").first()
            or MonthlyAccount.objects.order_by("-id").first()
        )
        if account is None:
            raise CommandError("No month exists. Seed some data first.")
        users = {}
        mgr = User.objects.filter(username=manager).first() if manager else (
            User.objects.filter(role="manager", is_active=True).order_by("id").first())
        mem = User.objects.filter(username=member).first() if member else (
            User.objects.filter(role="member", is_active=True, member__isnull=False).order_by("id").first())
        if mgr:
            users["manager"] = mgr
        if mem:
            users["member"] = mem
        if not users:
            raise CommandError("No users found.")

        day = min(max(timezone.localdate(), account.start_date), account.end_date).isoformat()
        results = []
        out_dir = Path(snapshot) if snapshot else None
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

        self.stdout.write(f"Month: {account.label} (id={account.pk}, {account.status}); runs per endpoint: {runs}\n")
        for role, user in users.items():
            client = APIClient(SERVER_NAME="localhost")
            client.force_authenticate(user=user)
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n{role} ({user.username})"))
            self.stdout.write(f"{'endpoint':<22}{'status':>7}{'q cold':>9}{'q warm':>9}{'cold ms':>10}{'warm ms':>10}{'KB':>8}")
            for name, url in endpoints(account.pk, day):
                if role == "member" and name == "users":
                    continue
                times, queries, status, size, body, differs = [], 0, 0, 0, None, False
                for i in range(runs):
                    reset_queries()
                    with CaptureQueriesContext(connection) as ctx:
                        t0 = time.perf_counter()
                        resp = client.get(url)
                        times.append((time.perf_counter() - t0) * 1000)
                    if i == 0:
                        queries, status = len(ctx), resp.status_code
                        body = resp.content
                        size = len(body) / 1024
                    elif resp.content != body:
                        differs = True  # a cache hit must be byte-identical to the computed response
                warm = statistics.median(times[1:]) if len(times) > 1 else times[0]
                # Queries on the *last* run show the steady state (cache hit => few or none).
                results.append({"role": role, "endpoint": name, "status": status, "queries_cold": queries,
                                "queries_warm": len(ctx), "cold_ms": round(times[0], 1),
                                "warm_ms": round(warm, 1), "kb": round(size, 1)})
                flag = "  !! RESPONSE DIFFERS BETWEEN RUNS" if differs else ""
                self.stdout.write(f"{name:<22}{status:>7}{queries:>9}{len(ctx):>9}{times[0]:>10.1f}{warm:>10.1f}{size:>8.1f}{flag}")
                if out_dir and body is not None:
                    (out_dir / f"{role}__{re.sub(r'[^A-Za-z0-9]+', '_', name).strip('_')}.json").write_bytes(
                        jsonlib.dumps(jsonlib.loads(body or b"null"), sort_keys=True, indent=1).encode())
        if json:
            Path(json).write_text(jsonlib.dumps(results, indent=1), encoding="utf-8")
