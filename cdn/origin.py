"""Origin service: the authoritative store in front of MinIO.

Adds a configurable artificial delay (to model a far-away origin), counts every origin fetch,
seeds MinIO with the generated newspaper on startup and handles publishing new articles.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import urllib3
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from minio import Minio
from minio.error import S3Error

from . import config
from .content import generator
from .node import NodeState, install_admin

log = logging.getLogger("cdn.origin")

OBJECT_TYPES = {"article", "image", "thumbnail", "static", "index"}
SEED_MARKER = "meta/seed-version"

state = NodeState(config.NODE_ID if config.NODE_ID != "node" else "origin", "origin")
# Fail fast when MinIO is down instead of urllib3's default long retry/backoff.
client = Minio(config.MINIO_ENDPOINT, access_key=config.MINIO_ACCESS_KEY,
               secret_key=config.MINIO_SECRET_KEY, secure=False,
               http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=1.0, read=5.0),
                                               retries=urllib3.Retry(total=1, backoff_factor=0.1),
                                               maxsize=32))
seed_info: dict = {"seeded": False, "objects": 0, "error": None}
publish_lock = asyncio.Lock()


def storage_key(object_id: str) -> str | None:
    kind, _, name = object_id.partition(":")
    if kind not in OBJECT_TYPES or not name or ".." in name:
        return None
    return f"{kind}/{name}"


def _put(object_id: str, body: bytes, content_type: str, content_class: str) -> None:
    client.put_object(config.MINIO_BUCKET, storage_key(object_id), io.BytesIO(body), len(body),
                      content_type=content_type, metadata={"content-class": content_class})


def _get(key: str) -> tuple[bytes, str, str]:
    resp = client.get_object(config.MINIO_BUCKET, key)
    try:
        body = resp.read()
        ctype = resp.headers.get("Content-Type", "application/octet-stream")
        cls = resp.headers.get("x-amz-meta-content-class", "regular")
        return body, ctype, cls
    finally:
        resp.close()
        resp.release_conn()


def _read_marker() -> str | None:
    try:
        resp = client.get_object(config.MINIO_BUCKET, SEED_MARKER)
        try:
            return resp.read().decode()
        finally:
            resp.close()
            resp.release_conn()
    except S3Error:
        return None


def _seed() -> int:
    if not client.bucket_exists(config.MINIO_BUCKET):
        client.make_bucket(config.MINIO_BUCKET)
    version = generator.seed_version()
    if _read_marker() == version:
        return -1
    _, objects = generator.generate_all()
    for obj in objects:
        _put(obj.object_id, obj.body, obj.content_type, obj.content_class)
    marker = version.encode()
    client.put_object(config.MINIO_BUCKET, SEED_MARKER, io.BytesIO(marker), len(marker))
    return len(objects)


async def seed_forever() -> None:
    """Wait for MinIO to come up, then seed it (idempotent per generator version)."""
    for attempt in range(120):
        try:
            count = await asyncio.to_thread(_seed)
            seed_info.update(seeded=True, error=None,
                             objects=count if count >= 0 else "already seeded")
            log.info("MinIO seeded: %s", seed_info["objects"])
            return
        except Exception as exc:  # MinIO still starting
            seed_info["error"] = f"{exc.__class__.__name__}: {exc}"
            await asyncio.sleep(min(5, 0.5 + attempt * 0.25))


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(seed_forever())
    yield
    task.cancel()


app = FastAPI(title="CDN Origin Service", lifespan=lifespan)


async def extra_status() -> dict:
    return {"seed": seed_info, "bucket": config.MINIO_BUCKET}


install_admin(app, state, extra_status)


@app.get("/object/{object_id}")
async def get_object(object_id: str):
    started = time.perf_counter()
    key = storage_key(object_id)
    if key is None:
        return JSONResponse({"error": f"unknown object id {object_id!r}"}, status_code=404)
    if not seed_info["seeded"]:
        return JSONResponse({"error": "origin is still seeding"}, status_code=503)
    state.metrics.inc("origin_requests")
    state.metrics.inc_key(object_id)
    cfg = state.config
    delay = cfg.origin_delay_ms + random.uniform(0, cfg.origin_jitter_ms)
    await asyncio.sleep(delay / 1000)
    try:
        body, ctype, cls = await asyncio.to_thread(_get, key)
    except S3Error as exc:
        if exc.code in ("NoSuchKey", "NoSuchObject"):
            state.metrics.inc("not_found")
            return JSONResponse({"error": f"{object_id} not found"}, status_code=404)
        state.metrics.inc("storage_errors")
        return JSONResponse({"error": f"storage error: {exc.code}"}, status_code=503)
    except Exception as exc:  # MinIO down / unreachable
        state.metrics.inc("storage_errors")
        return JSONResponse({"error": f"storage unavailable: {exc.__class__.__name__}"}, status_code=503)
    elapsed = (time.perf_counter() - started) * 1000
    state.metrics.observe(elapsed)
    return Response(body, media_type=ctype, headers={
        "X-Content-Class": cls, "X-Served-By": state.node_id, "X-Origin-Latency-ms": f"{elapsed:.1f}",
    })


@app.post("/admin/publish")
async def publish(payload: dict | None = None):
    """Publish a new breaking-news article: write article, images and an updated index to MinIO."""
    payload = payload or {}
    if state.failed:
        return JSONResponse({"error": "origin is down (simulated failure)"}, status_code=503)
    async with publish_lock:
        try:
            body, _, _ = await asyncio.to_thread(_get, "index/latest")
        except Exception as exc:
            return JSONResponse({"error": f"storage unavailable: {exc.__class__.__name__}"}, status_code=503)
        index = json.loads(body)
        existing = {a["article_id"] for a in index["articles"]}
        n = 1 + sum(1 for a in existing if a.startswith("breaking-news-"))
        while f"breaking-news-{n:03d}" in existing:
            n += 1
        rng = random.Random(time.time_ns())
        section = payload.get("section") or rng.choice(list(generator.SECTIONS))
        article = generator.make_article(rng, n, section, "breaking", datetime.now(timezone.utc),
                                         breaking=True)
        if payload.get("title"):
            article.title = str(payload["title"])[:160]
        objects = await asyncio.to_thread(generator.article_objects, article)
        index["articles"].insert(0, article.card())
        index["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            for obj in objects:
                await asyncio.to_thread(_put, obj.object_id, obj.body, obj.content_type, obj.content_class)
            await asyncio.to_thread(_put, "index:latest", json.dumps(index).encode(),
                                    "application/json", "index")
        except Exception as exc:
            return JSONResponse({"error": f"storage unavailable: {exc.__class__.__name__}"}, status_code=503)
        state.metrics.inc("published")
        return {"published": article.card(), "objects": [o.object_id for o in objects] + ["index:latest"]}
