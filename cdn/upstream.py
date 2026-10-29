"""HTTP helpers for calling the next layer (edge -> shield -> origin).

aiohttp is used for every internal hop: it sustains several times the request rate of httpx
under concurrency, which matters because each user request crosses up to four hops.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import aiohttp


CONNECT_TIMEOUT = 1.5


@dataclass
class Fetched:
    body: bytes
    content_type: str
    content_class: str
    headers: dict[str, str]  # lower-cased header names


class UpstreamError(Exception):
    """The upstream layer is unreachable, down or failed to produce the object."""

    def __init__(self, message: str, node_down: bool = False):
        super().__init__(message)
        self.node_down = node_down


class NotFound(Exception):
    pass


def new_session(limit: int = 200) -> aiohttp.ClientSession:
    # Short DNS cache so a restarted container's new IP is picked up quickly.
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=limit, keepalive_timeout=30, ttl_dns_cache=2))


async def get_raw(session: aiohttp.ClientSession, url: str, timeout: float) -> tuple[int, dict[str, str], bytes]:
    """GET a URL; returns (status, lower-cased headers, body). Raises aiohttp/timeout errors."""
    # A short connect timeout makes a dead node (stopped container, failed DNS) fail fast.
    client_timeout = aiohttp.ClientTimeout(total=timeout, connect=min(CONNECT_TIMEOUT, timeout))
    async with session.get(url, timeout=client_timeout) as resp:
        body = await resp.read()
        return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body


async def fetch(session: aiohttp.ClientSession, url: str, timeout: float) -> Fetched:
    try:
        status, headers, body = await get_raw(session, url, timeout)
    except aiohttp.ConnectionTimeoutError as exc:
        raise UpstreamError(f"connect timeout calling {url}", node_down=True) from exc
    except asyncio.TimeoutError as exc:
        # Slow, not dead: callers should not treat a read timeout as the node being down.
        raise UpstreamError(f"timeout calling {url}", node_down=False) from exc
    except aiohttp.ClientError as exc:
        raise UpstreamError(f"{exc.__class__.__name__} calling {url}", node_down=True) from exc
    if status == 404:
        raise NotFound(url)
    if status >= 400:
        raise UpstreamError(f"HTTP {status} from {url}", node_down=headers.get("x-node-down") == "1")
    return Fetched(
        body=body,
        content_type=headers.get("content-type", "application/octet-stream"),
        content_class=headers.get("x-content-class", "regular"),
        headers=headers,
    )
