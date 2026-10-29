"""Origin Shield: shared second-level cache with request collapsing, a bounded origin pool
and a circuit breaker protecting the origin."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from . import config
from .cache import CacheStore
from .node import NodeState, install_admin
from .upstream import Fetched, NotFound, UpstreamError, fetch, new_session


class CircuitOpen(Exception):
    pass


class CircuitBreaker:
    """closed -> (N consecutive failures) -> open -> (cool-down) -> half-open -> one trial call."""

    def __init__(self, threshold: int, reset_seconds: float):
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self.state = "closed"
        self.failures = 0
        self.trips = 0
        self.opened_at = 0.0
        self.trial_in_flight = False

    def before_call(self) -> None:
        if self.state == "open":
            if time.monotonic() - self.opened_at < self.reset_seconds:
                raise CircuitOpen("origin circuit is open")
            self.state = "half-open"
        if self.state == "half-open":
            if self.trial_in_flight:
                raise CircuitOpen("origin circuit is half-open (trial in flight)")
            self.trial_in_flight = True

    def record_success(self) -> None:
        self.state, self.failures, self.trial_in_flight = "closed", 0, False

    def record_failure(self) -> None:
        self.failures += 1
        self.trial_in_flight = False
        if self.state == "half-open" or (self.state == "closed" and self.failures >= self.threshold):
            self.state = "open"
            self.opened_at = time.monotonic()
            self.trips += 1

    def snapshot(self) -> dict:
        return {"state": self.state, "consecutive_failures": self.failures, "trips": self.trips}


state = NodeState(config.NODE_ID if config.NODE_ID != "node" else "shield", "shield",
                  CacheStore(config.REDIS_URL, "shield"))
breaker = CircuitBreaker(config.BREAKER_FAILURE_THRESHOLD, config.BREAKER_RESET_SECONDS)
inflight: dict[str, asyncio.Future] = {}
pool = asyncio.Semaphore(config.ORIGIN_POOL_SIZE)
pool_stats = {"waiting": 0, "active": 0, "size": config.ORIGIN_POOL_SIZE}
http = None  # aiohttp.ClientSession, created at startup


@asynccontextmanager
async def lifespan(_: FastAPI):
    global http
    http = new_session()
    yield
    await http.close()


app = FastAPI(title="CDN Origin Shield", lifespan=lifespan)


async def extra_status() -> dict:
    return {"circuit": breaker.snapshot(), "pool": pool_stats, "inflight": sorted(inflight)}


install_admin(app, state, extra_status)


async def fetch_from_origin(object_id: str) -> Fetched:
    breaker.before_call()
    pool_stats["waiting"] += 1
    acquired = False
    try:
        async with pool:
            acquired = True
            pool_stats["waiting"] -= 1
            pool_stats["active"] += 1
            try:
                state.metrics.inc("origin_fetches")
                fetched = await fetch(http, f"{config.ORIGIN_URL}/object/{object_id}",
                                      state.config.upstream_timeout_s)
            finally:
                pool_stats["active"] -= 1
    except NotFound:
        breaker.record_success()
        raise
    except UpstreamError:
        breaker.record_failure()
        raise
    except asyncio.CancelledError:
        breaker.trial_in_flight = False
        raise
    finally:
        if not acquired:
            pool_stats["waiting"] -= 1
    breaker.record_success()
    ttl = state.config.ttl_for(fetched.content_class, "shield")
    try:
        await state.store.set(object_id, fetched.body, fetched.content_type, fetched.content_class,
                              ttl, state.config.stale_grace_seconds)
    except Exception:
        state.metrics.inc("cache_errors")
    return fetched


async def collapsed_fetch(object_id: str) -> tuple[Fetched, bool]:
    """Ensure concurrent misses for the same object trigger exactly one origin fetch."""
    if not state.config.collapsing:
        return await fetch_from_origin(object_id), False
    pending = inflight.get(object_id)
    if pending is not None:
        state.metrics.inc("collapsed")
        return await asyncio.shield(pending), True
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    inflight[object_id] = future
    try:
        result = await fetch_from_origin(object_id)
        future.set_result(result)
        return result, False
    except asyncio.CancelledError:
        future.cancel()
        raise
    except Exception as exc:
        future.set_exception(exc)
        future.exception()  # mark retrieved so lone failures don't log warnings
        raise
    finally:
        inflight.pop(object_id, None)


def respond(body: bytes, content_type: str, content_class: str, status: str, age: int = 0) -> Response:
    return Response(body, media_type=content_type, headers={
        "X-Cache-Shield": status, "X-Content-Class": content_class,
        "X-Served-By": state.node_id, "Age": str(age),
    })


@app.get("/object/{object_id}")
async def get_object(object_id: str):
    started = time.perf_counter()
    state.metrics.inc("requests")
    try:
        cached = await state.store.get(object_id)
    except Exception:
        state.metrics.inc("cache_errors")
        cached = None
    try:
        if cached is not None and cached.fresh:
            state.metrics.inc("hits")
            return respond(cached.body, cached.content_type, cached.content_class, "HIT", cached.age)
        state.metrics.inc("misses")
        try:
            fetched, was_collapsed = await collapsed_fetch(object_id)
        except NotFound:
            return JSONResponse({"error": f"{object_id} not found"}, status_code=404)
        except (UpstreamError, CircuitOpen) as exc:
            if cached is not None:
                state.metrics.inc("stale_served")
                return respond(cached.body, cached.content_type, cached.content_class, "STALE", cached.age)
            state.metrics.inc("errors")
            return JSONResponse({"error": f"origin unavailable: {exc}"}, status_code=503)
        return respond(fetched.body, fetched.content_type, fetched.content_class,
                       "COLLAPSED" if was_collapsed else "MISS")
    finally:
        state.metrics.observe((time.perf_counter() - started) * 1000)
