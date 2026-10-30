# Deploying the Edge CDN on Render

This guide deploys every microservice as its own Render service on the free plan. You can use the
Blueprint (`render.yaml`, recommended) or create each service by hand. Both produce the same result.

## 1. What gets deployed

| Render service | Type | Folder | Purpose | Needs a URL from |
|---|---|---|---|---|
| `edge-cdn-minio` | Web, Docker | `services/minio` | S3 object storage | nothing |
| `edge-cdn-origin` | Web, Docker | `services/origin` | Reads MinIO, seeds the newspaper, publishes | minio |
| `edge-cdn-shield` | Web, Docker | `services/shield` | Shared cache, request collapsing, circuit breaker | origin |
| `edge-cdn-edge-1` | Web, Docker | `services/edge` | Edge cache | shield, origin |
| `edge-cdn-edge-2` | Web, Docker | `services/edge` | Edge cache | shield, origin |
| `edge-cdn-edge-3` | Web, Docker | `services/edge` | Edge cache | shield, origin |
| `edge-cdn-router` | Web, Docker | `services/router` | Public API, load balancing, metrics, control | edges, shield, origin, web |
| `edge-cdn-web` | Static site | `services/web` | Newspaper and dashboard pages | router |

Redis is **not** a separate Render service. Render allows one free Key Value instance per workspace,
and this design needs four independent caches. So each edge and the shield starts its own private
Redis inside its container, used only when `REDIS_URL` is empty.

## 2. How the services connect

```
Browser ─► edge-cdn-web (static pages)
   │
   └─ HTTPS + CORS ─► edge-cdn-router ─► edge-cdn-edge-1/2/3 ─► edge-cdn-shield ─► edge-cdn-origin ─► edge-cdn-minio
                            │                   │                                        ▲
                            │                   └── bypass when the shield is down ──────┘
                            └── /direct, health checks, control API to every service
```

- **Addressing:** services call each other through their public `https://<name>.onrender.com` URLs,
  set in environment variables. Render's private network can't be used: free web services can send
  private traffic but can't receive it.
- **Authentication between services:** because those URLs are public, every internal request carries
  `X-Internal-Token`. Services reject requests without it, except `/health` and `/healthz`. All
  services must have the same `INTERNAL_TOKEN`.
- **Control API:** POST requests to the router need `X-Control-Token`. The dashboard asks for this
  value the first time you press a control button.
