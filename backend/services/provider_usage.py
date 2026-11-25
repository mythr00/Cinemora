"""Provider usage ledger (Phase 5c-1 / B-10).

Append-only recording of LLM provider calls plus the read-only
aggregate behind ``GET /api/v1/admin/usage``.

Design rules:

- STRUCTURE ONLY, NO PRICES. No price table is configured
  anywhere in the system, so cost is rendered as ``None`` with
  ``pricing_status: "not_configured"`` — never 0, never
  estimated. The Phase 8 cost engine will price the recorded
  events later from its own configuration.
- HONEST UNKNOWN. Token fields the provider did not report are
  recorded NULL; caller context (user / project / run) the
  caller did not supply is recorded NULL. Nothing is fabricated.
- FAIL-SOFT RECORDING. :func:`record_usage` never raises: a
  ledger problem (DB down, table missing) is logged and the LLM
  call that triggered the recording proceeds untouched.
- SEARCH COUNTERS ARE PROCESS-LOCAL. Serper usage is tracked
  in-process by services/run_metrics (per-run states in memory);
  the aggregate labels those numbers ``process_local`` instead
  of pretending they are a global ledger.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

try:
    from services import logging_setup
except ImportError:  # pragma: no cover - flat-layout fallback
    import logging_setup

logger = logging_setup.get_logger("provider_usage")

PRICING_STATUS = "not_configured"


def _token_fields(usage) -> dict:
    """Normalise a provider usage dict (see
    llm_provider._usage_token_fields — imported lazily so this
    module never participates in an import cycle)."""
    from services.llm_provider import _usage_token_fields

    return _usage_token_fields(usage)


def record_usage(*, provider, model=None, usage=None, context=None) -> bool:
    """Append one usage event. Returns True when the row was
    written, False when recording failed (already logged).
    NEVER raises.

    ``context`` is an optional dict with any of ``user_id`` /
    ``project_id`` / ``run_id`` / ``context`` (a free-text label);
    absent keys are stored NULL.
    """
    try:
        from database import SessionLocal
        from models import ProviderUsageEvent

        tokens = _token_fields(usage)
        context = context if isinstance(context, dict) else {}
        event = ProviderUsageEvent(
            id=str(uuid.uuid4()),
            provider=str(provider or "unknown"),
            model=str(model) if model else None,
            prompt_tokens=tokens["prompt_tokens"],
            completion_tokens=tokens["completion_tokens"],
            total_tokens=tokens["total_tokens"],
            user_id=context.get("user_id") or None,
            project_id=context.get("project_id") or None,
            run_id=context.get("run_id") or None,
            context=(
                str(context["context"])[:120]
                if context.get("context")
                else None
            ),
        )
        db = SessionLocal()
        try:
            db.add(event)
            db.commit()
        finally:
            db.close()
        return True
    except Exception:
        logger.warning("provider usage recording failed", exc_info=True)
        return False


def aggregate_usage(db, *, days: int = 30) -> dict:
    """Token totals grouped by provider + model + day, from the
    ledger. Cost is null with pricing_status "not_configured" —
    no price table exists (Phase 8 will price these events).

    The search section exposes services/run_metrics' in-process
    Serper counters, explicitly labelled process-local."""
    from models import ProviderUsageEvent

    days = max(1, min(int(days or 30), 365))
    since = datetime.now(timezone.utc) - timedelta(days=days)

    day_column = func.date(ProviderUsageEvent.created_at)
    rows = (
        db.query(
            ProviderUsageEvent.provider,
            ProviderUsageEvent.model,
            day_column.label("day"),
            func.count(ProviderUsageEvent.id).label("calls"),
            func.coalesce(func.sum(ProviderUsageEvent.prompt_tokens), 0),
            func.coalesce(
                func.sum(ProviderUsageEvent.completion_tokens), 0
            ),
            func.coalesce(func.sum(ProviderUsageEvent.total_tokens), 0),
        )
        .filter(ProviderUsageEvent.created_at >= since)
        .group_by(ProviderUsageEvent.provider, ProviderUsageEvent.model, day_column)
        .order_by(day_column, ProviderUsageEvent.provider, ProviderUsageEvent.model)
        .all()
    )

    groups = []
    totals = {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }
    for provider, model, day, calls, prompt, completion, total in rows:
        group = {
            "provider": provider,
            "model": model,
            "day": str(day),
            "calls": int(calls),
            "prompt_tokens": int(prompt),
            "completion_tokens": int(completion),
            "total_tokens": int(total),
            # No price table is configured: cost stays unknown.
            "cost": None,
            "pricing_status": PRICING_STATUS,
        }
        groups.append(group)
        totals["calls"] += group["calls"]
        totals["prompt_tokens"] += group["prompt_tokens"]
        totals["completion_tokens"] += group["completion_tokens"]
        totals["total_tokens"] += group["total_tokens"]

    totals["cost"] = None
    totals["pricing_status"] = PRICING_STATUS

    search_section = {
        "provider": "serper",
        "scope": "process_local",
        "note": (
            "Serper counters are held in-process by "
            "services/run_metrics (per-run states in memory); "
            "they cover only this process and its most recent "
            "runs, not the whole platform."
        ),
        "calls": None,
    }
    try:
        from services import run_metrics

        search_section["calls"] = run_metrics.process_totals()[
            "serper_calls"
        ]
    except Exception:
        logger.warning(
            "search counter read failed", exc_info=True
        )

    return {
        "days": days,
        "llm": {
            "groups": groups,
            "totals": totals,
        },
        "search": search_section,
    }
