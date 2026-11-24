"""The Developer API documentation payload (founder priority #16).

Built from the public router's own catalogue (api_public
.PUBLIC_ROUTES) and cross-checked against its live route table:
every documented endpoint must resolve to a registered route, or
building the docs fails loudly instead of documenting a fiction.
Curl examples are generated from the same method + path data, so
an example can only ever name a real route.
"""

from __future__ import annotations

from services import api_keys

_EXAMPLE_BODIES = {
    ("POST", "/api/public/v1/projects"): (
        '{"title": "My documentary", "script": "Narration text...", '
        '"category": "Documentary"}'
    ),
}


def _example_path(path: str) -> str:
    return path.replace("{project_id}", "PROJECT_ID").replace(
        "{run_id}", "RUN_ID"
    )


def _curl(method: str, path: str) -> str:
    url = f'"$CINEMORA_API_BASE{_example_path(path)}"'
    parts = ["curl"]
    if method != "GET":
        parts.append(f"-X {method}")
    parts.append('-H "X-API-Key: $CINEMORA_API_KEY"')
    body = _EXAMPLE_BODIES.get((method, path))
    if body is not None:
        parts.append('-H "Content-Type: application/json"')
        parts.append(f"-d '{body}'")
    parts.append(url)
    return " ".join(parts)


def build_docs() -> dict:
    import api_public

    live = {
        (method, api_public.BASE_PATH + route.path)
        for route in api_public.router.routes
        for method in (getattr(route, "methods", None) or set())
    }
    endpoints = []
    for entry in api_public.PUBLIC_ROUTES:
        if (entry["method"], entry["path"]) not in live:
            raise RuntimeError(
                "Developer docs catalogue names a route that is not "
                f"registered: {entry['method']} {entry['path']}"
            )
        endpoints.append(
            {
                **entry,
                "curl": _curl(entry["method"], entry["path"]),
            }
        )
    return {
        "base_path": api_public.BASE_PATH,
        "auth": {
            "header": "X-API-Key",
            "format": "X-API-Key: cmk_<secret>",
            "notes": (
                "API keys are created in the product under "
                "Developers. The secret is shown once at creation; "
                "only its hash is stored. Keys authenticate ONLY "
                "on /api/public/v1 — sessions and the operator "
                "token do not work there, and keys do not work "
                "anywhere else."
            ),
        },
        "scopes": [
            {
                "name": "read",
                "description": "Read projects, runs, outputs and "
                "reports, and the key's own usage.",
            },
            {
                "name": "write",
                "description": "Create projects, submit runs and "
                "request run cancellations.",
            },
        ],
        "rate_limit": {
            "default_per_minute": api_keys.default_rate_limit(),
            "window_seconds": 60,
            "per_key_override": (
                "A key may carry its own per-minute limit, set at "
                "creation and shown on the key."
            ),
            "overage": (
                "Requests beyond the budget fail with HTTP 429 "
                "and error code 'rate_limited'."
            ),
        },
        "errors": {
            "shape": {"error": {"code": "...", "message": "..."}},
            "codes": [
                "unauthorized (401): missing, invalid, revoked or "
                "expired key",
                "insufficient_scope (403): the key lacks the "
                "route's scope",
                "rate_limited (429): the key's per-minute budget "
                "is exhausted",
                "not_found-style 404s: another user's project or "
                "run is indistinguishable from a missing one",
            ],
        },
        "endpoints": endpoints,
        "webhooks": {
            "events": ["run.completed", "run.failed"],
            "headers": {
                "X-Cinemora-Event": "The event name.",
                "X-Cinemora-Delivery": "The delivery group id; "
                "retries of one event share it.",
                "X-Cinemora-Signature": "sha256=<hex HMAC>",
            },
            "signature": (
                "signature = HMAC_SHA256(signing_key, raw_body), "
                "hex-encoded, where signing_key is the SHA-256 hex "
                "of the signing secret shown once when the webhook "
                "was registered. Only the hash is stored server-"
                "side; recompute it from your secret to verify."
            ),
            "retries": (
                "Up to 3 attempts per event; transport errors and "
                "5xx responses retry, other non-2xx outcomes are "
                "final. Every attempt is recorded and listed under "
                "the endpoint's deliveries."
            ),
            "payload_example": {
                "id": "DELIVERY_ID",
                "event": "run.completed",
                "created_at": "2026-11-24T00:00:00+00:00",
                "data": {
                    "run_id": "RUN_ID",
                    "project_id": "PROJECT_ID",
                    "status": "completed",
                    "finished_at": "2026-11-24T00:00:00+00:00",
                },
            },
        },
    }