- **Browser access:** the router allows only the origins in `WEB_ORIGINS` (the static site's URL).

## 3. Before you start

1. Push this repository to GitHub.
2. Create a Render account and connect GitHub. Check whether Render asks for a card when you sign up.
3. Pick the same region for every service. The Blueprint uses Oregon.

## 4. Deploy with the Blueprint (recommended)

1. In Render, go to **New → Blueprint** and select this repository. Render finds `render.yaml`.
2. Render prompts for the values marked `sync: false`. Enter the values in the table below, assuming
   each service gets the URL matching its name. You'll correct any that differ in step 5.

   | Service | Key | Value |
   |---|---|---|
   | `edge-cdn-origin` | `MINIO_ENDPOINT` | `edge-cdn-minio.onrender.com` (**no** `https://`) |
   | `edge-cdn-shield` | `ORIGIN_URL` | `https://edge-cdn-origin.onrender.com` |
   | `edge-cdn-edge-1/2/3` | `SHIELD_URL` | `https://edge-cdn-shield.onrender.com` |
   | `edge-cdn-edge-1/2/3` | `ORIGIN_URL` | `https://edge-cdn-origin.onrender.com` |
   | `edge-cdn-router` | `SHIELD_URL` | `https://edge-cdn-shield.onrender.com` |
   | `edge-cdn-router` | `ORIGIN_URL` | `https://edge-cdn-origin.onrender.com` |
   | `edge-cdn-router` | `EDGE_URLS` | `edge-1=https://edge-cdn-edge-1.onrender.com,edge-2=https://edge-cdn-edge-2.onrender.com,edge-3=https://edge-cdn-edge-3.onrender.com` |
   | `edge-cdn-router` | `WEB_ORIGINS` | `https://edge-cdn-web.onrender.com` |
   | `edge-cdn-web` | `CDN_API_URL` | `https://edge-cdn-router.onrender.com` |

3. Click **Apply** and wait for all services to build. Docker builds can take several minutes each.
4. Open each service and compare the URL at the top of its page with what you entered. Render may add
   a random suffix if a name is taken, for example `edge-cdn-router-x1y2.onrender.com`.
5. If any URL differs, fix the matching variable under **Environment**. Saving redeploys that service.
   `CDN_API_URL` is read at build time, so the static site must be redeployed if it changes.
6. Open the static site's URL. The first load can take a minute or more while services wake and the
   origin seeds MinIO. Then go through the checks in section 7.

The Blueprint generates `INTERNAL_TOKEN`, `MINIO_SECRET_KEY` and `CONTROL_TOKEN` for you. To find the
control token, open `edge-cdn-router → Environment → CONTROL_TOKEN`.

## 5. Deploy by hand (without the Blueprint)

Create the services **in this order**, so each one's URL exists before something needs it. If a later
URL isn't known yet, come back and set it afterwards.

### Settings for all 7 web services

| Setting | Value |
|---|---|
| New | **Web Service**, this repository, branch `main` |
| Language / Runtime | **Docker** |
| Region | Same for all, for example Oregon |
| Instance type | **Free** |
| Root Directory | *leave empty* |
| Docker Build Context Directory | `.` |
| Dockerfile Path | see table below |
| Docker Command | *leave empty* |
| Health Check Path | see table below |
| Auto-Deploy | On commit |

Leave Root Directory empty: the Dockerfiles copy `shared/`, so they build from the repository root.

| Name | Dockerfile Path | Health Check Path |
|---|---|---|
| `edge-cdn-minio` | `./services/minio/Dockerfile` | `/minio/health/live` |
| `edge-cdn-origin` | `./services/origin/Dockerfile` | `/healthz` |
| `edge-cdn-shield` | `./services/shield/Dockerfile` | `/healthz` |
| `edge-cdn-edge-1`, `-2`, `-3` | `./services/edge/Dockerfile` | `/healthz` |
| `edge-cdn-router` | `./services/router/Dockerfile` | `/healthz` |

Use `/healthz`, **not** `/health`. `/health` returns 503 during a simulated failure, which would make
Render restart the service and end the simulation.

### Shared variables

Create an **Environment Group** called `edge-cdn-shared` and link it to all 7 web services. If you
prefer not to use a group, add these to each service. The secret values must be identical everywhere.

| Key | Value | Notes |
|---|---|---|
| `PORT` | `10000` | Port each service listens on |
| `INTERNAL_TOKEN` | a long random string | Shared secret between services |
| `MINIO_ACCESS_KEY` | `cdnadmin` | Must not contain `=` or `,` |
| `MINIO_SECRET_KEY` | a long random string, 8+ characters | Used by minio and origin |
| `CONNECT_TIMEOUT_S` | `15` | Time to connect to another service |
| `UPSTREAM_TIMEOUT_S` | `90` | Long, so a sleeping service has time to wake |
| `HEALTH_TIMEOUT_S` | `20` | Router's health check timeout |
| `HEALTH_INTERVAL_S` | `5` | Time between router health checks |
| `ADMIN_TIMEOUT_S` | `20` | Timeout for metrics and control calls |

You can generate a random string with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### Variables for each service

**`edge-cdn-minio`**

| Key | Value |
|---|---|
| `MINIO_BROWSER` | `off` |
| `GOMEMLIMIT` | `350MiB` |

**`edge-cdn-origin`**

| Key | Value |
|---|---|
| `NODE_ID` | `origin` |
| `MINIO_ENDPOINT` | `edge-cdn-minio.onrender.com` (host only, no `https://`) |
| `MINIO_SECURE` | `true` |
| `MINIO_CONNECT_TIMEOUT_S` | `15` |
| `MINIO_READ_TIMEOUT_S` | `75` |
| `SEED_ARTICLES` | `24` |

**`edge-cdn-shield`**

| Key | Value |
|---|---|
| `NODE_ID` | `shield` |
| `ORIGIN_URL` | `https://edge-cdn-origin.onrender.com` |

**`edge-cdn-edge-1`, `edge-cdn-edge-2`, `edge-cdn-edge-3`** (same Dockerfile, different `NODE_ID`)

| Key | Value |
|---|---|
| `NODE_ID` | `edge-1`, `edge-2` or `edge-3` |
| `SHIELD_URL` | `https://edge-cdn-shield.onrender.com` |
| `ORIGIN_URL` | `https://edge-cdn-origin.onrender.com` |

**`edge-cdn-router`**

| Key | Value |
|---|---|
| `NODE_ID` | `router` |
| `EDGE_URLS` | `edge-1=https://edge-cdn-edge-1.onrender.com,edge-2=https://edge-cdn-edge-2.onrender.com,edge-3=https://edge-cdn-edge-3.onrender.com` |
| `SHIELD_URL` | `https://edge-cdn-shield.onrender.com` |
| `ORIGIN_URL` | `https://edge-cdn-origin.onrender.com` |
| `CONTROL_TOKEN` | a long random string; the dashboard will ask for it |
| `WEB_ORIGINS` | `https://edge-cdn-web.onrender.com` (no trailing slash) |

Don't set `REDIS_URL` on the edges or the shield. Leaving it empty starts their private Redis.

### Static site: `edge-cdn-web`

Choose **New → Static Site**, then:

| Setting | Value |
|---|---|
| Root Directory | `services/web` |
| Build Command | `printf 'window.CDN_API = "%s";' "$CDN_API_URL" > config.js` |
| Publish Directory | `.` |
| Environment variable | `CDN_API_URL` = `https://edge-cdn-router.onrender.com` (no trailing slash) |

## 6. All environment variables

| Variable | Used by | Default | Purpose |
|---|---|---|---|
| `PORT` | all web services | `8000` | Listening port |
| `NODE_ID` | all except minio | role name | Name in metrics and `X-Served-By` |
| `INTERNAL_TOKEN` | all except minio | empty (check off) | Secret between services |
| `CONTROL_TOKEN` | router | empty (check off) | Protects POST control API |
| `WEB_ORIGINS` | router | `*` | Browser origins allowed by CORS |
| `EDGE_URLS` | router | localhost | `name=url` pairs, comma separated |
| `SHIELD_URL` | router, edges | localhost | Shield base URL |
| `ORIGIN_URL` | router, edges, shield | localhost | Origin base URL |
| `REDIS_URL` | edges, shield | private Redis | Set only to use an external Redis |
| `REDIS_MAXMEMORY` | edges, shield | `64mb` | Limit of the private Redis |
| `MINIO_ENDPOINT` | origin | `localhost:9000` | MinIO host, without scheme |
| `MINIO_SECURE` | origin | `false` | `true` for HTTPS |
| `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` | origin, minio | `minioadmin` on origin | MinIO credentials |
| `MINIO_BUCKET` | origin | `newspaper` | Bucket name |
| `MINIO_CONNECT_TIMEOUT_S`, `MINIO_READ_TIMEOUT_S` | origin | `1`, `5` | MinIO timeouts |
| `MINIO_BROWSER` | minio | on | `off` disables the console (needed on Render) |
| `GOMEMLIMIT` | minio | none | Keeps MinIO inside the instance's memory |
| `SEED_ARTICLES` | origin | `60` | Articles seeded at startup |
| `CONNECT_TIMEOUT_S` | router, edges, shield | `1.5` | Connection timeout |
| `UPSTREAM_TIMEOUT_S` | router, edges, shield | `5` | Timeout for content requests |
| `HEALTH_TIMEOUT_S`, `HEALTH_INTERVAL_S` | router | `1`, `1` | Health checking |
| `ADMIN_TIMEOUT_S` | router | `5` | Metrics and control calls |
| `ORIGIN_POOL_SIZE` | shield | `4` | Parallel origin fetches |
| `BREAKER_FAILURE_THRESHOLD`, `BREAKER_RESET_SECONDS` | shield | `5`, `10` | Circuit breaker |
| `CDN_API_URL` | static site, at build | none | Router URL, written to `config.js` |

## 7. Check the deployment

Replace the URL below with your router's URL:

```bash
curl https://edge-cdn-router.onrender.com/health
```

You should see `"status":"ok"` and `"healthy_edges":3`. It may say `degraded` for a minute or two
while services wake. Then:

1. Open the static site. Articles and images should load.
2. Open **CDN dashboard**. All nodes should be UP.
3. Press **Start traffic**. The hit ratio should climb.
4. Press **Fail** on an edge and enter the control token when asked. Traffic should keep flowing.
5. Press **Restore all components**.

## 8. Free-plan limits

- **Sleep:** each web service sleeps after 15 minutes without inbound traffic and takes about a minute
  to wake. The router's health checks wake the others, and keep them awake while the router is awake.
- **Shared 750 hours a month:** the allowance is per workspace. Seven services awake together use 7
  hours per hour, or about 107 hours of everything running. Once it's used up, all free services are
  suspended until the next month. Leaving the dashboard open keeps everything awake and uses hours.
- **Data is lost on sleep:** there are no disks on the free plan. MinIO starts empty and the origin
  reloads the newspaper when it notices, so **published articles disappear**. Caches also start cold.
- **Bandwidth:** all traffic between services uses public URLs, so it may count toward your outbound
  allowance, and one image can cross several hops. Without a payment method, going over suspends all
  free services for the month. Run the full benchmark sparingly, and check usage in Render's dashboard.
- **Performance:** benchmark numbers on Render will be far worse than the ones in the README, because
  of the 0.1 CPU limit and HTTPS between services. Don't report them as the system's real performance.
- **Availability:** Render can restart free services at any time.

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser shows CORS errors | `WEB_ORIGINS` doesn't exactly match the static site's URL | Fix it; no trailing slash |
| Page stuck on "warming up" | Services waking, or origin seeding | Wait 2–3 minutes, then check `/health` |
| `edge-cdn-router/cache/status` shows `invalid internal token` | `INTERNAL_TOKEN` differs between services | Use one environment group |
| Origin shows `seed.error` with a signature or access error | MinIO keys differ, or `MINIO_ENDPOINT` includes `https://` | Fix the values on origin and minio |
| All edges DOWN | `EDGE_URLS` is wrong, or a service failed to deploy | Compare it with the real URLs; check each service's logs |
| 401 on dashboard buttons | Wrong control token | Reopen the tab and enter `CONTROL_TOKEN` again |
| Service restarts by itself during a "Fail" test | Health Check Path set to `/health` | Change it to `/healthz` |
| All services suspended | Hours or bandwidth used up | Wait for next month, or add a payment method |
| Out of memory in a service's logs | 512 MB exceeded | Lower `SEED_ARTICLES` or `REDIS_MAXMEMORY` |
| Static site still calls the wrong router | `CDN_API_URL` is read at build | Fix it, then redeploy the site |
