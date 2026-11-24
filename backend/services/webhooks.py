"""Run-lifecycle webhooks (founder priority #16; gap-audit doc 06
Phase N: "webhooks for run completion").

Registration is owner-scoped. The signing secret is shown once at
creation; only its SHA-256 is stored. Deliveries are signed with
HMAC-SHA256 keyed by that stored hash (hex string, UTF-8):

    signing_key = sha256_hex(webhook_secret)          # stored
    signature   = HMAC_SHA256(signing_key, raw_body)  # hex

sent as `X-Cinemora-Signature: sha256=<signature>`. A receiver
verifies by hashing the secret it was shown once and recomputing
the HMAC — so the plaintext secret never has to persist on the
server, while both sides can still verify.

Delivery discipline:

- The receiver URL is re-validated at delivery time through the
  Assistant fetcher's SSRF guard (services/assistant/fetcher.py):
  http/https only, standard ports, host must resolve exclusively
  to public addresses. Redirects are never followed.
- Up to 3 attempts per event (transport errors and 5xx retry;
  other statuses are final). Every attempt is recorded in
  `webhook_deliveries` from the real outcome.
- `notify_run_finished` is called from the run ledger's finish
  path and is fail-soft end to end: it never raises, and delivery
  runs on a daemon thread so a slow receiver can never stall a
  production worker.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import threading
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

EVENT_RUN_COMPLETED = "run.completed"
EVENT_RUN_FAILED = "run.failed"
VALID_EVENTS = frozenset({EVENT_RUN_COMPLETED, EVENT_RUN_FAILED})
MAX_ENDPOINTS_PER_USER = 10
MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = 10

# Terminal run status -> webhook event. Other finish statuses
# (none expected today) fire nothing.
_STATUS_TO_EVENT = {
    "completed": EVENT_RUN_COMPLETED,
    "failed": EVENT_RUN_FAILED,
    "cancelled": EVENT_RUN_FAILED,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def validate_events(events) -> list[str]:
    if not isinstance(events, (list, tuple)) or not events:
        raise ValueError("At least one event is required.")
    cleaned = sorted({str(event).strip() for event in events})
    unknown = [event for event in cleaned if event not in VALID_EVENTS]
    if unknown:
        raise ValueError(
            "Unknown event(s): "
            + ", ".join(unknown)
            + ". Valid events are: run.completed, run.failed."
        )
    return cleaned


def validate_registration_url(url: str) -> str:
    """Shape checks applied at registration (no DNS here — the
    full SSRF guard, including resolution, runs at delivery time
    through the Assistant fetcher's validate_url). Literal IP
    hosts must already be public at registration."""
    text = str(url or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Webhook URL must be http or https.")
    if not parsed.hostname:
        raise ValueError("Webhook URL has no host.")
    if parsed.username or parsed.password:
        raise ValueError("Webhook URL must not embed credentials.")
    if parsed.port not in (None, 80, 443):
        raise ValueError("Webhook URL must use a standard port (80/443).")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise ValueError(
            "Webhook URL host must be a public address; private and "
            "loopback destinations are refused."
        )
    return text


def endpoint_view(row) -> dict:
    return {
        "id": row.id,
        "url": row.url,
        "events": list(row.events or []),
        "created_at": row.created_at,
        "disabled_at": row.disabled_at,
        "status": "disabled" if row.disabled_at is not None else "active",
    }


def delivery_view(row) -> dict:
    return {
        "id": row.id,
        "endpoint_id": row.endpoint_id,
        "delivery_group": row.delivery_group,
        "event": row.event,
        "run_id": row.run_id,
        "project_id": row.project_id,
        "attempt": row.attempt,
        "response_status": row.response_status,
        "error": row.error,
        "created_at": row.created_at,
    }


def create_endpoint(db, user_id: str, *, url: str, events) -> tuple[object, str]:
    """Register an endpoint. Returns (row, signing_secret) — the
    secret is available ONLY here."""
    from models import WebhookEndpoint

    cleaned_url = validate_registration_url(url)
    cleaned_events = validate_events(events)
    active = (
        db.query(WebhookEndpoint)
        .filter(
            WebhookEndpoint.user_id == user_id,
            WebhookEndpoint.disabled_at.is_(None),
        )
        .count()
    )
    if active >= MAX_ENDPOINTS_PER_USER:
        raise ValueError(
            f"Webhook limit reached ({MAX_ENDPOINTS_PER_USER} active "
            "endpoints); disable one before registering another."
        )
    secret = "whsec_" + secrets.token_urlsafe(32)
    row = WebhookEndpoint(
        id=str(uuid.uuid4()),
        user_id=user_id,
        url=cleaned_url,
        events=cleaned_events,
        secret_hash=_hash_secret(secret),
        created_at=_utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, secret


def list_endpoints(db, user_id: str) -> list:
    from models import WebhookEndpoint

    return (
        db.query(WebhookEndpoint)
        .filter(WebhookEndpoint.user_id == user_id)
        .order_by(WebhookEndpoint.created_at.desc())
        .all()
    )


def get_owned_endpoint(db, user_id: str, endpoint_id: str):
    from models import WebhookEndpoint

    return (
        db.query(WebhookEndpoint)
        .filter(
            WebhookEndpoint.id == endpoint_id,
            WebhookEndpoint.user_id == user_id,
        )
        .one_or_none()
    )


def disable_endpoint(db, user_id: str, endpoint_id: str):
    row = get_owned_endpoint(db, user_id, endpoint_id)
    if row is None:
        return None
    if row.disabled_at is None:
        row.disabled_at = _utcnow()
        db.commit()
        db.refresh(row)
    return row


def list_deliveries(db, user_id: str, endpoint_id: str, limit: int = 50):
    """Deliveries for one of the caller's endpoints, newest first.
    Returns None when the endpoint is not theirs (the route maps
    that to 404)."""
    from models import WebhookDelivery

    if get_owned_endpoint(db, user_id, endpoint_id) is None:
        return None
    return (
        db.query(WebhookDelivery)
        .filter(WebhookDelivery.endpoint_id == endpoint_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )


# ---------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------


def sign(secret_hash: str, body: bytes) -> str:
    """The delivery signature for a raw body, keyed by the stored
    SHA-256 hex of the endpoint's signing secret."""
    return hmac.new(
        secret_hash.encode(), body, hashlib.sha256
    ).hexdigest()


def _http_post(url: str, body: bytes, headers: dict) -> int:
    """The transport seam (tests stub this). No redirects: a 3xx is
    the recorded outcome, never a followed hop."""
    response = requests.post(
        url,
        data=body,
        headers=headers,
        timeout=TIMEOUT_SECONDS,
        allow_redirects=False,
    )
    return response.status_code


def _record_attempt(db, endpoint, group, event, run_id, project_id,
                    attempt, status, error) -> None:
    from models import WebhookDelivery

    db.add(
        WebhookDelivery(
            id=str(uuid.uuid4()),
            endpoint_id=endpoint.id,
            delivery_group=group,
            event=event,
            run_id=run_id,
            project_id=project_id,
            attempt=attempt,
            response_status=status,
            error=error,
            created_at=_utcnow(),
        )
    )
    db.commit()


def deliver(endpoint, event: str, payload: dict, group: str | None = None) -> str:
    """Deliver one event to one endpoint, recording every attempt.
    Returns the delivery group id. Never raises."""
    from database import SessionLocal
    from services.assistant.fetcher import FetchRefused, validate_url

    group = group or str(uuid.uuid4())
    run_id = (payload.get("data") or {}).get("run_id")
    project_id = (payload.get("data") or {}).get("project_id")
    # The payload's id IS the delivery group: one id names the
    # event across the header, the body and the delivery records.
    payload = {**payload, "id": group}
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "CinemoraWebhooks/1.0",
        "X-Cinemora-Event": event,
        "X-Cinemora-Delivery": group,
        "X-Cinemora-Signature": "sha256=" + sign(endpoint.secret_hash, body),
    }
    db = SessionLocal()
    try:
        try:
            url = validate_url(endpoint.url)
        except FetchRefused as refused:
            _record_attempt(
                db, endpoint, group, event, run_id, project_id,
                1, None, f"refused: {refused.reason}",
            )
            return group
        for attempt in range(1, MAX_ATTEMPTS + 1):
            status = None
            error = None
            try:
                status = _http_post(url, body, headers)
            except Exception as transport_error:  # noqa: BLE001
                error = f"{type(transport_error).__name__}: {transport_error}"
            _record_attempt(
                db, endpoint, group, event, run_id, project_id,
                attempt, status, error,
            )
            if status is not None and 200 <= status < 300:
                break
            if status is not None and status < 500:
                break  # final non-2xx (3xx/4xx): retrying cannot help
    except Exception:  # noqa: BLE001 — delivery must never raise
        logger.warning("webhooks: delivery failed", exc_info=True)
    finally:
        db.close()
    return group


