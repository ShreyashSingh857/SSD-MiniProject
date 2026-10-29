"""Load generator and benchmark scenarios (PDF section 17 comparisons).

Runs against the router's public API, either from the dashboard ("Run benchmark") or the CLI:

    python -m cdn.bench --url http://localhost:8000 --scenario all
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from collections import Counter

import aiohttp

from .metrics import percentile

HIT_LAYERS = ("HIT-EDGE", "HIT-SHIELD", "STALE-EDGE", "STALE-SHIELD")


def summarize(samples: list[tuple[float, int, str]], wall: float) -> dict:
    latencies = [s[0] for s in samples]
    ok = sum(1 for s in samples if 200 <= s[1] < 400)
    layers = Counter(s[2] for s in samples)
    n = len(samples) or 1
    return {
        "requests": len(samples),
        "success_rate": round(ok / n, 4),
        "avg_ms": round(sum(latencies) / n, 2),
        "p50_ms": round(percentile(latencies, 50), 2),
        "p95_ms": round(percentile(latencies, 95), 2),
        "p99_ms": round(percentile(latencies, 99), 2),
        "throughput_rps": round(len(samples) / wall, 1) if wall else 0.0,
        "cache_hit_ratio": round(sum(layers[layer] for layer in HIT_LAYERS) / n, 4),
        "edge_hit_ratio": round(layers["HIT-EDGE"] / n, 4),
        "layers": dict(layers),
        "wall_s": round(wall, 2),
    }


class Control:
    def __init__(self, client: aiohttp.ClientSession):
        self.c = client

    async def post(self, path: str, body: dict | None = None) -> dict:
        async with self.c.post(path, json=body or {}) as resp:
            return await resp.json(content_type=None)

    async def get_json(self, path: str) -> dict:
        async with self.c.get(path) as resp:
            return await resp.json(content_type=None)

    async def fire(self, paths: list[str], concurrency: int) -> dict:
        sem = asyncio.Semaphore(concurrency)

        async def one(path: str) -> tuple[float, int, str]:
            async with sem:
                started = time.perf_counter()
                try:
                    async with self.c.get(path) as resp:
                        await resp.read()
                        status, layer = resp.status, resp.headers.get("x-cache", "?")
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    status, layer = 0, "CLIENT-ERROR"
                return (time.perf_counter() - started) * 1000, status, layer

        started = time.perf_counter()
        samples = await asyncio.gather(*(one(p) for p in paths))
        return summarize(list(samples), time.perf_counter() - started)

    async def flush(self, layer: str = "all") -> None:
        await self.post("/cache/flush", {"layer": layer})

    async def reset(self) -> None:
        await self.post("/metrics/reset")

    async def configure(self, **kwargs) -> None:
        await self.post("/config", kwargs)

    async def fail(self, component: str, down: bool) -> None:
        await self.post(f"/failure/{component}", {"down": down})
        await asyncio.sleep(1.2)  # let the router's health checker notice

    async def server_summary(self) -> dict:
        summary = (await self.get_json("/metrics"))["summary"]
        keep = ("origin_requests", "shield_origin_fetches", "collapsed_requests", "collapsing_efficiency",
                "shield_bypasses", "stale_served", "failovers", "edge_hit_ratio", "cache_hit_ratio")
        return {k: summary[k] for k in keep}

    async def objects(self) -> list[str]:
        index = await self.get_json("/content/index:latest")
        ids = []
        for art in index["articles"]:
            ids += [f"article:{art['article_id']}", f"thumbnail:{art['article_id']}", f"image:{art['article_id']}"]
        return ids + ["static:homepage.css", "static:app.js", "static:logo.svg", "index:latest"]


def zipf_paths(objects: list[str], count: int, s: float = 1.1, seed: int = 7) -> list[str]:
    """Popularity-skewed workload: a few hot objects (breaking news) receive most requests."""
    rng = random.Random(seed)
    weights = [1 / (rank + 1) ** s for rank in range(len(objects))]
    return [f"/content/{o}" for o in rng.choices(objects, weights=weights, k=count)]


async def workload(ctl: Control, requests: int = 1500, concurrency: int = 40) -> dict:
    await ctl.flush()
    objects = await ctl.objects()
    await ctl.reset()
    result = await ctl.fire(zipf_paths(objects, requests), concurrency)
    return {"description": f"Zipf-distributed newspaper traffic, {requests} requests, cold caches",
            "client": result, "server": await ctl.server_summary()}


async def direct_vs_edge(ctl: Control, requests: int = 300, concurrency: int = 20) -> dict:
    hot = [o for o in await ctl.objects() if o.startswith("article:")][:10]
    paths = [hot[i % len(hot)] for i in range(requests)]
    await ctl.flush()
    await ctl.fire([f"/content/{o}" for o in hot * 3], concurrency)  # warm every edge
    direct = await ctl.fire([f"/direct/{o}" for o in paths], concurrency)
    cdn = await ctl.fire([f"/content/{o}" for o in paths], concurrency)
    return {"description": "Same hot articles fetched directly from origin vs through warm edge caches",
            "direct_origin": direct, "edge_cache": cdn,
            "speedup_avg": round(direct["avg_ms"] / max(cdn["avg_ms"], 0.01), 1)}


async def shield_vs_origin(ctl: Control, requests: int = 30, concurrency: int = 10) -> dict:
    objs = [o for o in await ctl.objects() if o.startswith(("article:", "thumbnail:"))][:requests]
    paths = [f"/content/{o}" for o in objs]
    await ctl.flush()
    origin = await ctl.fire(paths, concurrency)
    await ctl.flush("edges")
    shield = await ctl.fire(paths, concurrency)
    edge = await ctl.fire(paths, concurrency)
    return {"description": "Edge miss served by origin vs by the shield vs edge hit",
            "origin_fetch": origin, "shield_hit": shield, "edge_hit": edge}


async def collapsing(ctl: Control, requests: int = 200, concurrency: int = 200) -> dict:
    target = next(o for o in await ctl.objects() if o.startswith("image:"))
    out = {"description": f"{requests} simultaneous requests for one uncached object ({target})"}
    await ctl.configure(upstream_timeout_s=30.0)
    for enabled in (False, True):
        await ctl.configure(collapsing=enabled)
        await ctl.flush()
        await ctl.reset()
        client = await ctl.fire([f"/content/{target}"] * requests, concurrency)
        out["with_collapsing" if enabled else "without_collapsing"] = {
            "client": client, "server": await ctl.server_summary()}
    return out


async def delayed_origin(ctl: Control, requests: int = 20, concurrency: int = 10) -> dict:
    objs = [o for o in await ctl.objects() if o.startswith("article:")][:requests]
    paths = [f"/content/{o}" for o in objs]
    out = {"description": "Cold (origin) vs warm (edge) latency with a normal vs artificially slow origin"}
    for delay in (0, 300):
        await ctl.configure(origin_delay_ms=delay, origin_jitter_ms=0)
        await ctl.flush()
        cold = await ctl.fire(paths, concurrency)
        await ctl.fire(paths * 2, concurrency)  # warm all three edges
        warm = await ctl.fire(paths * 3, concurrency)
        out[f"origin_delay_{delay}ms"] = {"cold": cold, "warm": warm}
    return out


async def failures(ctl: Control, requests: int = 300, concurrency: int = 20) -> dict:
    objects = await ctl.objects()
    paths = zipf_paths(objects, requests, seed=11)
    await ctl.flush()
    await ctl.fire([f"/content/{o}" for o in objects], concurrency)  # warm the shield
    out = {"description": "Success rate and serving layer during component failures"}
    out["normal"] = await ctl.fire(paths, concurrency)
    await ctl.fail("edge-1", True)
    out["edge_1_down"] = await ctl.fire(paths, concurrency)
    await ctl.fail("edge-1", False)
    await ctl.flush("edges")
    await ctl.fail("shield", True)
    out["shield_down"] = await ctl.fire(paths, concurrency)
    await ctl.fail("shield", False)
    await ctl.fail("origin", True)
    out["origin_down"] = await ctl.fire(paths, concurrency)
    # Re-warm every cache with a 2 s TTL, let it all expire, then take the origin down:
    # only expired-but-kept (stale) copies can be served, and the shield's circuit breaker trips.
    await ctl.fail("origin", False)
    await ctl.configure(ttls={k: 2 for k in ("breaking", "index", "popular", "regular", "archive",
                                              "image", "thumbnail", "static")}, shield_ttl_factor=1.0)
    await ctl.flush()
    await ctl.fire([f"/content/{o}" for o in objects for _ in range(3)], concurrency)
    await ctl.fail("origin", True)
    await asyncio.sleep(2.5)
    out["origin_down_expired"] = await ctl.fire(paths, concurrency)
    await ctl.fail("origin", False)
    out["server"] = await ctl.server_summary()
    return out


SCENARIOS = {
    "workload": workload,
    "direct_vs_edge": direct_vs_edge,
    "shield_vs_origin": shield_vs_origin,
    "collapsing": collapsing,
    "delayed_origin": delayed_origin,
    "failures": failures,
}


async def run(base_url: str, scenario: str = "all", **options) -> dict:
    async with aiohttp.ClientSession(base_url=base_url, timeout=aiohttp.ClientTimeout(total=90),
                                     connector=aiohttp.TCPConnector(limit=300)) as client:
        ctl = Control(client)
        original = await ctl.get_json("/config")
        names = list(SCENARIOS) if scenario == "all" else [scenario]
        results = {}
        try:
            for name in names:
                started = time.time()
                results[name] = await SCENARIOS[name](ctl, **options)
                results[name]["finished_at"] = time.time()
                results[name]["duration_s"] = round(time.time() - started, 1)
                await ctl.post("/config", original)
        finally:
            await ctl.post("/config", original)
            await ctl.post("/failure/reset")
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark the edge CDN")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--scenario", default="all", choices=list(SCENARIOS) + ["all"])
    parser.add_argument("--requests", type=int)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--out", help="write JSON results to this file")
    args = parser.parse_args()
    options = {k: v for k, v in (("requests", args.requests), ("concurrency", args.concurrency)) if v}
    results = asyncio.run(run(args.url, args.scenario, **options))
    text = json.dumps(results, indent=2)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    print(text)


if __name__ == "__main__":
    main()
