"""Static settings (from environment) and mutable runtime configuration."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


NODE_ID = _env("NODE_ID", "node")
PORT = int(_env("PORT", "8000"))
REDIS_URL = _env("REDIS_URL", "redis://localhost:6379/0")

# Shared secret on every hop between services. Empty disables the check (local development).
INTERNAL_TOKEN = _env("INTERNAL_TOKEN", "")
# Protects the router's public POST control API (failures, flush, config, publish, benchmarks).
CONTROL_TOKEN = _env("CONTROL_TOKEN", "")
# Browser origins allowed to call the router, comma separated.
WEB_ORIGINS = _env("WEB_ORIGINS", "*")

# Defaults suit one local network. Hosts where services sleep need much longer values.
CONNECT_TIMEOUT_S = float(_env("CONNECT_TIMEOUT_S", "1.5"))
UPSTREAM_TIMEOUT_S = float(_env("UPSTREAM_TIMEOUT_S", "5"))
HEALTH_TIMEOUT_S = float(_env("HEALTH_TIMEOUT_S", "1"))
HEALTH_INTERVAL_S = float(_env("HEALTH_INTERVAL_S", "1"))
ADMIN_TIMEOUT_S = float(_env("ADMIN_TIMEOUT_S", "5"))

SHIELD_URL = _env("SHIELD_URL", "http://localhost:8010")
ORIGIN_URL = _env("ORIGIN_URL", "http://localhost:8020")
# "edge-1=http://localhost:8001,edge-2=http://localhost:8002,..."
EDGE_URLS = _env(
    "EDGE_URLS",
    "edge-1=http://localhost:8001,edge-2=http://localhost:8002,edge-3=http://localhost:8003",
)

MINIO_ENDPOINT = _env("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = _env("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = _env("MINIO_SECRET_KEY", "minioadmin")
MINIO_BUCKET = _env("MINIO_BUCKET", "newspaper")
MINIO_SECURE = _env("MINIO_SECURE", "false").lower() == "true"
MINIO_CONNECT_TIMEOUT_S = float(_env("MINIO_CONNECT_TIMEOUT_S", "1"))
MINIO_READ_TIMEOUT_S = float(_env("MINIO_READ_TIMEOUT_S", "5"))
SEED_ARTICLES = int(_env("SEED_ARTICLES", "60"))

# Size of the shield's origin fetch pool and circuit breaker settings.
ORIGIN_POOL_SIZE = int(_env("ORIGIN_POOL_SIZE", "4"))
BREAKER_FAILURE_THRESHOLD = int(_env("BREAKER_FAILURE_THRESHOLD", "5"))
BREAKER_RESET_SECONDS = float(_env("BREAKER_RESET_SECONDS", "10"))


def parse_edges(spec: str = EDGE_URLS) -> dict[str, str]:
    edges: dict[str, str] = {}
    for part in spec.split(","):
        part = part.strip()
        if part:
            name, url = part.split("=", 1)
            edges[name.strip()] = url.strip().rstrip("/")
    return edges


# Edge TTL in seconds per content class. The shield keeps objects SHIELD_TTL_FACTOR times longer.
DEFAULT_TTLS = {
    "breaking": 20,
    "index": 10,
    "popular": 120,
    "regular": 300,
    "archive": 900,
    "image": 1800,
    "thumbnail": 1800,
    "static": 3600,
}


@dataclass
class RuntimeConfig:
    """Settings that can be changed at runtime through POST /config."""

    ttls: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TTLS))
    shield_ttl_factor: float = 2.0
    # How long an expired object is kept around to be served if upstream layers fail.
    stale_grace_seconds: int = 3600
    collapsing: bool = True
    origin_delay_ms: int = 120
    origin_jitter_ms: int = 40
    routing_mode: str = "round-robin"
    upstream_timeout_s: float = UPSTREAM_TIMEOUT_S

    def ttl_for(self, content_class: str, layer: str = "edge") -> int:
        ttl = self.ttls.get(content_class, self.ttls["regular"])
        if layer == "shield":
            ttl = int(ttl * self.shield_ttl_factor)
        return max(1, ttl)

    def update(self, data: dict) -> None:
        for key, value in data.items():
            if key == "ttls" and isinstance(value, dict):
                self.ttls.update({k: int(v) for k, v in value.items()})
            elif hasattr(self, key):
                current = getattr(self, key)
                if isinstance(current, bool) and not isinstance(value, bool):
                    value = str(value).lower() in ("1", "true", "yes", "on")
                setattr(self, key, type(current)(value))

    def to_dict(self) -> dict:
        return asdict(self)
