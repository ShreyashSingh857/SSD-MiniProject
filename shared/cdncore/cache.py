"""Redis-backed object cache shared by edge and shield nodes (cache-aside)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import quote

import redis.asyncio as aioredis

KEY_PREFIX = "obj:"
SURROGATE_PREFIX = "surrogate:"


def default_surrogate_keys(object_id: str) -> list[str]:
    """Provide useful demo groups; origins may override these with Surrogate-Key."""
    kind, _, identifier = object_id.partition(":")
    if kind == "article":
        return [f"article-{identifier}", "news", "homepage"]
    if kind in ("image", "thumbnail"):
        return [f"article-{identifier}", "media"]
    if object_id == "index:latest":
        return ["homepage", "news"]
    return [f"{kind}-{identifier}"] if kind else []


@dataclass
class CachedObject:
    object_id: str
    body: bytes
    content_type: str
    content_class: str
    created_at: float
    expires_at: float
    cache_layer: str
    surrogate_keys: list[str]

    @property
    def fresh(self) -> bool:
        return time.time() < self.expires_at

    @property
    def age(self) -> int:
        return int(time.time() - self.created_at)

    @property
    def max_age(self) -> int:
        return max(0, int(self.expires_at - self.created_at))

    @property
    def remaining_ttl(self) -> int:
        return max(0, int(self.expires_at - time.time()))


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
            surrogate_keys=data.get(b"surrogate_keys", b"").decode().split(),
        )

    async def set(self, object_id: str, body: bytes, content_type: str, content_class: str,
                  ttl: int, grace: int, surrogate_keys: list[str] | None = None) -> None:
        now = time.time()
        key = KEY_PREFIX + object_id
        keys = sorted(set(surrogate_keys if surrogate_keys is not None else default_surrogate_keys(object_id)))
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hget(key, "surrogate_keys")
            previous = (await pipe.execute())[0]
        async with self.redis.pipeline(transaction=True) as pipe:
            for old in (previous or b"").decode().split():
                pipe.delete(self._index_key(old, object_id))
            pipe.delete(key)
            pipe.hset(key, mapping={
                "body": body, "ctype": content_type, "cls": content_class,
                "created": now, "expires": now + ttl,
                "surrogate_keys": " ".join(keys),
            })
            # The Redis key outlives the logical TTL by `grace` so stale copies can be served on failure.
            pipe.expire(key, ttl + grace)
            for tag in keys:
                index = self._index_key(tag, object_id)
                pipe.set(index, object_id, ex=ttl + grace)
            await pipe.execute()

    @staticmethod
    def _index_key(tag: str, object_id: str) -> str:
        return f"{SURROGATE_PREFIX}{quote(tag, safe='')}:{quote(object_id, safe='')}"

    async def purge_surrogate(self, tag: str) -> int:
        count = 0
        async for index in self.redis.scan_iter(match=self._index_key(tag, "*").replace("%2A", "*"), count=500):
            value = await self.redis.get(index)
            if value:
                count += await self.delete(value.decode())
            else:
                await self.redis.delete(index)
        return count

    async def objects(self) -> list[dict]:
        """Return cache inventory, pruning any orphan surrogate index entries on expired data."""
        rows = []
        async for key in self.redis.scan_iter(match=KEY_PREFIX + "*", count=500):
            object_id = key.decode()[len(KEY_PREFIX):]
            obj = await self.get(object_id)
            if obj:
                rows.append({"object_id": object_id, "surrogate_keys": obj.surrogate_keys,
                             "max_age": obj.max_age, "remaining_ttl": obj.remaining_ttl,
                             "status": "fresh" if obj.fresh else "stale/expired"})
        return rows

    async def delete(self, object_id: str) -> int:
        key = KEY_PREFIX + object_id
        tags = await self.redis.hget(key, "surrogate_keys")
        async with self.redis.pipeline(transaction=True) as pipe:
            if tags:
                for tag in tags.decode().split():
                    pipe.delete(self._index_key(tag, object_id))
            pipe.delete(key)
            result = await pipe.execute()
        return int(result[-1])

    async def flush(self) -> int:
        count = 0
        async for key in self.redis.scan_iter(match=KEY_PREFIX + "*", count=500):
            count += await self.delete(key.decode()[len(KEY_PREFIX):])
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
                    "max_age": round(float(expires) - float(await self.redis.hget(key, "created"))) if expires else None,
                    "expires_in": round(float(expires) - now, 1) if expires else None,
                    "surrogate_keys": (await self.redis.hget(key, "surrogate_keys") or b"").decode().split(),
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
