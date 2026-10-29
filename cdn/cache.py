"""Redis-backed object cache shared by edge and shield nodes (cache-aside)."""
from __future__ import annotations

import time
from dataclasses import dataclass

import redis.asyncio as aioredis

KEY_PREFIX = "obj:"


@dataclass
class CachedObject:
    object_id: str
    body: bytes
    content_type: str
    content_class: str
    created_at: float
    expires_at: float
    cache_layer: str

    @property
    def fresh(self) -> bool:
        return time.time() < self.expires_at

    @property
    def age(self) -> int:
        return int(time.time() - self.created_at)


class CacheStore:
    def __init__(self, redis_url: str, layer: str):
        self.layer = layer
        self.redis = aioredis.from_url(redis_url, socket_timeout=1.0, socket_connect_timeout=1.0)

    async def get(self, object_id: str) -> CachedObject | None:
        """Return the cached object even if expired; callers decide whether stale copies are usable."""
        data = await self.redis.hgetall(KEY_PREFIX + object_id)
        if not data:
            return None
        return CachedObject(
            object_id=object_id,
            body=data[b"body"],
            content_type=data[b"ctype"].decode(),
            content_class=data[b"cls"].decode(),
            created_at=float(data[b"created"]),
            expires_at=float(data[b"expires"]),
            cache_layer=self.layer,
        )

    async def set(self, object_id: str, body: bytes, content_type: str, content_class: str,
                  ttl: int, grace: int) -> None:
        now = time.time()
        key = KEY_PREFIX + object_id
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.hset(key, mapping={
                "body": body, "ctype": content_type, "cls": content_class,
                "created": now, "expires": now + ttl,
            })
            # The Redis key outlives the logical TTL by `grace` so stale copies can be served on failure.
            pipe.expire(key, ttl + grace)
            await pipe.execute()

    async def delete(self, object_id: str) -> int:
        return await self.redis.delete(KEY_PREFIX + object_id)

    async def flush(self) -> int:
        count = 0
        async for key in self.redis.scan_iter(match=KEY_PREFIX + "*", count=500):
            count += await self.redis.delete(key)
        return count

    async def status(self) -> dict:
        info_mem = await self.redis.info("memory")
        info_stats = await self.redis.info("stats")
        keys = 0
        fresh = 0
        now = time.time()
        sample: list[dict] = []
        async for key in self.redis.scan_iter(match=KEY_PREFIX + "*", count=500):
            keys += 1
            expires = await self.redis.hget(key, "expires")
            is_fresh = expires is not None and float(expires) > now
            fresh += is_fresh
            if len(sample) < 25:
                sample.append({
                    "object_id": key.decode()[len(KEY_PREFIX):],
                    "fresh": is_fresh,
                    "expires_in": round(float(expires) - now, 1) if expires else None,
                })
        return {
            "layer": self.layer,
            "keys": keys,
            "fresh_keys": fresh,
            "stale_keys": keys - fresh,
            "used_memory": info_mem.get("used_memory_human"),
            "maxmemory_policy": info_mem.get("maxmemory_policy"),
            "redis_evicted_keys": info_stats.get("evicted_keys", 0),
            "redis_expired_keys": info_stats.get("expired_keys", 0),
            "sample": sorted(sample, key=lambda s: s["object_id"]),
        }
