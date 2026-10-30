"""In-process counters and latency tracking for every node."""
from __future__ import annotations

import time
from collections import Counter, deque


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[k]


class Metrics:
    def __init__(self, window: int = 5000):
        self.window = window
        self.reset()

    def reset(self) -> None:
        self.counters: Counter[str] = Counter()
        self.per_key: Counter[str] = Counter()
        self.samples: deque[tuple[float, float]] = deque(maxlen=self.window)
        self.started = time.time()

    def inc(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    def inc_key(self, key: str, n: int = 1) -> None:
        self.per_key[key] += n

    def observe(self, latency_ms: float) -> None:
        self.samples.append((time.time(), latency_ms))

    def snapshot(self) -> dict:
        now = time.time()
        latencies = [ms for _, ms in self.samples]
        recent = sum(1 for ts, _ in self.samples if now - ts <= 10)
        span = min(10.0, max(1.0, now - self.started))
        return {
            "counters": dict(self.counters),
            "latency": {
                "count": len(latencies),
                "avg_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0.0,
                "p50_ms": round(percentile(latencies, 50), 2),
                "p95_ms": round(percentile(latencies, 95), 2),
                "p99_ms": round(percentile(latencies, 99), 2),
            },
            "top_keys": dict(self.per_key.most_common(10)),
            "throughput_rps": round(recent / span, 2),
            "uptime_s": round(now - self.started, 1),
        }
