# open-monitoring

Status page. Reads Prometheus, exposes filtered JSON at `/api/status`, serves static HTML at `/`. Async stack, GitOps via ArgoCD, multi-stage CI with lint+type-check+build.

---

## Architecture

```
Internet
  ↓
Cloudflare (DDoS + 10s edge cache)
  ↓
HAProxy edge (k3s workers, SNI: status.jerb.net → backend ClusterIP:8000)
  ↓
┌──────────────────────────────────────────────────────────────┐
│ k3s cluster (FastAPI async backend, 3 replicas, 2 vCPU each) │
│   ├─ /api/*  → queries Prometheus via http://192.168.88.62   │
│   └─ /*      → serves html/ via ConfigMap                    │
└──────────────────────────────────────────────────────────────┘
                                ↓ scrape /metrics
┌──────────────────────────────────────────────────────────────┐
│ VM .62 (192.168.88.62) — outside kubernetes                  │
│   ├─ Prometheus + blackbox-exporter                          │
│   ├─ Authenticated helper (open-monitoring-auth, private)    │
│   └─ UniFi Controller via helper                             │
└──────────────────────────────────────────────────────────────┘
```

**Why split:** Prometheus/blackbox/helper live on VM `.62` because they scrape internal IPs (Proxmox, kubelets) and the helper holds isolated credentials. Public backend on k3s isolates from DDoS and scales independently.

---

## Repository layout

```
.
├── README.md
├── CLAUDE.md                          # this file
├── .gitignore
├── docker-compose.yml                 # local stack (prometheus + blackbox + backend)
├── argocd/
│   └── status-app.yaml                # ArgoCD Application
├── .github/workflows/
│   └── build.yaml                     # CI: ruff + basedpyright + docker build
├── backend/                           # FastAPI async service
│   ├── app.py
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── probes.yaml
│   └── html/                          # (none, served from k8s/app/html via ConfigMap)
├── html/                              # frontend source (dev mirror)
│   ├── index.html
│   ├── app.js
│   ├── styles.css
│   └── i18n.js
├── k8s/app/                           # cluster manifests
│   ├── kustomization.yaml
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── hpa.yaml
│   ├── configmap-probes.yaml
│   └── html/                          # copy of ../html (kustomize restriction)
├── prometheus/
│   └── prometheus.yml                 # scrape config (prod)
└── blackbox/
    └── blackbox.yml                   # blackbox modules
```

---

## Backend (`backend/`)

### Stack
- **Python 3.14** (slim), `requires-python = ">=3.14"`.
- **FastAPI 0.138.0** async + **uvicorn 0.49.0** with `uvloop` + `httptools`.
- **httpx 0.28.1** (`AsyncClient`, keep-alive pool: 50 connections / 20 keep-alive, timeout 10s, HTTP/1.1).
- **pyyaml 6.0.3** for `probes.yaml`.
- CORS: `allow_origins=["*"]`, `allow_methods=["GET"]` only.
- Single `httpx.AsyncClient` managed via `lifespan` in `app.state.http`.

### Endpoints
| Route | Method | Behavior |
|---|---|---|
| `/api/status` | GET | Aggregated payload. `Cache-Control: public, max-age=10`. |
| `/healthz` | GET | `{"status":"ok"}` for HAProxy. |
| `/` | GET | `StaticFiles(directory=STATIC_DIR, html=True)`. Declared last (order matters). |

`/api/status` payload:
```json
{
  "updated_at": "<ISO8601 UTC>",
  "services": [
    {
      "name": "...",
      "group": "Proxmox VE|Cluster k3s|Network|Sites",
      "status": "up|down|unknown",
      "uptime": { "24h": 99.95, "7d": ..., "30d": ..., "90d": ... },
      "ssl_days": 47,
      "details": { "devices_online": 12, "aps_online": 8, "gateway_up": 1 }  // unifi-controller only
    }
  ]
}
```
Services sorted by `GROUP_PRIORITY` (Proxmox→Cluster→Network→Sites, then alphabetical).

