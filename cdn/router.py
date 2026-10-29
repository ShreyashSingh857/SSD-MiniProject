"""Request router / load balancer: the public entry point of the CDN.

Round-robins requests over healthy edges (with failover), aggregates metrics from every node,
exposes the control API (flush, failures, config, publish, benchmarks) and serves the
newspaper site and the monitoring dashboard.
"""
from __future__ import annotations

import asyncio
import itertools
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import aiohttp
from fastapi import Body, FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response

from . import bench, config
from .config import RuntimeConfig
from .metrics import Metrics
from .upstream import get_raw, new_session

WEB_DIR = Path(__file__).parent / "web"
EDGES = config.parse_edges()
NODES = {**EDGES, "shield": config.SHIELD_URL.rstrip("/"), "origin": config.ORIGIN_URL.rstrip("/")}
PASS_HEADERS = ("content-type", "x-cache", "x-served-by", "x-content-class", "age", "cache-control")

health: dict[str, dict] = {name: {"up": True, "checked_at": None, "latency_ms": None} for name in NODES}
rr = itertools.count()
metrics = Metrics()
request_log: deque[dict] = deque(maxlen=200)
runtime = RuntimeConfig()
benchmark_state: dict = {"running": False, "scenario": None, "started_at": None, "results": {}, "error": None}
http: aiohttp.ClientSession | None = None
NET_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError)


