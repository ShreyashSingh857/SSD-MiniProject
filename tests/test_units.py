"""Unit tests that need no running infrastructure: python -m pytest tests"""
import asyncio
import fnmatch
import time

import pytest

import shield_app as shield
import router_app as router
from cdncore.config import RuntimeConfig, parse_edges
from cdncore.metrics import Metrics, percentile
from cdncore.upstream import Fetched, UpstreamError
from content import generator


class MemoryRedis:
    """Small async Redis subset for cache-index tests; expirations are enforced on access/scan."""
    def __init__(self): self.data, self.expiry = {}, {}
    @staticmethod
    def _key(key): return key.decode() if isinstance(key, bytes) else key
    def _live(self, key):
        key = self._key(key)
        if key in self.expiry and self.expiry[key] <= time.time():
            self.data.pop(key, None); self.expiry.pop(key, None)
        return key in self.data
    async def hgetall(self, key): key = self._key(key); return dict(self.data[key]) if self._live(key) else {}
    async def hget(self, key, field): key = self._key(key); return self.data[key].get(field.encode()) if self._live(key) else None
    async def get(self, key): key = self._key(key); return self.data[key] if self._live(key) else None
    async def delete(self, key):
        key = self._key(key)
        existed = self._live(key); self.data.pop(key, None); self.expiry.pop(key, None); return int(existed)
    async def scan_iter(self, match, count=500):
        for key in list(self.data):
            if self._live(key) and fnmatch.fnmatch(key, match): yield key.encode()
    def pipeline(self, transaction=True): return MemoryPipeline(self)


class MemoryPipeline:
    def __init__(self, redis): self.redis, self.ops = redis, []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def __getattr__(self, name):
        def queue(*args, **kwargs): self.ops.append((name, args, kwargs)); return self
        return queue
    async def execute(self):
        result = []
        for name, args, kwargs in self.ops:
            key = args[0]
            if name == "hget": result.append(await self.redis.hget(*args))
            elif name == "delete": result.append(await self.redis.delete(key))
            elif name == "hset":
                mapping = {k.encode(): (v.encode() if isinstance(v, str) else v)
                           for k, v in kwargs["mapping"].items()}
                self.redis.data.setdefault(key, {}).update(mapping); result.append(1)
            elif name == "expire":
                self.redis.expiry[key] = time.time() + args[1]; result.append(True)
            elif name == "set":
                self.redis.data[key] = args[1].encode() if isinstance(args[1], str) else args[1]
                self.redis.expiry[key] = time.time() + kwargs["ex"]; result.append(True)
        self.ops.clear()
        return result


@pytest.fixture
def cache_store(monkeypatch):
    import cdncore.cache as cache
    redis = MemoryRedis()
    monkeypatch.setattr(cache.aioredis, "from_url", lambda *a, **k: redis)
    return cache.CacheStore("redis://fake", "edge"), redis


def test_surrogate_indexes_multiple_tags_and_shared_groups(cache_store):
    store, redis = cache_store
    async def run():
        await store.set("article:101", b"a", "text/plain", "regular", 30, 2,
                        ["article-101", "news", "homepage"])
        await store.set("article:102", b"b", "text/plain", "regular", 30, 2,
                        ["article-102", "news", "homepage"])
        assert await store.purge_surrogate("homepage") == 2
        assert not await store.get("article:101")
        assert not await store.get("article:102")
        assert not any(k.startswith("surrogate:") for k in redis.data)
    asyncio.run(run())


def test_object_delete_and_replacement_clean_old_surrogate_indexes(cache_store):
    store, redis = cache_store
    async def run():
        await store.set("article:1", b"a", "text/plain", "regular", 30, 2, ["old", "shared"])
        await store.set("article:1", b"b", "text/plain", "regular", 30, 2, ["new"])
        assert await store.purge_surrogate("old") == 0
        assert await store.delete("article:1") == 1
        assert not any(k.startswith("surrogate:") for k in redis.data)
    asyncio.run(run())


def test_surrogate_indexes_expire_with_object_grace(cache_store):
    store, redis = cache_store
    async def run():
        await store.set("article:short", b"a", "text/plain", "breaking", 1, 1, ["brief"])
        assert (await store.get("article:short")).fresh
        await asyncio.sleep(1.05)
        assert not (await store.get("article:short")).fresh
        await asyncio.sleep(1.05)
        assert await store.purge_surrogate("brief") == 0
        assert not any(k.startswith("surrogate:") for k in redis.data)
    asyncio.run(run())


def test_manual_object_purge_and_all_node_fanout(monkeypatch):
    calls = {}
    async def fake_all_nodes(method, path, body, names):
        calls.update(method=method, path=path, body=body, names=names)
        return {name: {"node": name, "flushed": 1} for name in names}
    monkeypatch.setattr(router, "all_nodes", fake_all_nodes)
    result = asyncio.run(router.cache_flush({"layer": "all", "object_id": "article:101"}))
    assert calls["body"] == {"object_id": "article:101"}
    assert set(calls["names"]) == set(router.EDGES) | {"shield"}
    assert result["purge_type"] == "object_id" and result["objects_invalidated"] == 4
    assert result["nodes_contacted"] == 4 and not result["failed_nodes"]


