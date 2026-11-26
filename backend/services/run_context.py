"""Per-run context for media_pipeline's mutable state (Stage 2C).

Historically media_pipeline kept per-run state in module-level mutable
containers (sets/dicts/lists mutated in place). That made concurrent or
interleaved runs share state. RunContext gives each run its own set of
containers; media_pipeline registers a *binder* that rebinds its module
globals to the active context's containers, so every existing read/write
site lands in the active context with no logic changes.

Design notes:
- Rebinding, not copying: during activation the module names point at
  the active context's containers (identity matters — see tests).
- The DEFAULT context wraps the ORIGINAL container objects created at
  media_pipeline import time, so pre-activation behaviour is
  bit-identical to the old module-global behaviour.
- Synchronization primitives (locks) are NOT part of a context:
  swapping lock identity mid-flight would break mutual exclusion.
- Cross-project stores (services.source_cache SQLite profiles) and
  already run-scoped holders (services.run_metrics) are NOT part of a
  context either — they are cross-project / run-scoped by design.
- Best-effort helpers (note_missing_visual) never raise: a context bug
  must never be able to fail a production run.
"""

from __future__ import annotations

import contextlib
import os
import uuid


class RunContext:
    """All per-run mutable containers for one production run."""

    def __init__(
        self,
        run_id=None,
        project_id=None,
        *,
        failed_download_urls=None,
        discovery_failures=None,
        segment_basis=None,
        segment_attempts=None,
        segment_failure_cause=None,
        yt_segment_uses=None,
        youtube_metadata_cache=None,
        youtube_transcript_cache=None,
        yt_info_failed=None,
        yt_transcript_failed=None,
        segment_file_cache=None,
        search_failure_state=None,
        missing_visual_count=0,
        rights_policy=None,
        verdict_cache=None,
    ):
        self.run_id = run_id if run_id is not None else uuid.uuid4().hex
        self.project_id = project_id
        # Containers: when an existing container is passed it is used
        # AS-IS (identity preserved — the default context wraps
        # media_pipeline's original objects); otherwise a fresh empty
        # container is created.
        self.failed_download_urls = (
            failed_download_urls if failed_download_urls is not None else set()
        )
        self.discovery_failures = (
            discovery_failures if discovery_failures is not None else []
        )
        self.segment_basis = segment_basis if segment_basis is not None else {}
        self.segment_attempts = (
            segment_attempts if segment_attempts is not None else {}
        )
        self.segment_failure_cause = (
            segment_failure_cause if segment_failure_cause is not None else {}
        )
        self.yt_segment_uses = (
            yt_segment_uses if yt_segment_uses is not None else {}
        )
        self.youtube_metadata_cache = (
            youtube_metadata_cache if youtube_metadata_cache is not None else {}
        )
        self.youtube_transcript_cache = (
            youtube_transcript_cache
            if youtube_transcript_cache is not None
            else {}
        )
        self.yt_info_failed = yt_info_failed if yt_info_failed is not None else set()
        self.yt_transcript_failed = (
            yt_transcript_failed if yt_transcript_failed is not None else set()
        )
        # Stage 7 / R8: extracted-clip cache keyed by segment identity
        # (selection segment_id -> asset template). Per-run by
        # construction: cross-project file reuse is not permitted —
        # the cache dies with the run context.
        self.segment_file_cache = (
            segment_file_cache if segment_file_cache is not None else {}
        )
        self.search_failure_state = (
            search_failure_state
            if search_failure_state is not None
            else {"consecutive_failures": 0, "last_error": None}
        )
        # Gate 4 diagnosis (2026-11-26): per-run verdict cache for
        # pipeline_verification._uve_verify_all — (candidate key +
        # metadata/need/scenario fingerprint) -> the fully built
        # scored item. The Hyatt run re-verified the same
        # (URL, beat) pair across its 59 coverage passes / 26
        # recovery rounds: 3,443 of 5,916 candidate records were
        # exact re-evaluations. Per-run by construction, like every
        # container here: the cache dies with the run context.
        self.verdict_cache = verdict_cache if verdict_cache is not None else {}
        self.missing_visual_count = int(missing_visual_count or 0)
        # Informational snapshot taken at construction; the pipeline's
        # actual rights behaviour is unchanged by this value.
        self.rights_policy = (
            rights_policy
            if rights_policy is not None
            else os.getenv("RIGHTS_POLICY", "allow_all")
        )

    def __repr__(self):  # pragma: no cover - debugging aid
        return (
            f"RunContext(run_id={self.run_id!r}, "
            f"project_id={self.project_id!r}, "
            f"missing_visual_count={self.missing_visual_count!r})"
        )


_binder = None
_default = None
_active = None


def register_binder(fn):
    """Register the callable that rebinds a consumer module's globals
    to a context's containers. Called once by media_pipeline at import.
    If a context is already current, it is (re)bound immediately."""
    global _binder
    _binder = fn
    if fn is not None:
        current = current_context()
        if current is not None:
            try:
                fn(current)
            except Exception:
                pass


def set_default(ctx):
    """Set the fallback context used when no context is active. If no
    context is active, the binder is pointed at the new default so
    module globals stay in sync with it."""
    global _default
    _default = ctx
    if _active is None and _binder is not None and ctx is not None:
        try:
            _binder(ctx)
        except Exception:
            pass


def default_context():
    """Return the default context, creating one lazily if none was set
    (e.g. run_context used standalone, without media_pipeline)."""
    global _default
    if _default is None:
        _default = RunContext()
    return _default


def current_context():
    """Return the active context, else the default context."""
    if _active is not None:
        return _active
    return default_context()


def activate(ctx):
    """Make *ctx* the active context and rebind consumers to it.
    Returns the previously active context (or None if none was active),
    suitable for passing to deactivate()."""
    global _active
    previous = _active
    _active = ctx
    if _binder is not None and ctx is not None:
        _binder(ctx)
    return previous


def deactivate(previous):
    """Restore the context returned by activate(). Passing None (no
    context was active before) clears activation and rebinds consumers
    to the default context."""
    global _active
    _active = previous
    if _binder is not None:
        target = previous if previous is not None else default_context()
        if target is not None:
            _binder(target)


@contextlib.contextmanager
def use_context(ctx):
    """Context manager: activate *ctx* on entry, restore the previous
    context on exit — including when the body raises."""
    previous = activate(ctx)
    try:
        yield ctx
    finally:
        deactivate(previous)


def note_missing_visual():
    """Increment the current context's missing-visual counter.
    Never raises."""
    try:
        ctx = current_context()
        if ctx is not None:
            ctx.missing_visual_count += 1
    except Exception:
        pass