async def check_node(name: str, url: str) -> None:
    started = time.perf_counter()
    try:
        status, _, _ = await get_raw(http, f"{url}/health", 1.0)
        up = status == 200
    except NET_ERRORS:
        up = False
    was_up = health[name]["up"]
    health[name] = {"up": up, "checked_at": time.time(),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    if was_up and not up:
        metrics.inc("health_down_events")


async def health_loop() -> None:
    while True:
        await asyncio.gather(*(check_node(n, u) for n, u in NODES.items()), return_exceptions=True)
        await asyncio.sleep(1.0)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global http
    http = new_session(limit=500)
    task = asyncio.create_task(health_loop())
    yield
    task.cancel()
    await http.close()


app = FastAPI(title="Edge CDN with Origin Shielding", lifespan=lifespan)


def edge_order() -> list[str]:
    """Round-robin over healthy edges; the remaining edges follow as failover candidates."""
    names = list(EDGES)
    healthy = [n for n in names if health[n]["up"]] or names
    start = next(rr) % len(healthy)
    ordered = healthy[start:] + healthy[:start]
    return ordered + [n for n in names if n not in ordered]


def mark_down(edge: str) -> None:
    health[edge] = {**health[edge], "up": False, "checked_at": time.time()}
    metrics.inc("failovers")


async def route(object_id: str) -> Response:
    started = time.perf_counter()
    request_id = uuid.uuid4().hex[:10]
    resp = None
    attempts = 0
    for edge in edge_order():
        attempts += 1
        try:
            resp = await get_raw(http, f"{EDGES[edge]}/content/{quote(object_id, safe=':')}",
                                 runtime.upstream_timeout_s + 3)
        except aiohttp.ConnectionTimeoutError:
            mark_down(edge)
            resp = None
            continue
        except asyncio.TimeoutError:
            # A slow edge is usually waiting on a slow origin; failing over would only repeat the wait.
            latency = (time.perf_counter() - started) * 1000
            metrics.inc("requests")
            metrics.inc("layer:TIMEOUT")
            metrics.inc("errors")
            metrics.observe(latency)
            return JSONResponse({"error": f"{edge} timed out"}, status_code=504,
                                headers={"X-Request-Id": request_id, "X-Cache": "TIMEOUT", "X-Served-By": edge})
        except aiohttp.ClientError:
            mark_down(edge)
            resp = None
            continue
        if resp[0] == 503 and resp[1].get("x-node-down") == "1":
            mark_down(edge)
            resp = None
            continue
        break
    latency = (time.perf_counter() - started) * 1000
    metrics.inc("requests")
    metrics.observe(latency)
    if resp is None:
        metrics.inc("layer:NO-EDGE")
        metrics.inc("errors")
        return JSONResponse({"error": "no edge node available"}, status_code=503,
                            headers={"X-Request-Id": request_id, "X-Cache": "NO-EDGE"})
    status, resp_headers, body = resp
    layer = resp_headers.get("x-cache", "ERROR")
    metrics.inc(f"layer:{layer}")
    if status >= 500:
        metrics.inc("errors")
    request_log.appendleft({
        "request_id": request_id, "object_id": object_id, "edge_id": resp_headers.get("x-served-by"),
        "cache_layer": layer, "status": status, "latency_ms": round(latency, 2),
        "attempts": attempts, "ts": time.time(),
    })
    headers = {k: v for k, v in resp_headers.items() if k in PASS_HEADERS}
    headers.update({"X-Request-Id": request_id, "X-Latency-ms": f"{latency:.1f}",
                    "X-Edge-Attempts": str(attempts)})
    return Response(body, status_code=status, headers=headers)


# ---------------------------------------------------------------- content API

@app.get("/article/{article_id}")
async def get_article(article_id: str):
    return await route(f"article:{article_id}")


@app.get("/content/{object_id}")
async def get_content(object_id: str):
    return await route(object_id)


@app.get("/direct/{object_id}")
async def get_direct(object_id: str):
    """Bypass the CDN entirely (baseline for the direct-origin benchmark)."""
    started = time.perf_counter()
    try:
        status, headers, body = await get_raw(http, f"{NODES['origin']}/object/{quote(object_id, safe=':')}",
                                              runtime.upstream_timeout_s + 1)
    except NET_ERRORS as exc:
        return JSONResponse({"error": f"origin unreachable: {exc.__class__.__name__}"}, status_code=503)
    latency = (time.perf_counter() - started) * 1000
    return Response(body, status_code=status, headers={
        "content-type": headers.get("content-type", "application/octet-stream"),
        "X-Cache": "DIRECT-ORIGIN", "X-Latency-ms": f"{latency:.1f}",
    })


# ---------------------------------------------------------------- control plane helpers

async def node_call(name: str, method: str, path: str, payload: dict | None = None) -> dict:
    try:
        async with http.request(method, f"{NODES[name]}{path}", json=payload,
                                timeout=aiohttp.ClientTimeout(total=5)) as resp:
            return await resp.json(content_type=None)
    except Exception as exc:
        return {"node": name, "error": f"unreachable: {exc.__class__.__name__}"}


async def all_nodes(method: str, path: str, payload: dict | None = None,
                    names: list[str] | None = None) -> dict[str, dict]:
    names = names or list(NODES)
    results = await asyncio.gather(*(node_call(n, method, path, payload) for n in names))
    return dict(zip(names, results))


def ratio(part: float, whole: float) -> float:
    return round(part / whole, 4) if whole else 0.0


def summarize(router_snap: dict, nodes: dict[str, dict]) -> dict:
    c = router_snap["counters"]
    layers = {k.split(":", 1)[1]: v for k, v in c.items() if k.startswith("layer:")}
    total = c.get("requests", 0)
    edge_hits = layers.get("HIT-EDGE", 0)
    shield_hits = layers.get("HIT-SHIELD", 0)
    stale = sum(v for k, v in layers.items() if k.startswith("STALE"))
    shield = nodes.get("shield", {}).get("counters", {})
    origin = nodes.get("origin", {}).get("counters", {})
    collapsed = shield.get("collapsed", 0)
    shield_fetches = shield.get("origin_fetches", 0)
    edge_misses = total - edge_hits
    return {
        "total_requests": total,
        "layers": layers,
        "edge_hit_ratio": ratio(edge_hits, total),
        "cache_hit_ratio": ratio(edge_hits + shield_hits + stale, total),
        "shield_hit_ratio": ratio(shield_hits, edge_misses),
        "miss_rate": ratio(edge_misses, total),
        "avg_latency_ms": router_snap["latency"]["avg_ms"],
        "p50_latency_ms": router_snap["latency"]["p50_ms"],
        "p95_latency_ms": router_snap["latency"]["p95_ms"],
        "p99_latency_ms": router_snap["latency"]["p99_ms"],
        "throughput_rps": router_snap["throughput_rps"],
        "origin_requests": origin.get("origin_requests", 0),
        "shield_origin_fetches": shield_fetches,
        "collapsed_requests": collapsed,
        "collapsing_efficiency": ratio(collapsed, collapsed + shield_fetches),
        "shield_bypasses": sum(n.get("counters", {}).get("shield_bypass", 0)
                               for name, n in nodes.items() if name in EDGES),
        "stale_served": stale,
        "failovers": c.get("failovers", 0),
        "errors": c.get("errors", 0),
    }


# ---------------------------------------------------------------- monitoring API

@app.get("/metrics")
async def get_metrics():
    nodes = await all_nodes("GET", "/admin/metrics")
    snap = metrics.snapshot()
    return {"summary": summarize(snap, nodes), "router": snap, "nodes": nodes,
            "health": health, "generated_at": time.time()}


@app.post("/metrics/reset")
async def reset_metrics():
    metrics.reset()
    request_log.clear()
    return {"reset": await all_nodes("POST", "/admin/metrics/reset")}


@app.get("/health")
async def get_health():
    components = {name: {"up": h["up"], "latency_ms": h["latency_ms"]} for name, h in health.items()}
    healthy_edges = sum(1 for n in EDGES if health[n]["up"])
    status = "ok" if healthy_edges and all(h["up"] for h in health.values()) else (
        "degraded" if healthy_edges else "down")
    return {"status": status, "router": "ok", "healthy_edges": healthy_edges, "components": components}


@app.get("/cache/status")
async def cache_status():
    return await all_nodes("GET", "/admin/status")


@app.post("/cache/flush")
async def cache_flush(payload: dict = Body(default={})):
    """Flush caches. layer: all | edges | shield | <edge id>; optional object_id purges one object."""
    layer = payload.get("layer", "all")
    if layer == "all":
        names = list(EDGES) + ["shield"]
    elif layer == "edges":
        names = list(EDGES)
    elif layer in EDGES or layer == "shield":
        names = [layer]
    else:
        return JSONResponse({"error": f"unknown layer {layer!r}"}, status_code=400)
    body = {"object_id": payload["object_id"]} if payload.get("object_id") else {}
    return {"layer": layer, "result": await all_nodes("POST", "/admin/flush", body, names)}


@app.get("/api/requests")
async def recent_requests(limit: int = 50):
    return list(itertools.islice(request_log, max(1, min(limit, 200))))


# ---------------------------------------------------------------- failure simulation & config

@app.post("/failure/{component}")
async def set_failure(component: str, payload: dict = Body(default={})):
    """Simulate a component failure. Body: {"down": true|false}; omitted toggles."""
    if component == "reset":
        result = await all_nodes("POST", "/admin/failure", {"down": False})
        await all_nodes("POST", "/admin/shield-recovered", names=list(EDGES))
        return {"restored": result}
    if component not in NODES:
        return JSONResponse({"error": f"unknown component {component!r}",
                             "components": list(NODES)}, status_code=404)
    result = await node_call(component, "POST", "/admin/failure", payload)
    if component == "shield" and result.get("down") is False:
        await all_nodes("POST", "/admin/shield-recovered", names=list(EDGES))
    await check_node(component, NODES[component])
    return result


@app.get("/config")
async def get_config():
    return runtime.to_dict()


@app.post("/config")
async def set_config(payload: dict = Body(default={})):
    runtime.update(payload)
    await all_nodes("POST", "/admin/config", runtime.to_dict())
    return runtime.to_dict()


@app.post("/api/publish")
async def publish(payload: dict = Body(default={})):
    """Publish breaking news at the origin, then purge the stale homepage index from every cache."""
    result = await node_call("origin", "POST", "/admin/publish", payload)
    if "error" in result:
        return JSONResponse(result, status_code=503)
    purge = await all_nodes("POST", "/admin/flush", {"object_id": "index:latest"}, list(EDGES) + ["shield"])
    return {**result, "purged": purge}


# ---------------------------------------------------------------- benchmarks

async def run_benchmark(scenario: str, options: dict) -> None:
    benchmark_state.update(running=True, scenario=scenario, started_at=time.time(), error=None)
    try:
        base = f"http://127.0.0.1:{config.PORT}"
        result = await bench.run(base, scenario, **options)
        benchmark_state["results"].update(result)
    except Exception as exc:
        benchmark_state["error"] = f"{exc.__class__.__name__}: {exc}"
    finally:
        benchmark_state["running"] = False


@app.post("/api/benchmark")
async def start_benchmark(payload: dict = Body(default={})):
    scenario = payload.get("scenario", "all")
    if scenario not in bench.SCENARIOS and scenario != "all":
        return JSONResponse({"error": f"unknown scenario {scenario!r}",
                             "scenarios": list(bench.SCENARIOS) + ["all"]}, status_code=400)
    if benchmark_state["running"]:
        return JSONResponse({"error": "a benchmark is already running"}, status_code=409)
    options = {k: int(v) for k, v in payload.items() if k in ("requests", "concurrency")}
    asyncio.create_task(run_benchmark(scenario, options))
    return {"started": scenario}


@app.get("/api/benchmark")
async def get_benchmark():
    return benchmark_state


# ---------------------------------------------------------------- web pages

@app.get("/", include_in_schema=False)
async def newspaper():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    return FileResponse(WEB_DIR / "dashboard.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return await route("static:logo.svg")
