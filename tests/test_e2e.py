"""End-to-end tests against a running deployment.

    CDN_URL=http://localhost:8000 python -m pytest tests/test_e2e.py
"""
import os
import time
import urllib.error
import urllib.request
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

BASE = os.getenv("CDN_URL")
pytestmark = pytest.mark.skipif(not BASE, reason="set CDN_URL to run end-to-end tests")


def call(path, method="GET", body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


@pytest.fixture(autouse=True)
def clean():
    call("/failure/reset", "POST", {})
    call("/cache/flush", "POST", {"layer": "all"})
    yield
    call("/failure/reset", "POST", {})


def test_tiered_cache_path():
    layers = [call("/article/world-013")[1]["x-cache"] for _ in range(5)]
    assert layers[0] == "MISS-ORIGIN"
    assert "HIT-SHIELD" in layers[1:3]          # other edges are filled from the shield
    assert layers[3:] == ["HIT-EDGE", "HIT-EDGE"]


def test_edge_failover():
    call("/failure/edge-1", "POST", {"down": True})
    served = {call("/content/static:homepage.css")[1]["x-served-by"] for _ in range(9)}
    assert "edge-1" not in served and len(served) == 2


def test_shield_bypass_when_shield_down():
    call("/failure/shield", "POST", {"down": True})
    status, headers, _ = call("/content/image:world-013")
    assert status == 200 and headers["x-cache"] == "MISS-ORIGIN-BYPASS"


def test_cached_content_survives_origin_failure():
    for _ in range(3):
        call("/article/world-013")
    call("/failure/origin", "POST", {"down": True})
    for _ in range(3):
        status, headers, _ = call("/article/world-013")
        assert status == 200 and headers["x-cache"] in ("HIT-EDGE", "HIT-SHIELD")
    status, _, _ = call("/article/science-026")  # exists at the origin but never cached -> unavailable
    assert status == 503


def test_publish_purges_index():
    _, _, before = call("/content/index:latest")
    status, _, body = call("/api/publish", "POST", {})
    assert status == 200
    new_id = json.loads(body)["published"]["article_id"]
    _, headers, after = call("/content/index:latest")
    assert headers["x-cache"] == "MISS-ORIGIN"
    assert json.loads(after)["articles"][0]["article_id"] == new_id


def test_cache_lifetime_headers():
    status, headers, _ = call("/content/static:homepage.css")
    assert status == 200
    assert int(headers["x-max-age"]) > 0
    assert 0 <= int(headers["x-cache-ttl"]) <= int(headers["x-max-age"])


def test_minio_delete_invalidates_every_cache_node():
    status, _, body = call("/api/publish", "POST", {})
    assert status == 200
    article_id = json.loads(body)["published"]["article_id"]
    object_id = f"article:{article_id}"
    assert call(f"/content/{object_id}")[0] == 200  # warm edge and shield

    status, _, body = call(f"/api/delete/{object_id}", "POST", {})
    result = json.loads(body)
    assert status == 200
    assert result["source"] == "minio-event"
    assert set(result["purged"]) == {"edge-1", "edge-2", "edge-3", "shield"}
    assert result["propagation_latency_ms"] >= 0
    assert all("propagation_latency_ms" in node for node in result["purged"].values())
    assert call(f"/content/{object_id}")[0] == 404


def test_collapsing_toggle_changes_concurrent_fetches():
    _, _, original = call("/config")
    original_config = json.loads(original)
    object_id = "image:world-013"

    def burst():
        with ThreadPoolExecutor(max_workers=20) as pool:
            return list(pool.map(lambda _: call(f"/content/{object_id}")[0], range(20)))

    try:
        call("/config", "POST", {"collapsing": False})
        call("/cache/flush", "POST", {"layer": "all"})
        call("/metrics/reset", "POST", {})
        assert all(status == 200 for status in burst())
        _, _, off_metrics = call("/metrics")
        off_fetches = json.loads(off_metrics)["nodes"]["shield"]["counters"].get("origin_fetches", 0)

        call("/config", "POST", {"collapsing": True})
        call("/cache/flush", "POST", {"layer": "all"})
        call("/metrics/reset", "POST", {})
        assert all(status == 200 for status in burst())
        _, _, on_metrics = call("/metrics")
        shield = json.loads(on_metrics)["nodes"]["shield"]["counters"]
        assert off_fetches > 1
        assert shield.get("origin_fetches", 0) == 1
        assert shield.get("collapsed", 0) > 0
    finally:
        call("/config", "POST", original_config)
        call("/cache/flush", "POST", {"layer": "all"})


def test_expired_cache_serves_stale_during_origin_failure():
    _, _, original = call("/config")
    original_config = json.loads(original)
    _, _, index_body = call("/content/index:latest")
    index = json.loads(index_body)
    object_ids = ["static:homepage.css", "static:app.js", "static:logo.svg", "index:latest"]
    for article in index["articles"][:4]:
        object_ids.extend((f"thumbnail:{article['article_id']}", f"image:{article['article_id']}"))
    object_ids = object_ids[:12]

    def burst():
        with ThreadPoolExecutor(max_workers=len(object_ids)) as pool:
            return list(pool.map(lambda object_id: call(f"/content/{object_id}"), object_ids))

    try:
        call("/config", "POST", {"ttls": {key: 1 for key in original_config["ttls"]}})
        call("/cache/flush", "POST", {"layer": "all"})
        warmed = burst()
        assert all(status == 200 for status, _, _ in warmed)
        time.sleep(2.3)  # edge TTL is 1s; shield TTL is 2s
        call("/failure/origin", "POST", {"down": True})
        stale = burst()
        assert all(status == 200 and headers["x-cache"].startswith("STALE")
                   for status, headers, _ in stale)
    finally:
        call("/failure/reset", "POST", {})
        call("/config", "POST", original_config)
        call("/cache/flush", "POST", {"layer": "all"})


def test_metrics_and_health_shape():
    call("/article/world-013")
    status, _, body = call("/metrics")
    summary = json.loads(body)["summary"]
    assert status == 200 and summary["total_requests"] >= 1
    for key in ("edge_hit_ratio", "p95_latency_ms", "origin_requests", "collapsing_efficiency"):
        assert key in summary
    assert json.loads(call("/health")[2])["healthy_edges"] == 3
