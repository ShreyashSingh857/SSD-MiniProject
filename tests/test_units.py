"""Unit tests that need no running infrastructure: python -m pytest tests"""
import asyncio
import time

import pytest

from cdn import shield
from cdn.config import RuntimeConfig, parse_edges
from cdn.content import generator
from cdn.metrics import Metrics, percentile
from cdn.upstream import Fetched, UpstreamError


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
