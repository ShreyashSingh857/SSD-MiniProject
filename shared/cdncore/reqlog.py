"""One log line per request on every service, so each hop (router -> edge -> shield -> origin) shows
up in the hosting platform's logs.

The router gives each reader request an ID and every hop passes it on in X-Request-Id, so one
request can be followed through all the services' logs by searching for its ID.
"""
from __future__ import annotations

import contextvars
import logging
import re
import sys
import time
import uuid

from . import config

# ID of the request being handled; upstream.get_raw() forwards it to the next layer.
request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

log = logging.getLogger("cdn.requests")
if not log.handlers:
    # Own handler: uvicorn runs with --log-level warning and does not configure the root logger.
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False

# Health checks and dashboard polling run every few seconds and would bury the real traffic.
QUIET_PATHS = {"/health", "/healthz", "/favicon.ico"}
QUIET_GETS = {"/metrics", "/api/requests", "/api/benchmark", "/cache/status", "/config"}
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def is_quiet(method: str, path: str) -> bool:
    if method == "OPTIONS" or path in QUIET_PATHS:
        return True
    return method == "GET" and (path in QUIET_GETS or path.startswith("/admin/"))


def format_line(node_id: str, rid: str, method: str, path: str, status: int,
                headers: dict[str, str], elapsed_ms: float) -> str:
    parts = [f"[{node_id}]", f"req={rid}", method, path, "->", str(status)]
    layer = headers.get("x-cache") or headers.get("x-cache-shield")
    if layer:
        parts.append(layer)
    served_by = headers.get("x-served-by")
    if served_by and served_by != node_id:
        parts.append(f"via={served_by}")
    if headers.get("x-edge-attempts", "1") != "1":
        parts.append(f"attempts={headers['x-edge-attempts']}")
    if "x-origin-latency-ms" in headers:
        parts.append("source=MinIO")
    parts.append(f"{elapsed_ms:.1f}ms")
    size = headers.get("content-length")
    if size and size.isdigit():
        n = int(size)
        parts.append(f"{n / 1024:.1f}KB" if n >= 1024 else f"{n}B")
    return " ".join(parts)


class RequestLog:
    """ASGI middleware: logs every request this service handles, with its result and duration.

    trust_incoming=False makes the service start a new ID for every request (the public router);
    internal services keep the ID they were sent so the whole chain shares it.
    """

    def __init__(self, app, node_id: str, trust_incoming: bool = True):
        self.app = app
        self.node_id = node_id
        self.trust_incoming = trust_incoming

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not config.REQUEST_LOG:
            await self.app(scope, receive, send)
            return
        rid = ""
        if self.trust_incoming:
            sent = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
            rid = _UNSAFE.sub("", sent)[:32]
        rid = rid or new_id()
        token = request_id.set(rid)
        started = time.perf_counter()
        result: dict = {"status": 500, "headers": {}}

        async def capture(message):
            if message["type"] == "http.response.start":
                result["status"] = message["status"]
                result["headers"] = {k.decode("latin-1").lower(): v.decode("latin-1")
                                     for k, v in message.get("headers", [])}
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            request_id.reset(token)
            method, path = scope["method"], scope["path"]
            if not is_quiet(method, path):
                log.info(format_line(self.node_id, rid, method, path, result["status"], result["headers"],
                                     (time.perf_counter() - started) * 1000))