def notify_run_finished(run_id: str, status: str, *, wait: bool = False) -> None:
    """Fan a terminal run status out to the project owner's
    subscribed endpoints. Fail-soft: called from the run ledger's
    finish path, it must never disturb a production. Delivery runs
    on a daemon thread unless `wait` is set (tests)."""
    event = _STATUS_TO_EVENT.get(str(status or ""))
    if not event or not run_id:
        return

    def _fan_out() -> None:
        try:
            from database import SessionLocal
            from models import ProductionRun, Project, WebhookEndpoint

            db = SessionLocal()
            try:
                run = db.get(ProductionRun, str(run_id))
                if run is None:
                    return
                project = db.get(Project, run.project_id)
                if project is None:
                    return
                endpoints = (
                    db.query(WebhookEndpoint)
                    .filter(
                        WebhookEndpoint.user_id == project.owner_id,
                        WebhookEndpoint.disabled_at.is_(None),
                    )
                    .all()
                )
                targets = [
                    endpoint
                    for endpoint in endpoints
                    if event in list(endpoint.events or [])
                ]
                payload = {
                    "id": str(uuid.uuid4()),
                    "event": event,
                    "created_at": _utcnow().isoformat(),
                    "data": {
                        "run_id": run.id,
                        "project_id": project.id,
                        # The status that fired the event (the
                        # finish path has already committed it).
                        "status": str(status),
                        "finished_at": (
                            run.finished_at.isoformat()
                            if run.finished_at is not None
                            else None
                        ),
                    },
                }
            finally:
                db.close()
            for endpoint in targets:
                deliver(endpoint, event, payload)
        except Exception:  # noqa: BLE001 — fail-soft by contract
            logger.warning(
                "webhooks: run-finished fan-out failed", exc_info=True
            )

    if wait:
        _fan_out()
    else:
        threading.Thread(
            target=_fan_out, name="cinemora-webhook-fanout", daemon=True
        ).start()
