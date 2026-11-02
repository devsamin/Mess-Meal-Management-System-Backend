"""In-memory stand-in for ``upstash_redis.Redis`` covering only what the cache uses."""

from apps.core import cache as cache_module


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.sets: dict[str, set] = {}
        self.expires: dict[str, float] = {}
        self.now = 1000.0
        self.fail = False
        self.calls = 0

    # -- helpers ---------------------------------------------------------------
    def _check(self):
        self.calls += 1
        if self.fail:
            raise ConnectionError("redis down")
        for k in [k for k, t in self.expires.items() if t <= self.now]:
            self.expires.pop(k)
            self.store.pop(k, None)
            self.sets.pop(k, None)

    def advance(self, seconds: float):
        self.now += seconds

    # -- commands ----------------------------------------------------------------
    def get(self, key):
        self._check()
        return self.store.get(key)

    def ttl(self, key):
        self._check()
        return int(self.expires[key] - self.now) if key in self.expires else -1

    def multi(self):
        return _Tx(self)

    def eval(self, script, keys=None, args=None):
        self._check()
        assert script == cache_module._INVALIDATE_LUA, "unexpected script"
        all_key, removed = args[0], 0
        for tag in keys:
            for member in list(self.sets.get(tag, ())):
                removed += int(self.store.pop(member, None) is not None)
                self.expires.pop(member, None)
                if tag != all_key:
                    self.sets.get(all_key, set()).discard(member)
            self.sets.pop(tag, None)
            self.expires.pop(tag, None)
        return removed


class _Tx:
    def __init__(self, redis):
        self.r, self.ops = redis, []

    def set(self, key, value, ex=None):
        self.ops.append(("set", key, value, ex))

    def sadd(self, key, member):
        self.ops.append(("sadd", key, member))

    def expire(self, key, seconds):
        self.ops.append(("expire", key, seconds))

    def exec(self):
        self.r._check()
        for op, key, *rest in self.ops:
            if op == "set":
                self.r.store[key] = rest[0]
                if rest[1]:
                    self.r.expires[key] = self.r.now + rest[1]
            elif op == "sadd":
                self.r.sets.setdefault(key, set()).add(rest[0])
            elif op == "expire":
                self.r.expires[key] = self.r.now + rest[0]
        return []