def test_purge_reports_partial_node_failures(monkeypatch):
    async def fake_all_nodes(method, path, body, names):
        return {"edge-1": {"flushed": 2}, "edge-2": {"error": "unreachable"}}
    monkeypatch.setattr(router, "all_nodes", fake_all_nodes)
    result = asyncio.run(router.cache_flush({"layer": "edges", "surrogate_key": "homepage"}))
    assert result["purge_type"] == "surrogate_key"
    assert result["objects_invalidated"] == 2
    assert result["nodes_contacted"] == 1
    assert result["failed_nodes"] == {"edge-2": "unreachable"}


def test_publish_still_purges_index(monkeypatch):
    calls = []
    async def fake_node_call(*args): return {"published": {"article_id": "demo"}}
    async def fake_all_nodes(method, path, body, names):
        calls.append((method, path, body, names)); return {name: {"flushed": 1} for name in names}
    monkeypatch.setattr(router, "node_call", fake_node_call)
    monkeypatch.setattr(router, "all_nodes", fake_all_nodes)
    result = asyncio.run(router.publish({}))
    assert result["published"]["article_id"] == "demo"
    assert calls[0][2] == {"object_id": "index:latest"}


def test_parse_edges():
    assert parse_edges("edge-1=http://a:1/, edge-2=http://b:2") == {"edge-1": "http://a:1", "edge-2": "http://b:2"}


def test_ttl_per_class_and_shield_factor():
    cfg = RuntimeConfig()
    assert cfg.ttl_for("breaking") < cfg.ttl_for("popular") < cfg.ttl_for("archive") < cfg.ttl_for("static")
    assert cfg.ttl_for("popular", "shield") == cfg.ttl_for("popular") * 2
    assert cfg.ttl_for("unknown-class") == cfg.ttls["regular"]


def test_config_update_parses_booleans_and_ttls():
    cfg = RuntimeConfig()
    cfg.update({"collapsing": "false", "origin_delay_ms": "250", "ttls": {"breaking": "5"}})
    assert cfg.collapsing is False and cfg.origin_delay_ms == 250 and cfg.ttls["breaking"] == 5
    cfg.update({"collapsing": True})
    assert cfg.collapsing is True


def test_metrics_percentiles():
    m = Metrics()
    for v in range(1, 101):
        m.observe(v)
    snap = m.snapshot()
    assert snap["latency"]["p95_ms"] == pytest.approx(95, abs=1)
    assert percentile([], 95) == 0.0


def test_generator_is_deterministic_and_complete():
    a = generator.generate_articles(20, now=generator.datetime(2026, 1, 1, tzinfo=generator.timezone.utc))
    b = generator.generate_articles(20, now=generator.datetime(2026, 1, 1, tzinfo=generator.timezone.utc))
    assert [x.title for x in a] == [x.title for x in b]
    assert {x.category for x in a} >= {"breaking", "popular"}
    jpeg = generator.render_image(a[0], 160, 90)
    assert jpeg[:2] == b"\xff\xd8"


def test_circuit_breaker_opens_and_recovers():
    br = shield.CircuitBreaker(threshold=3, reset_seconds=0.05)
    for _ in range(3):
        br.before_call()
        br.record_failure()
    assert br.state == "open" and br.trips == 1
    with pytest.raises(shield.CircuitOpen):
        br.before_call()
    time.sleep(0.06)
    br.before_call()  # half-open trial allowed
    with pytest.raises(shield.CircuitOpen):
        br.before_call()  # only one trial at a time
    br.record_success()
    assert br.state == "closed"


@pytest.fixture
def fake_origin(monkeypatch):
    calls = {"n": 0}

    async def slow_fetch(object_id):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        if object_id == "boom":
            raise UpstreamError("origin down")
        return Fetched(b"body", "text/plain", "regular", {})

    monkeypatch.setattr(shield, "fetch_from_origin", slow_fetch)
    shield.state.metrics.reset()
    return calls


def test_request_collapsing_single_origin_fetch(fake_origin):
    shield.state.config.collapsing = True

    async def burst():
        return await asyncio.gather(*(shield.collapsed_fetch("article:x") for _ in range(50)))

    results = asyncio.run(burst())
    assert fake_origin["n"] == 1
    assert sum(1 for _, collapsed in results if collapsed) == 49
    assert all(r.body == b"body" for r, _ in results)
    assert shield.inflight == {}


def test_without_collapsing_every_request_hits_origin(fake_origin):
    shield.state.config.collapsing = False
    try:
        async def burst():
            return await asyncio.gather(*(shield.collapsed_fetch("article:y") for _ in range(10)))

        asyncio.run(burst())
        assert fake_origin["n"] == 10
    finally:
        shield.state.config.collapsing = True


def test_collapsed_waiters_share_failure(fake_origin):
    shield.state.config.collapsing = True

    async def burst():
        return await asyncio.gather(*(shield.collapsed_fetch("boom") for _ in range(5)), return_exceptions=True)

    results = asyncio.run(burst())
    assert fake_origin["n"] == 1
    assert all(isinstance(r, UpstreamError) for r in results)
    assert shield.inflight == {}
