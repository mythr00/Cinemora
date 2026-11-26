# SearXNG for Cinemora — deployment runbook

Master Implementation (2026-11-26), Part 1: SearXNG is the
platform's primary search provider; Serper remains the fallback.
The backend reaches SearXNG over the internal compose network —
there is no API key and no public exposure.

## What runs where

- `docker-compose.yml` (repo root) defines a `searxng` service
  from the official `searxng/searxng` image. It publishes **no
  port** (`expose` only), mounts `deploy/searxng/settings.yml`,
  restarts unless stopped, and health-checks `/healthz`.
- The backend env anchor sets
  `SEARXNG_URL: ${SEARXNG_URL:-http://searxng:8888}` — override
  only to point at an instance hosted elsewhere.
- Provider selection is config-driven
  (`CINEMORA_SEARCH_PROVIDER` = auto/searxng/serper,
  `CINEMORA_SEARCH_FALLBACK` = 1/0; see `backend/.env.example`).

## Validated version and measured footprint

Validated on the development VM (2026-11-26) against SearXNG
**2026.11.23+a7163d0** (pip install), serving the backend's real
query classes (general / images / videos):

- Idle RSS ≈ **186 MB**; burst peak RSS ≈ **193 MB**
  (10 concurrent searches).
- CPU is negligible (the workload is I/O-bound on upstream
  engines): ≈0.8 CPU-seconds across a 10-search burst.
- The compose service is therefore limited to 512 MB / 1 CPU,
  which carries generous headroom. On a production host, budget
  **~256 MB RAM and a fraction of one core** for the instance;
  it coexists comfortably with the backend on a small VPS.
- Pin the image tag in production: the compose file tracks
  `latest` so a fresh checkout runs; once pulled, pin the dated
  tag matching the validated version and record it here.

## First deployment

1. Generate the secret and put it in `settings.yml`
   (`secret_key`): `python3 -c "import secrets; print(secrets.token_hex(32))"`.
2. `docker compose up -d searxng backend worker`
3. Verify from inside the network:
   `docker compose exec backend curl -s http://searxng:8888/healthz`
   → `OK`. A JSON smoke search:
   `... /search?q=test&format=json&categories=general`.
4. The backend logs the active routing at first search
   (`search_provider` events) and records every fallback; the
   admin search stats show per-provider call counts.

## Operations

- **Health**: `/healthz` returns `OK`. The backend also probes
  it (cached 30 s) for its own search-health view, and a failed
  SearXNG trips a 60 s circuit breaker that routes straight to
  Serper until the instance recovers — productions keep working
  with SearXNG down, at Serper prices.
- **Engines**: upstream engines occasionally rate-limit or
  CAPTCHA a host (observed during validation: Brave throttled,
  DuckDuckGo CAPTCHA from a shared egress IP). SearXNG aggregates
  many engines, so individual suspensions degrade results rather
  than break them; the backend's Serper fallback covers the rest.
  If one engine is persistently suspended on the production host,
  disable it in `settings.yml` under `engines:`.
- **Updates**: bump the image, `docker compose up -d searxng`,
  re-run the smoke search. Settings are stateless (no volume) —
  the file in this directory is the whole configuration.
- **Savings accounting**: SearXNG searches are recorded in the
  provider-usage ledger at $0 marginal cost with a seeded
  pricing rule; the instance's infrastructure cost is attributed
  in the profitability report from the measured footprint above,
  never folded into a per-search price.
