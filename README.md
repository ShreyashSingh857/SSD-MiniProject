# Edge CDN Cache with Origin Shielding

A small tiered CDN built with **Python / FastAPI + Redis + MinIO**. Its traffic comes from a
simulated newspaper, *The Daily Edge*. The system has three edge caches with independent Redis
instances, an Origin Shield shared by all of them, request collapsing, TTL-based expiry by content
type, failure simulation and recovery, live metrics, and the benchmark suite from the project
document.

- **Newspaper:** `index.html` on the web service. Every article, image, thumbnail, stylesheet and script is fetched through the
  CDN. Open the *CDN inspector* (bottom right) to see which layer served each object.
- **Control room:** `dashboard.html` on the web service. Shows the live topology, hit ratios and latency. It also has the
  failure buttons, a traffic generator, newsroom publishing, guided cache scenarios, MinIO deletion events and benchmarks.
- **API docs:** `/docs` on the router.

## Architecture

```
Readers ─► Router ──round-robin + health checks──► Edge 1 │ Edge 2 │ Edge 3      (FastAPI + own Redis)
                                                     │ miss
                                                     ▼
                                   Origin Shield  (FastAPI + shared Redis)
                                   ├─ request collapser: one origin fetch per object, waiters share it
                                   ├─ bounded origin pool (4 concurrent fetches)
                                   └─ circuit breaker (opens after 5 consecutive origin failures)
                                                     │ miss
                                                     ▼
                                   Origin service ─► MinIO   (articles / images / thumbnails / static)
```

| Path | Route |
|---|---|
| Edge hit | User → Router → Edge → User |
| Shield hit | User → Router → Edge → Shield → Edge (fills edge) → User |
| Origin miss | User → Router → Edge → Shield → Origin/MinIO → Shield (fills) → Edge (fills) → User |

Every response says where it was served from:

