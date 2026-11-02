"""Read-through cache on Upstash Redis (REST), used for heavy read-only endpoints.

PostgreSQL stays the source of truth; Redis only holds copies of computed responses.

* **Per-user keys.** A key is ``<prefix>:u<user_id>:<name>:m<month_id>:<hash of query>``, and the
  stored envelope repeats the user id, which is checked on read. One user can never be served
  another user's response, even if a key were ever mixed up.
* **Tag invalidation.** Every key is registered in a ``month:<id>`` set and an ``all`` set. A write
  deletes the keys of the affected tag in one Lua call. Model signals (``apps.core.signals``) call
  ``invalidate_on_commit`` so the delete runs after the database transaction commits.
* **Bounded staleness.** Every key has a TTL, so even a missed invalidation heals itself.
* **Fail open.** Any Redis error is logged, opens a circuit breaker for ``REDIS_BREAKER_SECONDS``
  (no request waits on a dead Redis), and the endpoint computes the answer from PostgreSQL. After an
  outage this process flushes the whole prefix before trusting the cache again, because an
  invalidation may have been lost while Redis was unreachable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any, Callable

from django.conf import settings
from django.db import transaction
from rest_framework.response import Response
from rest_framework.utils.encoders import JSONEncoder

log = logging.getLogger("apps.core.cache")

# Deletes every key listed in the given tag sets, then the sets themselves (one round trip).
# ARGV[1] is the "all" set; deleted keys are also removed from it so it does not collect dead entries.
_INVALIDATE_LUA = """
local all = ARGV[1]
local removed = 0
for _, tag in ipairs(KEYS) do
  local members = redis.call('SMEMBERS', tag)
  for i = 1, #members, 500 do
    local chunk = {unpack(members, i, math.min(i + 499, #members))}
    removed = removed + redis.call('DEL', unpack(chunk))
    if tag ~= all then redis.call('SREM', all, unpack(chunk)) end
  end
  redis.call('DEL', tag)
end
return removed
"""

ALL = "all"


def month_tag(month_id: int) -> str:
    return f"month:{month_id}"


class RedisCache:
    def __init__(self):
        self._client = None
        self._lock = threading.Lock()
        self._down_until = 0.0
        self._needs_flush = False
        self.hits = self.misses = self.errors = 0

    # -- configuration --------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(getattr(settings, "REDIS_CACHE_ENABLED", False))

    @property
    def prefix(self) -> str:
        return settings.REDIS_KEY_PREFIX

    def _tag_key(self, tag: str) -> str:
        return f"{self.prefix}:tag:{tag}"

    def _tag_ttl(self) -> int:
        # A tag set must outlive every key registered in it.
        return max(settings.REDIS_TTL_OPEN, settings.REDIS_TTL_CLOSED) + 300

    def _get_client(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    from upstash_redis import Redis

                    client = Redis(
                        url=settings.UPSTASH_REDIS_REST_URL,
                        token=settings.UPSTASH_REDIS_REST_TOKEN,
                        rest_retries=0,  # the SDK default sleeps 3 s and retries; we would rather fail open
                        allow_telemetry=False,
                    )
                    # The SDK builds its httpx client with timeout=None (waits forever); bound it.
                    try:
                        import httpx

                        client._http._client.timeout = httpx.Timeout(settings.REDIS_TIMEOUT)
                    except Exception:  # pragma: no cover - SDK internals changed
                        log.warning("Could not set a Redis timeout; relying on the circuit breaker only.")
                    self._client = client
        return self._client

    # -- failure handling -----------------------------------------------------
    def _usable(self) -> bool:
        if not self.enabled:
            return False
        if time.monotonic() < self._down_until:
            return False
        if self._needs_flush:
            # Redis is reachable again after an outage: drop everything we might have failed to invalidate.
            try:
                self._run_invalidate([self._tag_key(ALL)])
                self._needs_flush = False
            except Exception as exc:
                self._failed("flush after outage", exc)
                return False
        return True

    def _failed(self, what: str, exc: Exception) -> None:
        self.errors += 1
        self._needs_flush = True
        self._down_until = time.monotonic() + settings.REDIS_BREAKER_SECONDS
        # Never log the URL/token; the exception type and message from httpx do not contain them.
        log.warning("Redis %s failed (%s); serving from PostgreSQL for %ss.", what, type(exc).__name__,
                    settings.REDIS_BREAKER_SECONDS)

    # -- primitives -----------------------------------------------------------
    def _run_invalidate(self, tag_keys: list[str]) -> int:
        return int(self._get_client().eval(_INVALIDATE_LUA, keys=tag_keys, args=[self._tag_key(ALL)]) or 0)

    def key(self, user_id: int, name: str, month_id: int | None, params: Any = None) -> str:
        digest = hashlib.sha1(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]
        return f"{self.prefix}:u{user_id}:{name}:m{month_id}:{digest}"

    def get(self, key: str, user_id: int) -> tuple[bool, Any]:
        """Return ``(hit, value)``. Any problem is a miss."""
        if not self._usable():
            return False, None
        try:
            raw = self._get_client().get(key)
        except Exception as exc:
            self._failed("GET", exc)
            return False, None
        if raw is None:
            self.misses += 1
            return False, None
        try:
            envelope = json.loads(raw)
            if envelope["u"] != user_id:  # defence in depth against key mix-ups
                log.error("Cache envelope user mismatch for %s; ignoring it.", key)
                self.misses += 1
                return False, None
            self.hits += 1
            return True, envelope["d"]
        except (ValueError, KeyError, TypeError):
            self.misses += 1
            return False, None

    def set(self, key: str, user_id: int, value: Any, ttl: int, tags: list[str]) -> None:
        if not self._usable():
            return
        try:
            payload = json.dumps({"u": user_id, "d": value}, cls=JSONEncoder, separators=(",", ":"))
            tx = self._get_client().multi()
            tx.set(key, payload, ex=ttl)
            for tag in tags:
                tag_key = self._tag_key(tag)
                tx.sadd(tag_key, key)
                tx.expire(tag_key, self._tag_ttl())
            tx.exec()
        except Exception as exc:
            self._failed("SET", exc)

    def invalidate(self, *tags: str) -> None:
        if not tags or not self.enabled:
            return
        if time.monotonic() < self._down_until:
            self._needs_flush = True  # we cannot reach Redis now; flush when it is back
            return
        try:
            self._run_invalidate([self._tag_key(t) for t in tags])
        except Exception as exc:
            self._failed("invalidate", exc)

    # -- invalidation after commit -------------------------------------------
    _pending = threading.local()

    def invalidate_on_commit(self, *tags: str) -> None:
        """Queue tags and delete them once the surrounding transaction commits.

        Callbacks are registered per call but the first one drains the whole set, so a bulk write
        costs one Redis round trip. A rolled-back transaction leaves tags queued, which only causes
        a harmless extra invalidation later.
        """
        if not self.enabled:
            return
        pending = self._pending.__dict__.setdefault("tags", set())
        pending.update(tags)
        transaction.on_commit(self._flush_pending)

    def _flush_pending(self) -> None:
        pending = self._pending.__dict__.get("tags")
        if not pending:
            return
        tags = list(pending)
        pending.clear()
        self.invalidate(*tags)

    def reset_client(self) -> None:  # tests
        self._client = None
        self._down_until = 0.0
        self._needs_flush = False
        self.hits = self.misses = self.errors = 0


cache = RedisCache()


def cached_response(request, name: str, month_id: int, compute: Callable[[], tuple[Any, bool]], *,
                    params: Any = None) -> Response:
    """Serve the user's cached copy of a month response, computing and storing it on a miss.

    ``compute()`` returns ``(data, month_is_closed)`` and runs the view's normal permission and
    visibility logic, so what is stored is exactly what this user would have received. Closed months
    get the longer TTL. The response carries ``X-Cache: HIT|MISS`` when the cache is enabled.
    """
    user_id = request.user.pk
    key = cache.key(user_id, name, month_id, params if params is not None else dict(request.query_params))
    hit, data = cache.get(key, user_id)
    if hit:
        return Response(data, headers={"X-Cache": "HIT"})
    data, closed = compute()
    ttl = settings.REDIS_TTL_CLOSED if closed else settings.REDIS_TTL_OPEN
    cache.set(key, user_id, data, ttl, [month_tag(month_id), ALL])
    return Response(data, headers={"X-Cache": "MISS"} if cache.enabled else None)