### Cache: single-flight + stale-while-revalidate
Module globals: `_cache_payload`, `_cache_expires`, `_cache_lock = asyncio.Lock()`, `_cache_refreshing: bool`. TTL = 10s, backoff = 1s.

Flow in `get_status`:
1. **Fast path** (no lock): payload exists and `now < _cache_expires` → return.
2. Otherwise grab `asyncio.Lock`, **re-check** (double-check).
3. **Cold start** (`_cache_payload is None`): `await _refresh_cache()` synchronously.
4. **Stale + nobody refreshing**: set `_cache_refreshing=True`, fire `asyncio.create_task(_refresh_cache(client))`, return stale immediately.
5. **Stale + refresh in flight**: return stale.
6. `_refresh_cache` is exception-safe: on error, keep previous payload, lower TTL to backoff (1s), `finally` always clears `_cache_refreshing`.

### Prometheus I/O
`prom_query` does `GET {PROMETHEUS_URL}/api/v1/query?query=...`. Failures (`httpx.HTTPError`, `ValueError`, status≠`success`) → `[]` (silent fallback).

Helpers:
- `is_up` → `probe_success{instance=...} == 1` (or `unifi_controller_up` for unifi-controller).
- `uptime_pct` → `avg_over_time(metric[duration]) * 100`. Durations: 24h/7d/30d/90d.
- `ssl_days_remaining` → `(probe_ssl_earliest_cert_expiry - time()) / 86400`; ≤0 → 0.
- `fetch_unifi_details` → 3 parallel queries via `asyncio.gather` (devices/APs/gateway).

Parallelism in `_build_status`:
- **Per probe**: `asyncio.gather(is_up, uptime*4, ssl?)` — 6 simultaneous (5 if no `check_ssl`).
- **Across probes**: `asyncio.gather(*(_entry_for(...)))` for all services.
- **UniFi**: sequential `await fetch_unifi_details(client)` inside `_entry_for` after the gather (adds latency for unifi-controller only).

### Environment variables
| Var | Default | Function |
|---|---|---|
| `PROMETHEUS_URL` | `http://prometheus:9090` | Prometheus API base |
| `PROBES_FILE` | `/app/probes.yaml` | instance→{name, group, check_ssl?} map |
| `STATIC_DIR` | `/app/static` | StaticFiles root |
| `REQUEST_TIMEOUT` | **10.0 (constant, not from env)** | httpx timeout |

### Build & runtime
- `python:3.14-slim`, non-root user `app`, `WORKDIR /app`.
- Build: `COPY backend/pyproject.toml` → `pip install --no-cache-dir .` (package install; `py-modules=["app"]`).
- Runtime: copy `app.py` + `probes.yaml`; create empty `/app/static` (k8s mounts ConfigMap with frontend).
- `CMD uvicorn app:app --host 0.0.0.0 --port 8000 --workers 2 --loop uvloop --http httptools --backlog 4096`.

### Dependencies
- **Runtime** (all pinned): `fastapi==0.138.0`, `uvicorn[standard]==0.49.0`, `httpx==0.28.1`, `pyyaml==6.0.3`, `uvloop==0.22.1`.
- **Dev** (`[project.optional-dependencies].dev`): `ruff==0.15.19`, `basedpyright==1.39.8`, `uv==0.11.24`.
- **Tool config**: `[tool.ruff] line-length=100, target-version=py314`; `[tool.basedpyright] pythonVersion="3.14", typeCheckingMode="standard"`.
- `uv.lock` (rev 3) pins all transitive deps.

---

## Frontend (`html/`)

- **100% client-side rendering.** No SSR/Jinja. `app.js` does `fetch('/api/status', {cache:'no-store'})` and populates `#status-body`.
- **Polling**: 60s `setInterval`. No WebSocket/SSE.
- **Languages**: pt (default) + en, via `data-i18n` attributes and `i18n.js` dictionary (no i18next). Persisted in `localStorage`. Detected from `navigator.language` prefix.
- **Theming**: dark terminal, JetBrains Mono. CSS vars: `--bg #0a0e14`, `--green #5fe6c4`, `--red #ff6b8b`, `--amber #ffb86c`.
- **Status display**: `.dot-status.up/down/unknown`; uptime thresholds (≥99.9 ok, ≥99.0 warn, else bad); SSL thresholds (≤0 expired, ≤14 bad, ≤30 warn).
- **Responsive**: `@media (max-width: 720px)` mobile layout with `flex-wrap` nav and horizontal-scroll table.

