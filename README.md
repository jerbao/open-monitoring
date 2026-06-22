# open-monitoring

Public status page. Reads Prometheus, exposes filtered JSON at `/api/status`, serves static HTML at `/`. Async stack with 10s cache + stale-while-revalidate + single-flight. GitOps deploy via ArgoCD.

## Architecture

```
Internet
  ↓
Cloudflare (DDoS + 10s cache)
  ↓
HAProxy edge (k3s workers, SNI)
  ↓
┌───────────────────────────────────────────────────────────────┐
│ k3s cluster (FastAPI async backend, 3 replicas, 2 vCPU each)  │
│   ├─ /api/*  → queries Prometheus via http://192.168.88.62    │
│   └─ /*      → serves html/ via ConfigMap                     │
└───────────────────────────────────────────────────────────────┘
                                ↓ scrape /metrics
┌───────────────────────────────────────────────────────────────┐
│ VM .62 (192.168.88.62) — outside the kubernetes cluster       │
│   ├─ Prometheus + blackbox-exporter                           │
│   ├─ Authenticated helper (open-monitoring-auth, private repo)│
│   └─ UniFi Controller via helper                              │
└───────────────────────────────────────────────────────────────┘
```

**Why split like this?** Prometheus, blackbox, and the authenticated helper live on VM `.62` because they scrape internal IPs (Proxmox, kubelets) and the helper holds isolated credentials. The public backend runs on the k3s cluster to isolate from DDoS and scale independently — if the site is under attack, monitoring stays intact.

## Components

|                  Component                   |                                    Where                                |
|----------------------------------------------|-------------------------------------------------------------------------|
| FastAPI async backend (`/api/status` + `/`)  | k3s, 3 replicas | this repo (`open-monitoring`)                         |
| Static HTML frontend                         | k3s, same pod as backend | this repo                                    |
| Prometheus + blackbox | VM `.62`             | this repo                                                               |
| Authenticated helper (UniFi) | VM `.62`      | [`open-monitoring-auth`](https://github.com/jerbao/open-monitoring-auth)|

## Stack

- **Prometheus** — metrics collection (90d retention, 10GB max)
- **blackbox-exporter** — HTTP / ICMP / SSL probes
- **FastAPI async backend** — `httpx.AsyncClient` + `asyncio.gather`, 10s cache on `/api/status`, exposes only non-sensitive data
- **HAProxy** — edge, terminates TLS, SNI dispatch (`status.jerb.net` → backend)
- **certbot** — auto-renew every 12h
- **cert-manager + dns-01 Cloudflare** — TLS via k8s
- **ArgoCD** — GitOps, reconciles cluster with this repo
- **Cloudflare** — DDoS mitigation + edge cache (max-age=10)

## Deploy

### Cluster (via ArgoCD)

```bash
# Apply the Application
kubectl apply -f argocd/status-app.yaml

# Force initial sync
argocd app sync status-app

# Verify
kubectl -n monitoring get pods,svc
kubectl -n monitoring logs -l app=status-app
```

ArgoCD reads `k8s/app/` from this repo, generates ConfigMaps (probes.yaml + html/), and keeps the deployment synced. Self-heal + prune enabled. HPA scales up aggressively (doubles replicas every 30s after 20s stabilization, triggers at mean CPU > 60%) and scales down conservatively (removes 10% every 60s after 5min stable).

### Image

Automated build in `.github/workflows/build.yaml`:
- Push on `main` → GHCR (`ghcr.io/jerbao/open-monitoring-backend`)
- Tags `latest` + commit sha
- `context: .` at repo root, `dockerfile: backend/Dockerfile`

### VM .62 (Prometheus + helper)

```bash
ssh ubuntu@192.168.88.62
cd ~/open-monitoring && git pull && docker compose up -d --build
cd ~/open-monitoring-auth && git pull && docker compose up -d --build
```

## Endpoints

| Route | Response |
|---|---|
| `GET /api/status` | Filtered JSON, cached 10s (stale-while-revalidate), `Cache-Control: public, max-age=10` header |
| `GET /` | Static HTML |
| `GET /healthz` | `{"status":"ok"}` |

`/api/status` is `async def` using `httpx.AsyncClient` + `asyncio.gather`: for each probe fires 1 status + 4 uptime windows + 1 ssl in parallel. Shared cache via `asyncio.Lock` (single-flight per worker).

## Active probes

| Group | Target | Probe |
|---|---|---|
| Sites | `chat.jerb.net` | HTTP+SSL |
| Sites | `pharmaervas.com.br`, `www.pharmaervas.com.br` | HTTP+SSL |
| Cluster k3s | `192.168.88.51`–`.58` (3 masters + 5 workers) | HTTP kubelet `/healthz` |
| Proxmox VE | `192.168.88.40`–`.45` (5 PVE + 1 PBS) | HTTP API version |
| Network | UniFi Controller (via authenticated helper) | custom metric |

Display order on the page (most critical to highest): Proxmox VE → Cluster k3s → Network → Sites.

## Useful commands

```bash
# Cluster
kubectl -n monitoring get pods,svc,cm
kubectl -n monitoring logs -l app=status-app -f
kubectl -n monitoring rollout restart deployment status-app

# ArgoCD
argocd app list
argocd app history status-app

# VM .62
ssh ubuntu@192.168.88.62
cd ~/open-monitoring && docker compose ps
cd ~/open-monitoring-auth && docker compose ps && curl -s http://127.0.0.1:9000/metrics | grep unifi

# Prometheus direct
curl -s 'http://192.168.88.62:9090/api/v1/query?query=unifi_controller_up' | jq .
```

## Adding a new probe

1. Add target in `prometheus/prometheus.yml` (job blackbox_*)
2. Add entry in `backend/probes.yaml`
3. Mirror to `k8s/app/configmap-probes.yaml` (ArgoCD sync ships to cluster)
4. `kustomize build k8s/app/` and apply (or wait for ArgoCD sync — `configMapGenerator` changes hash and forces rollout)

## Not exposed (privacy)

- Exact latency
- Internal Prometheus labels (`job`, `__address__`, raw `instance`)
- Detailed topology (worker counts, specific IPs beyond the mapped ones)
- HTTP codes / headers
- Credentials (never leave the private helper)
