"""Edge cache node: first-level Redis cache; misses go to the Origin Shield, or straight to the
origin when the shield is down. Stale copies are served if every upstream layer fails."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from cdncore import config
from cdncore.cache import CacheStore, CachedObject
from cdncore.node import NodeState, install_admin
from cdncore.upstream import Fetched, NotFound, UpstreamError, fetch, new_session

state = NodeState(config.NODE_ID if config.NODE_ID != "node" else "edge", "edge",
                  CacheStore(config.REDIS_URL, "edge"))
http = None  # aiohttp.ClientSession, created at startup

SHIELD_TO_LAYER = {
    "HIT": "HIT-SHIELD",
    "MISS": "MISS-ORIGIN",
    "COLLAPSED": "MISS-COLLAPSED",
    "STALE": "STALE-SHIELD",
}
# A stale object relayed from the shield is cached only briefly so recovery is noticed quickly.
STALE_RELAY_TTL = 5
SHIELD_RETRY_SECONDS = 2.0
shield_down_until = 0.0


@asynccontextmanager
async def lifespan(_: FastAPI):
    global http
    http = new_session()
    yield
    await http.close()


app = FastAPI(title="CDN Edge Cache", lifespan=lifespan)
install_admin(app, state)


def respond(body: bytes, content_type: str, content_class: str, layer: str, age: int = 0) -> Response:
    return Response(body, media_type=content_type, headers={
        "X-Cache": layer,
        "X-Served-By": state.node_id,
        "X-Content-Class": content_class,
        "Age": str(age),
        # Keep browsers from caching so every page view exercises the CDN layers in the demo.
        "Cache-Control": "no-store",
    })


async def fetch_upstream(object_id: str) -> tuple[Fetched | None, str]:
    """Return (object, layer label) or (None, reason) when no upstream could serve it."""
    global shield_down_until
    timeout = state.config.upstream_timeout_s
    if time.monotonic() >= shield_down_until:
        try:
            # One second longer than the shield's own origin timeout, so the shield answers first.
            fetched = await fetch(http, f"{config.SHIELD_URL}/object/{object_id}", timeout + 1)
            return fetched, SHIELD_TO_LAYER.get(fetched.headers.get("x-cache-shield", "MISS"), "MISS-ORIGIN")
        except UpstreamError as exc:
            if not exc.node_down:
                return None, str(exc)  # shield is alive but the origin failed and it had no stale copy
            # Skip the dead shield for a moment instead of paying the connect failure on every request.
            state.metrics.inc_key(f"shield-unreachable: {str(exc).split(' calling')[0]}")
            shield_down_until = time.monotonic() + SHIELD_RETRY_SECONDS
    # Shield unreachable: bypass it and go straight to the origin.
    state.metrics.inc("shield_bypass")
    try:
        fetched = await fetch(http, f"{config.ORIGIN_URL}/object/{object_id}", timeout)
        return fetched, "MISS-ORIGIN-BYPASS"
    except UpstreamError as exc:
        return None, str(exc)


@app.post("/admin/shield-recovered")
async def shield_recovered():
    """Called by the router when the shield is restored, so edges stop bypassing it at once."""
    global shield_down_until
    shield_down_until = 0.0
    return {"node": state.node_id, "shield_bypass_cleared": True}


@app.get("/content/{object_id}")
async def get_content(object_id: str):
    started = time.perf_counter()
    state.metrics.inc("requests")
    try:
        cached: CachedObject | None = await state.store.get(object_id)
    except Exception:
        state.metrics.inc("cache_errors")
        cached = None
    try:
        if cached is not None and cached.fresh:
            state.metrics.inc("hits")
            return respond(cached.body, cached.content_type, cached.content_class, "HIT-EDGE", cached.age)
        state.metrics.inc("misses")
        if cached is not None:
            state.metrics.inc("expired")
        try:
            fetched, layer = await fetch_upstream(object_id)
        except NotFound:
            return JSONResponse({"error": f"{object_id} not found"}, status_code=404,
                                headers={"X-Served-By": state.node_id, "X-Cache": "MISS"})
        if fetched is None:
            if cached is not None:
                state.metrics.inc("stale_served")
                return respond(cached.body, cached.content_type, cached.content_class, "STALE-EDGE", cached.age)
            state.metrics.inc("errors")
            return JSONResponse({"error": f"content unavailable: {layer}"}, status_code=503,
                                headers={"X-Served-By": state.node_id, "X-Cache": "ERROR"})
        state.metrics.inc_key(layer)
        ttl = STALE_RELAY_TTL if layer == "STALE-SHIELD" else state.config.ttl_for(fetched.content_class)
        try:
            await state.store.set(object_id, fetched.body, fetched.content_type, fetched.content_class,
                                  ttl, state.config.stale_grace_seconds)
        except Exception:
            state.metrics.inc("cache_errors")
        return respond(fetched.body, fetched.content_type, fetched.content_class, layer)
    finally:
        state.metrics.observe((time.perf_counter() - started) * 1000)