### `html/` vs `k8s/app/html/`
**Identical content**. ArgoCD serves the frontend via ConfigMap generated by kustomize from `k8s/app/html/`, mounted on the backend pod; `html/` is the dev mirror. **Any edit in `html/` must be copied to `k8s/app/html/`** (kustomize rejects files outside its root, and symlinks are also rejected). The kustomization generates a configMap with a content hash so the pod rolls on html changes.

---

## Local development (`docker-compose.yml`)

3 services on bridge `monitor`:
- **prometheus** (v3.4.0, retention 90d/10GB, `127.0.0.1:9090`).
- **blackbox** (v0.28.0, NET_RAW cap for ICMP, `127.0.0.1:9115`).
- **backend** (build `./backend/`, `127.0.0.1:8000`).

`extra_hosts: host.docker.internal:host-gateway` lets Prometheus reach the UniFi helper on the host. Prometheus bind-mounts `./prometheus` and `./blackbox`; backend bind-mounts `./html` to `/app/static`.

---

## Production deployment

### ArgoCD (`argocd/status-app.yaml`)
- `repoURL: https://github.com/jerbao/open-monitoring.git`
- `targetRevision: HEAD`, `path: k8s/app`
- `destination: namespace monitoring` (in-cluster)
- `syncPolicy.automated: {prune: true, selfHeal: true}`
- `CreateNamespace=true`, retry: limit 5, backoff 10s→factor 2→max 5m

### Manifests (`k8s/app/`)
- **deployment.yaml** — `status-app`, namespace `monitoring`, 3 replicas, `RollingUpdate maxSurge:1 maxUnavailable:0`. Container port 8000. Env: `PROMETHEUS_URL=http://192.168.88.62:9090`, `STATIC_DIR=/app/static`. Resources: req 700m/160Mi, **lim 2 vCPU / 200Mi**. Probes `/healthz` (liveness 15s/10s, readiness 2s/5s). Volumes: `probes` (ConfigMap `open-monitoring-probes` → `/app/probes.yaml`) and `html` (ConfigMap `open-monitoring-html` → `/app/static`).
- **service.yaml** — ClusterIP port 8000 → `targetPort: http`.
- **hpa.yaml** — autoscaling/v2, min 3 / max 10, CPU 60%. scaleUp 100%/30s (window 20s). scaleDown 10%/60s (window 300s) — anti-flapping.
- **configmap-probes.yaml** — embeds `probes.yaml` (Sites HTTPS, k3s kubelet :10250/healthz, Proxmox API, UniFi sentinel).
- **kustomization.yaml** — `resources`, `configMapGenerator` (html files), `namespace: monitoring`.

### CI (`.github/workflows/build.yaml`)
Trigger: push to `main` with paths `['backend/**','html/**','k8s/**','.github/workflows/**']` + manual `workflow_dispatch`. Permissions: `contents:read, packages:write`.

Jobs:
1. **checkup-format** — `docker run ghcr.io/astral-sh/uv:python3.14-trixie-slim sh -c "uv sync --locked --all-extras && uv run ruff format --check"`.
2. **type-check** — same container, `uv run basedpyright --project pyproject.toml`.
3. **backend** (`needs: [checkup-format, type-check]`) — `docker/build-push-action@v6` with `context: .`, `file: ./backend/Dockerfile`, push to GHCR (`latest` + `${{ github.sha }}`), GHA cache.

**CI does not deploy.** ArgoCD detects HEAD via `targetRevision: HEAD`; sync is automatic on merge.

