"""Shared plumbing for every CDN node: failure simulation, health, metrics and config endpoints."""
from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Awaitable, Callable

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from . import config
from .config import RuntimeConfig
from .metrics import Metrics

if TYPE_CHECKING:  # the router and origin have no Redis dependency
    from .cache import CacheStore


class NodeState:
    def __init__(self, node_id: str, role: str, store: CacheStore | None = None):
        self.node_id = node_id
        self.role = role
        self.store = store
        self.failed = False
        self.metrics = Metrics()
        self.config = RuntimeConfig()


def down_response(state: NodeState) -> JSONResponse:
    return JSONResponse(
        {"error": f"{state.node_id} is down (simulated failure)"},
        status_code=503,
        headers={"X-Node-Down": "1", "X-Served-By": state.node_id},
    )


class InternalAuth:
    """ASGI middleware: services are publicly reachable on some hosts, so hops carry a shared secret."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and config.INTERNAL_TOKEN and scope["path"] not in ("/health", "/healthz"):
            sent = dict(scope["headers"]).get(b"x-internal-token", b"").decode("latin-1")
            if not hmac.compare_digest(sent, config.INTERNAL_TOKEN):
                await JSONResponse({"error": "invalid internal token"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


class FailureGate:
    """ASGI middleware: a failed node refuses all traffic except the admin API used to restore it."""

    def __init__(self, app, state: NodeState):
        self.app = app
        self.state = state

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.state.failed and scope["path"] != "/healthz" and not scope["path"].startswith("/admin"):
            await down_response(self.state)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def install_admin(app: FastAPI, state: NodeState,
                  extra_status: Callable[[], Awaitable[dict]] | None = None) -> None:
    app.add_middleware(FailureGate, state=state)
    app.add_middleware(InternalAuth)

    @app.get("/healthz")
    async def liveness():
        # For the hosting platform: stays 200 during a simulated failure so the node is not restarted.
        return {"node": state.node_id, "alive": True}

    @app.get("/health")
    async def health():
        return {"node": state.node_id, "role": state.role, "status": "ok"}

    @app.post("/admin/failure")
    async def set_failure(payload: dict = Body(default={})):
        state.failed = bool(payload.get("down", not state.failed))
        state.metrics.inc("failure_toggles")
        return {"node": state.node_id, "down": state.failed}

    @app.get("/admin/metrics")
    async def metrics():
        return {"node": state.node_id, "role": state.role, "down": state.failed, **state.metrics.snapshot()}

    @app.post("/admin/metrics/reset")
    async def reset_metrics():
        state.metrics.reset()
        return {"node": state.node_id, "reset": True}

    @app.get("/admin/config")
    async def get_config():
        return state.config.to_dict()

    @app.post("/admin/config")
    async def set_config(payload: dict = Body(default={})):
        state.config.update(payload)
        return state.config.to_dict()

    @app.get("/admin/status")
    async def status():
        data: dict = {"node": state.node_id, "role": state.role, "down": state.failed}
        if state.store is not None:
            try:
                data["cache"] = await state.store.status()
            except Exception as exc:  # Redis unavailable
                data["cache"] = {"error": f"cache unavailable: {exc.__class__.__name__}"}
        if extra_status is not None:
            data.update(await extra_status())
        return data

    @app.post("/admin/flush")
    async def flush(payload: dict = Body(default={})):
        if state.store is None:
            return {"node": state.node_id, "flushed": 0}
        object_id = payload.get("object_id")
        flushed = await (state.store.delete(object_id) if object_id else state.store.flush())
        state.metrics.inc("flushes")
        return {"node": state.node_id, "flushed": flushed}
