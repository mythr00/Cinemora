"""Worker lifecycle forensics (Phase 3b / C-15).

On 2026-11-24 the local stack died at least six times and the
watchdog loop died repeatedly; none of those deaths could be
explained afterwards because NOTHING recorded the circumstances —
worker output went to ephemeral logs, the supervisor respawned
children silently, and a run whose heartbeat stopped was marked
"interrupted" with no record of which worker owned it or when its
last heartbeat landed.

This module is the durable, inspectable record those deaths lacked:
an append-only JSONL store of structured lifecycle events. Writers:

- worker.py — "worker_start" (pid, worker identity, queues) and
  "worker_exit" (how work() ended: returned / exception, with the
  exception recorded). A SIGKILLed worker cannot record its own
  exit; that case is covered by the two records below.
- supervise.WorkerPool / Supervisor — "worker_spawned",
  "worker_died" (slot, pid, exit code — negative means killed by
  that signal — and the restart count), "web_died". This is the
  deployment layer that SEES the exit code; Phase 2a's supervise.py
  already respawned dead children, it just never wrote down what
  it saw. The events are recorded here, not by changing that
  respawn behaviour.
- services.run_ledger.reconcile_stale_runs — "heartbeat_loss"
  when the heartbeat-aware reconciler marks runs interrupted:
  which runs, which worker owned each, its last heartbeat and the
  heartbeat's age. This is the record that survives even when both
  the worker and its supervisor died.

Location: CINEMORA_WORKER_EVENTS_PATH when set, else
<media root>/worker_events.jsonl (the media root is the stack's
durable local data dir). Every write is fail-soft: forensics must
never take a worker down. Reads are plain JSONL — inspect with
read_events() or any text tool.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()


def events_path() -> Path:
    override = os.getenv("CINEMORA_WORKER_EVENTS_PATH")
    if override:
        return Path(override)
    from services import media_paths

    return media_paths.media_root() / "worker_events.jsonl"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_event(event: str, **fields) -> bool:
    """Append one structured lifecycle event. Returns True when the
    entry was written. NEVER raises (fail-soft by design)."""
    try:
        worker_id = fields.pop("worker_id", None)
        if worker_id is None:
            try:
                from services import run_ledger

                worker_id = run_ledger.current_worker_id()
            except Exception:
                worker_id = None
        entry = {
            "ts": _utc_now(),
            "event": str(event),
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "worker_id": worker_id,
        }
        entry.update(fields)
        line = json.dumps(entry, default=str)
        path = events_path()
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        return True
    except Exception:
        return False


def read_events(limit: int = 100, path=None) -> list:
    """The most recent `limit` events, oldest first. [] when the
    store does not exist or cannot be read; malformed lines are
    skipped, never fatal."""
    try:
        target = Path(path) if path else events_path()
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for line in lines[-max(1, int(limit)):]:
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events
