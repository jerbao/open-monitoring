"""
open-monitoring — FastAPI backend that filters the Prometheus API and exposes
only non-sensitive data to the public status page.

Endpoints:
  GET /api/status    — filtered JSON for the page
  GET /healthz       — health check (used by HAProxy)
  /*                 — serves static files from /app/static (HTML/CSS/JS)
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
PROBES_FILE = os.environ.get("PROBES_FILE", "/app/probes.yaml")
STATIC_DIR = os.environ.get("STATIC_DIR", "/app/static")
REQUEST_TIMEOUT = 10.0

# Keep-alive connection pool: a single connection multiplexes multiple
# concurrent queries to Prometheus without per-request handshake cost.
_httpx_limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Manages the async HTTP client lifecycle."""
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, limits=_httpx_limits, http2=False
    ) as client:
        _app.state.http = client
        yield


app = FastAPI(title="open-monitoring", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------- Helpers ----------


def load_probes() -> dict[str, dict[str, str]]:
    """Carrega mapa {instance_label: {name, group}} de probes.yaml."""
    try:
        with open(PROBES_FILE, "r") as f:
            data = yaml.safe_load(f) or {}
        return data.get("probes", {})
    except FileNotFoundError:
        return {}


async def prom_query(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    """Faz query instant na API do Prometheus e retorna a lista de resultados."""
    url = f"{PROMETHEUS_URL}/api/v1/query"
    try:
        r = await client.get(url, params={"query": query})
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "success":
            return []
        return data.get("data", {}).get("result", [])
    except httpx.HTTPError, ValueError:
        return []


def _status_metric(instance: str) -> str:
    """Prometheus metric indicating up/down for the given service."""
    if instance == "unifi-controller":
        return "unifi_controller_up"
    return "probe_success"


async def uptime_pct(client: httpx.AsyncClient, instance: str, duration: str) -> float | None:
    """Calcula uptime % via avg_over_time(metric[duration])."""
    metric = _status_metric(instance)
    query = f'avg_over_time({metric}{{instance="{instance}"}}[{duration}])'
    result = await prom_query(client, query)
    if not result:
        return None
    try:
        return round(float(result[0]["value"][1]) * 100, 2)
    except KeyError, ValueError, IndexError:
        return None


async def ssl_days_remaining(client: httpx.AsyncClient, instance: str) -> int | None:
    """Days remaining until the oldest SSL cert expires."""
    query = f'probe_ssl_earliest_cert_expiry{{instance="{instance}"}} - time()'
    result = await prom_query(client, query)
    if not result:
        return None
    try:
        seconds = float(result[0]["value"][1])
        if seconds <= 0:
            return 0
        return int(seconds / 86400)
    except KeyError, ValueError, IndexError:
        return None


async def is_up(client: httpx.AsyncClient, instance: str) -> str:
    """Verifica status atual (probe_success==1 ou unifi_controller_up==1)."""
    metric = _status_metric(instance)
    query = f'{metric}{{instance="{instance}"}}'
    result = await prom_query(client, query)
    if not result:
        return "unknown"
    try:
        return "up" if float(result[0]["value"][1]) == 1 else "down"
    except KeyError, ValueError, IndexError:
        return "unknown"


async def fetch_unifi_details(client: httpx.AsyncClient) -> dict[str, Any]:
    """Reads UniFi metrics (devices/APs/gateway) directly from Prometheus."""
    details: dict[str, Any] = {}
    results = await asyncio.gather(
        *[
            prom_query(client, f'{metric}{{instance="unifi-controller"}}')
            for _, metric in [
                ("devices_online", "unifi_devices_online"),
                ("aps_online", "unifi_aps_online"),
                ("gateway_up", "unifi_gateway_up"),
            ]
        ]
    )
    for (key, _), result in zip(
        [
            ("devices_online", "unifi_devices_online"),
            ("aps_online", "unifi_aps_online"),
            ("gateway_up", "unifi_gateway_up"),
        ],
        results,
    ):
        if result:
            try:
                details[key] = float(result[0]["value"][1])
            except KeyError, ValueError, IndexError:
                pass
    return details


# ---------- Main endpoint ----------

# Display order for groups: from most critical (bottom) to highest (apps)
GROUP_PRIORITY = {
    "Proxmox VE": 1,  # hypervisor — if it falls, everything falls
    "Cluster k3s": 2,  # dependency: runs on top of Proxmox
    "Network": 3,  # switches/APs/gateway — dependency: access to services
    "Sites": 4,  # dependency: apps in the cluster
}


async def _build_status(client: httpx.AsyncClient) -> dict[str, Any]:
    """Builds the full payload. Cached 10s to avoid amplifying load on Prom.

    Prometheus queries are dispatched in parallel via asyncio.gather: for
    each probe, 4 uptimes + 1 status + 1 ssl (if applicable) run in parallel,
    and UniFi details also in parallel. Reduces payload latency from
    N*sum(avg_query_time) to ~max(avg_query_time).
    """
    probes = load_probes()

    async def _entry_for(instance: str, meta: dict[str, str]) -> dict[str, Any]:
        status, *uptimes, ssl = await asyncio.gather(
            is_up(client, instance),
            uptime_pct(client, instance, "24h"),
            uptime_pct(client, instance, "7d"),
            uptime_pct(client, instance, "30d"),
            uptime_pct(client, instance, "90d"),
            ssl_days_remaining(client, instance)
            if meta.get("check_ssl")
            else asyncio.sleep(0, result=None),
        )
        entry: dict[str, Any] = {
            "name": meta.get("name", instance),
            "group": meta.get("group", "Outros"),
            "status": status,
            "uptime": {
                "24h": uptimes[0],
                "7d": uptimes[1],
                "30d": uptimes[2],
                "90d": uptimes[3],
            },
            "ssl_days": ssl,
        }
        if instance == "unifi-controller":
            details = await fetch_unifi_details(client)
            if details:
                entry["details"] = details
        return entry

    services = await asyncio.gather(
        *(_entry_for(instance, meta) for instance, meta in probes.items())
    )
    services = list(services)
    services.sort(key=lambda s: (GROUP_PRIORITY.get(s["group"], 99), s["name"]))
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "services": services,
    }


# Cache with TTL + stale-while-revalidate + single-flight (async version).
# Solves the "cache stampede": when the cache expires with N concurrent
# requests, only the first triggers a refresh; the others receive the stale
# value and trigger a background refresh.
#
# Behavior:
#   - cache valid: returns immediately (1 Prometheus worker per TTL)
#   - cache expired + stale exists: returns stale, fires async refresh
#   - cache empty (first request): blocks and refreshes synchronously (once)
_CACHE_TTL_SEC = 10.0
_CACHE_BACKOFF_SEC = 1.0
_cache_payload: dict[str, Any] | None = None
_cache_expires: float = 0.0
_cache_lock = asyncio.Lock()
_cache_refreshing: bool = False


async def _refresh_cache(client: httpx.AsyncClient) -> None:
    """Recomputes the payload and updates the cache. Exception-safe:
    always resets _cache_refreshing in finally; on error keeps the stale
    payload and applies a short backoff for the next attempt."""
    global _cache_payload, _cache_expires, _cache_refreshing
    try:
        _cache_payload = await _build_status(client)
        _cache_expires = time.monotonic() + _CACHE_TTL_SEC
    except Exception:
        # Keep previous _cache_payload (stale) and schedule a short backoff.
        _cache_expires = time.monotonic() + _CACHE_BACKOFF_SEC
    finally:
        _cache_refreshing = False


@app.get("/api/status", response_model=None)
async def get_status(request: Request) -> JSONResponse:
    global _cache_payload, _cache_expires, _cache_refreshing
    client = request.app.state.http
    now = time.monotonic()

    # Fast path: valid cache, no lock
    if _cache_payload is not None and now < _cache_expires:
        return JSONResponse(
            content=_cache_payload,
            headers={"Cache-Control": f"public, max-age={int(_CACHE_TTL_SEC)}"},
        )

    # Cache expired or empty: needs lock
    async with _cache_lock:
        # Re-check inside lock (double-check)
        if _cache_payload is not None and now < _cache_expires:
            return JSONResponse(
                content=_cache_payload,
                headers={"Cache-Control": f"public, max-age={int(_CACHE_TTL_SEC)}"},
            )

        if _cache_payload is None:
            # First request: block and refresh synchronously
            await _refresh_cache(client)
        elif not _cache_refreshing:
            # Stale exists, nobody refreshing: fire async
            _cache_refreshing = True
            asyncio.create_task(_refresh_cache(client))
        # Else: refresh already in progress, return stale

        return JSONResponse(
            content=_cache_payload,
            headers={"Cache-Control": f"public, max-age={int(_CACHE_TTL_SEC)}"},
        )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# ---------- Static files (HTML/CSS/JS for the status page) ----------

# Mount static at root; must be AFTER the /api/* and /healthz routes
# because FastAPI resolves routes in declaration order.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
