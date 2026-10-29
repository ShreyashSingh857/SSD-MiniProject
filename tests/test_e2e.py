"""End-to-end tests against a running deployment.

    CDN_URL=http://localhost:8000 python -m pytest tests/test_e2e.py
"""
import os
import time
import urllib.error
import urllib.request
import json

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


def test_metrics_and_health_shape():
    call("/article/world-013")
    status, _, body = call("/metrics")
    summary = json.loads(body)["summary"]
    assert status == 200 and summary["total_requests"] >= 1
    for key in ("edge_hit_ratio", "p95_latency_ms", "origin_requests", "collapsing_efficiency"):
        assert key in summary
    assert json.loads(call("/health")[2])["healthy_edges"] == 3
