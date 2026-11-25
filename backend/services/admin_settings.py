"""Admin settings read (Phase 7; design admin/02 M8).

The effective platform configuration, displayed the way the run
manifest displays providers: real values for non-secrets,
configured booleans for secrets — a secret value is never returned.
Configuration in this codebase is environment-based and applied at
process start, so every entry is labelled `applies_on_restart`;
there are no runtime-writable settings today and this module does
not pretend otherwise.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone


def _value(name: str, *, secret: bool = False, default=None) -> dict:
    raw = os.environ.get(name)
    if secret:
        return {
            "name": name,
            "configured": bool(raw),
            "secret": True,
            "applies_on_restart": True,
        }
    return {
        "name": name,
        "value": raw if raw is not None else default,
        "configured": raw is not None,
        "secret": False,
        "applies_on_restart": True,
    }


def _computed(name: str, value) -> dict:
    return {
        "name": name,
        "value": value,
        "configured": True,
        "secret": False,
        "applies_on_restart": True,
    }


def effective_settings() -> dict:
    from services import run_ledger

    groups = {
        "credentials": [
            _value("CINEMORA_API_TOKEN", secret=True),
            _value("SERPER_API_KEY", secret=True),
            _value("BREWKEG_API_KEY", secret=True),
            _value("DATABASE_URL", secret=True),
            _value("REDIS_URL", secret=True),
        ],
        "providers": [
            _value("BREWKEG_MODEL"),
            _value("CINEMORA_DISABLED_PROVIDERS"),
            _value("CINEMORA_FOUNDER_EMAIL"),
        ],
        "auth_and_limits": [
            _value("CINEMORA_ALLOW_OPERATOR_TOKEN", default="0"),
            _value("CINEMORA_REGISTRATION", default="open"),
            _value("CINEMORA_API_RATE_LIMIT", default="30"),
            _value("CINEMORA_AUTH_RATE_LIMIT", default="10"),
            _value("CINEMORA_COOKIE_SECURE", default="1"),
        ],
        "pipeline": [
            _computed(
                "CINEMORA_RUN_STALE_SECONDS (effective)",
                run_ledger.STALE_AFTER_SECONDS,
            ),
            _value("CINEMORA_WORKER_COUNT"),
            _value("CINEMORA_RENDER_CONCURRENCY"),
            _value("CINEMORA_MEDIA_ROOT"),
            _value("CINEMORA_PUBLIC_URL"),
        ],
        "pricing": [
            {
                "name": "provider price table",
                "value": None,
                "configured": False,
                "secret": False,
                "applies_on_restart": True,
                "note": "No price table is configured anywhere in "
                "the platform; monetary cost is reported as "
                "unknown (None), never 0 or estimated.",
            }
        ],
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "Configuration is environment-based: every value here "
            "applies on process restart. No setting is writable at "
            "runtime today."
        ),
        "groups": groups,
    }