### Prometheus & Blackbox (on VM `.62`)
- `prometheus/prometheus.yml` — jobs: `prometheus` (self), `unifi_helper` (`host.docker.internal:9000/metrics`), `blackbox_http_sites` (https+ssl), `blackbox_icmp_workers` (.54–.58), `blackbox_kubelet_healthz` (k3s masters+workers), `blackbox_http_proxmox` (self-signed). Scrape interval 30s.
- `blackbox/blackbox.yml` — modules: `http_2xx`, `http_2xx_with_ssl` (HTTPS public), `http_2xx_no_ssl_verify` (Proxmox self-signed, accepts 401), `icmp`, `tcp_connect`, `k3s_healthz`, `kubelet_healthz` (accepts 200/401), `etcd_health`.

---

## Adding a new probe

1. `prometheus/prometheus.yml` — add target under the right `blackbox_*` job.
2. `backend/probes.yaml` — add entry `{url: {name, group, check_ssl?}}`.
3. `k8s/app/configmap-probes.yaml` — mirror the same entry (manually; CI doesn't sync them).
4. `kustomize build k8s/app/` (or wait for ArgoCD; `configMapGenerator` hash change forces rollout).

If you edited `html/`, **also copy to `k8s/app/html/`** so the ConfigMap ships the change.

---

## Gotchas

- **Cache is per-worker.** `--workers 2` ⇒ 2 independent caches. With `--workers 4` you'd see up to 4 parallel Prometheus fetches per TTL expiry. Each worker process has its own `asyncio.Lock`. Single-flight deduplication is intra-process only.
- **`REQUEST_TIMEOUT` is hardcoded** (10.0) — the env var with the same name is not read.
- **Silent fallbacks.** `prom_query` returns `[]` on any network/JSON error; the frontend receives `null`/unknown without indicating the failure.
- **UniFi sequential fetch.** `_entry_for` does `await fetch_unifi_details(client)` after the initial gather — adds latency for unifi-controller only.
- **`load_probes()` re-reads YAML on every `_build_status`** (no cache; file is small, cost is negligible).
- **Path filter on workflow** — pushes that don't touch `backend/`, `html/`, `k8s/`, or `.github/workflows/` are ignored. Editing `argocd/` or `prometheus/` won't trigger CI.
- **Build context is `.`** (repo root). Dockerfile is at `./backend/Dockerfile`; all `COPY` paths in it use `backend/...` prefix.
- **kustomize rejects paths outside its root.** Symlinks are also rejected (resolved before path check). Files referenced by `configMapGenerator.files` must be inside `k8s/app/`.
- **`kustomization.yaml` top-level keys** must be `resources`, `namespace`, etc. directly — NOT wrapped in `kustomize:` (causes `unknown field "kustomize"`).
- **configmap-probes drift.** No CI sync between `backend/probes.yaml` and `k8s/app/configmap-probes.yaml`; keep them in sync manually.
- **No Ingress in `k8s/app/`.** Exposure happens via HAProxy on the workers (SNI → backend ClusterIP:8000).
- **Python 3.14 required.** Still bleeding-edge for many environments.
- **`ruff format --check`** may suggest removing parentheses in `except (Foo, Bar):` — this is **valid Python 3.14 syntax** (PEP 758), not a bug.

---

## Quick reference

| Task | Command |
|---|---|
| Build backend image | `docker build -f backend/Dockerfile -t open-monitoring-backend:test .` |
| Verify CI locally | `cd backend && uv sync --locked --all-extras && uv run ruff format --check && uv run basedpyright` |
| Render k8s manifests | `kustomize build k8s/app/` (or via Docker: `docker run --rm -v "$PWD:/src" -w /src kustomize/kustomize build /src/k8s/app`) |
| Apply ArgoCD app | `kubectl apply -f argocd/status-app.yaml` |
| Force ArgoCD sync | `argocd app sync status-app` |
| Tail backend logs | `kubectl -n monitoring logs -l app=status-app -f` |
| Test backend locally | `docker compose -f docker-compose.yml up -d` then `curl http://127.0.0.1:8000/api/status` |