| Header | Meaning |
|---|---|
| `X-Cache` | `HIT-EDGE`, `HIT-SHIELD`, `MISS-ORIGIN`, `MISS-COLLAPSED` (waited on another request's fetch), `MISS-ORIGIN-BYPASS` (shield down), `STALE-EDGE` / `STALE-SHIELD` (origin down, expired copy served) |
| `X-Served-By` | the edge that answered |
| `X-Latency-ms` | time spent inside the CDN |
| `X-Request-Id` | ID of the request, also shown in the dashboard request log |

### Caching strategy (cache-aside)

- **TTL by content class:** breaking news 20 s, homepage index 10 s, popular 120 s, regular 300 s,
  archive 900 s, images and thumbnails 1800 s, static files 3600 s. The shield keeps each object 2×
  longer than the edges. TTLs can be changed at runtime from the dashboard or with `POST /config`.
- **Expiry and eviction:** each Redis key lives for *TTL + grace*. Once the TTL passes, the copy is
  stale. A stale copy is refetched on the next request, but it is served if every upstream layer is
  failing. Redis runs with `maxmemory` and `allkeys-lru`, so memory pressure evicts the least
  recently used objects.
- **Purge on publish:** publishing breaking news writes the article to MinIO and purges
  `index:latest` from every cache.
- **MinIO deletion events:** bucket removal notifications are delivered to the router webhook, which purges the matching
  object from every edge and Origin Shield. The dashboard shows event delivery and per-node propagation timings.

### Cache invalidation and expiry demo

Each cached object carries zero or more surrogate keys. Demo defaults group `article:101` under
`article-101`, `news`, and `homepage`; `image:101` is grouped under `article-101` and `media`.
Responses expose the keys in `Surrogate-Key`. Every Edge and the Shield stores its own indexes.
The Redis object hash is `obj:<object_id>` and stores `surrogate_keys`; each index member is an
individually expiring string at `surrogate:<URL-encoded-key>:<URL-encoded-object-id>`. Its Redis
expiry matches object TTL plus stale grace, so abandoned index entries expire without keyspace
notifications or Redis `KEYS` scans. Replacements and explicit deletions remove old index entries.

TTL expiration is automatic: logical expiry remains the configured content-class TTL and Redis
retains the object and index through TTL plus `stale_grace_seconds` for stale-on-origin-failure.
The dashboard's **Cache Invalidation & Expiry Demo** lists each node's objects, key groups,
configured max age, remaining TTL, and fresh/stale status. Set Breaking-news TTL to a few seconds
in Cache & origin and Apply, then request an article to watch it become stale. After grace it is
removed by Redis. The dashboard inventory refreshes every 2.5 seconds.

Manual object-ID purge removes that exact object from selected node caches. Surrogate-Key purge
removes every locally indexed object in that group on selected nodes. A layer-only flush removes
all objects on the selected layers. These operations are distinct from TTL expiry and do not
delete content in MinIO. The existing `/api/delete/{object_id}` deletes at the origin and then
purges all cache layers; publishing continues to purge `index:latest` everywhere.

`POST /cache/flush` accepts `{"layer":"all|edges|shield|edge-1|edge-2|edge-3"}` and either
`object_id` or `surrogate_key`. The response includes `purge_type`, requested identifier,
`objects_invalidated`, `nodes_contacted`, `failed_nodes`, per-node `result`, and
`propagation_latency_ms`. It is protected by the router's existing `CONTROL_TOKEN` middleware.
`GET /cache/objects` returns node-by-node inventory for the dashboard. When a control token is
configured, provide it as `X-Control-Token` on POST requests.

```bash
# Warm the object through the CDN, then remove one object from every cache layer
curl -X POST http://localhost:8000/cache/flush -H 'Content-Type: application/json' \
  -d '{"layer":"all","object_id":"article:101"}'

# Remove all cached articles/images grouped as homepage content
curl -X POST http://localhost:8000/cache/flush -H 'Content-Type: application/json' \
  -d '{"layer":"all","surrogate_key":"homepage"}'

# Flush only the Edge caches
curl -X POST http://localhost:8000/cache/flush -H 'Content-Type: application/json' \
  -d '{"layer":"edges"}'
```

Practical demo: open `/dashboard.html`, request `article:world-013` using the newspaper (the demo
classifies article objects into `news` and `homepage`), then inspect Cache Invalidation & Expiry
Demo. Purge `homepage` and observe removals on contacted Edge/Shield nodes. Warm it again, set the
breaking TTL to 3 seconds, request a breaking-news object and watch remaining TTL reach zero and
status turn stale. Restore the TTL afterwards. Object-ID purge targets one object; surrogate-key
purge targets a content group; TTL expiry follows each object's configured age automatically.

### Failure handling

| Failure | What happens |
|---|---|
| Edge down | The router's health checker (1 s) and per-request failover send traffic to the other edges |
| Shield down | Edges bypass the shield and fetch from the origin directly (`MISS-ORIGIN-BYPASS`) |
| Origin / MinIO down | Fresh cached copies are served as usual, and expired copies are served as `STALE-*`. The circuit breaker stops requests from piling onto the dead origin |
| Slow origin | `origin_delay_ms` (dashboard slider) adds artificial latency |
| Burst of identical misses | Request collapsing: one origin fetch, everyone else waits on it |

## Microservices

Each service has its own folder, Dockerfile and dependencies, and can be built, deployed and scaled
on its own. The services talk only over HTTP.

| Service | Folder | Responsibility | Storage |
|---|---|---|---|
| Router | `services/router` | Public API, round-robin, failover, metrics, control API, benchmarks | none |
| Edge (3 instances) | `services/edge` | First-level cache | its own Redis |
| Shield | `services/shield` | Shared cache, request collapsing, origin pool, circuit breaker | its own Redis |
| Origin | `services/origin` | Reads MinIO, adds delay, seeds and reseeds content, publishes | none |
| MinIO | `services/minio` | S3 object storage | `/data` |
| Web | `services/web` | Newspaper and dashboard static pages | none |

`shared/cdncore` is a small library copied into each Python image at build time: configuration, cache
store, metrics, service admin API and HTTP helpers.

`docker-compose.yml` runs each service in its own container, and `render.yaml` deploys each as a
separate Render service. `docker-compose.render-sim.yml` adds Render-like limits for local testing.

## Where the newspaper data comes from

The content is generated by `services/origin/content/generator.py` on first start and seeded into MinIO. It
includes 60 fictional articles in 8 sections, split into breaking, popular, regular and archive
categories. Each article gets a hero image and a thumbnail rendered as real JPEGs with Pillow.
The static CSS, JS and SVG files are seeded too, for 184 objects (about 4.4 MB) in total. Because
the data is synthetic, it has no copyright or API dependencies and is the same on every run. You
can add articles while the system is running with **Publish breaking news**.

## Run locally

```bash
docker compose up --build
```

- Newspaper: http://localhost:8080
- Dashboard: http://localhost:8080/dashboard.html
- Router API docs: http://localhost:8000/docs
- MinIO console: http://localhost:9002 (`cdnadmin` / `cdnadmin-secret`)

You can also cause real failures, for example `docker compose stop edge-2`.

To test under Render-like limits (512 MB and 0.1 CPU per service, tokens on every hop, private Redis in
each cache, MinIO without a disk):

```bash
docker compose -f docker-compose.yml -f docker-compose.render-sim.yml up --build
```

## Deploy on Render

Every service deploys separately on Render's free plan. **[docs/RENDER_DEPLOYMENT.md](docs/RENDER_DEPLOYMENT.md)**
lists all settings and environment variables, and how the services connect.

## Benchmarks

Run them from the dashboard (**Run benchmark**) or from the CLI:

```bash
pip install -r requirements-dev.txt
PYTHONPATH=shared python services/router/bench.py --url http://localhost:8000 --scenario all --out results.json
```

The results below were measured with `docker compose` on one development machine, without CPU limits,
with a 120 ms (+0–40 ms jitter) origin delay. Raw data is in
[`docs/benchmark-results.json`](docs/benchmark-results.json). Numbers on Render's free plan will be
much worse.

| Comparison | Result |
|---|---|
| Direct origin vs edge cache (same hot articles) | 142.6 ms → **6.0 ms** avg (24×), 135 → 3,050 req/s |
| Origin fetch vs shield hit vs edge hit | 324 ms → **5.1 ms** → **4.2 ms** avg |
| 200 simultaneous requests for one uncached image, without vs with collapsing | origin requests **172 → 1**; p95 5,853 ms → **196 ms** |
| Normal vs artificially delayed origin (300 ms) | cold 10 ms vs **643 ms**; warm stays about 3 ms |
| Realistic Zipf workload, cold caches (1,500 requests) | 81.8% cache hit ratio, 162 origin requests, 111 collapsed |
| Failures: edge-1 down / shield down / origin down / origin down with expired caches | **100% success in every phase**; shield-down uses 156 bypasses, expired-origin-down serves 137 stale copies |

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests                                   # unit tests (collapsing, breaker, TTLs, generator)
docker compose up -d --build
CDN_URL=http://localhost:8000 python -m pytest tests     # plus end-to-end tests against the live stack
```

## API

These are the router's endpoints. When `CONTROL_TOKEN` is set, every POST needs an `X-Control-Token` header.

| Method | Endpoint | Description |
|---|---|---|
| GET | `/article/{article_id}` | Request an article through the CDN |
| GET | `/content/{object_id}` | Request any cacheable object, e.g. `image:world-013`, `static:homepage.css`, `index:latest` |
| GET | `/direct/{object_id}` | Bypass the CDN (benchmark baseline) |
| GET | `/cache/status` | Keys, memory and evictions per cache; circuit and pool state |
| GET | `/cache/objects` | Node-by-node object inventory, surrogate keys, and TTL state |
| POST | `/cache/flush` | `{"layer":"all|edges|shield|edge-2", "object_id": optional, "surrogate_key": optional}` |
| GET | `/metrics` | Hit ratios, avg/p50/p95/p99 latency, throughput, origin requests, collapsing efficiency, per-node counters |
| POST | `/metrics/reset` | Reset all counters |
| GET | `/health` | Component availability |
| GET | `/healthz` | Liveness for hosting platforms on internal services; stays 200 during a simulated failure |
| POST | `/failure/{component}` | `{"down": true\|false}` for `edge-1..3`, `shield`, `origin`; `/failure/reset` restores everything |
| GET/POST | `/config` | Runtime config (TTLs, collapsing, origin delay, timeouts), pushed to every node |
| POST | `/api/publish` | Publish a breaking-news article and purge the homepage index |
| POST | `/api/delete/{object_id}` | Delete an object through the origin and wait for MinIO's removal event and cache invalidation |
| POST | `/minio/events` | Authenticated MinIO webhook receiver for bucket removal events |
| GET | `/api/invalidation/events` | Recent MinIO delete events, delivery latency and per-node purge timings |
| POST/GET | `/api/benchmark` | Start a scenario (`workload`, `direct_vs_edge`, `shield_vs_origin`, `collapsing`, `delayed_origin`, `failures`, `all`) / read results |

## Notes

- MinIO's upstream project was archived and stopped publishing images, so this project uses
  `pgsty/minio`, a maintained community build of the same server.
- Internal hops use aiohttp. With httpx, the router reached only about 390 req/s; with aiohttp it
  reaches about 1,860 req/s.
