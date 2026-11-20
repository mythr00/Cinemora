"""Anchored media root (Stage 2G-b).

Production code previously used the CWD-relative path ``media/<id>``,
so the media directory moved depending on where the process happened
to be launched from. media_root() anchors it:

- ``$CINEMORA_MEDIA_ROOT`` when set (read at CALL time, so tests and
  deployments can override it without a reload), else
- ``<backend>/media`` — resolved from this file's location, fully
  independent of the current working directory.
"""

from __future__ import annotations

import os
from pathlib import Path


def media_root() -> Path:
    """Absolute path of the media root directory."""
    override = os.environ.get("CINEMORA_MEDIA_ROOT")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "media"


def project_dir(project_id) -> Path:
    """Absolute path of one project's media directory."""
    return media_root() / str(project_id)


# ---------------------------------------------------------------------
# Run-scoped artefacts (DATA-004, Stage 4 / R3)
#
# Artefacts used to be project-scoped only: files belonged to
# projects while decisions belong to runs, so a second run overwrote
# the first run's derived artefacts while the first run's ledger rows
# and certificate still referenced those paths (FP-15). Following
# Master Review D3.9's phased plan, NEW runs write their derived
# artefacts — the ones read back as truth — under
# ``media/<project>/runs/<run_id>/``:
#
# - Phase 1 (derived artefacts): the names in RUN_SCOPED_ARTEFACTS.
# - Phase 2 (final media): the production final MP4 joins them; the
#   project-level film path survives only as the media index's
#   latest-run view (a resolved pointer, not a physical file other
#   runs overwrite).
# - Phase 3 (heavy shared media — downloaded candidates, narration
#   parts, intermediates) deliberately STAYS project-scoped: it is
#   content/hash-addressed in practice, so cross-run sharing is a
#   cache, not an overwrite risk.
#
# Nothing is migrated: projects produced before run-scoping keep
# their project-level files, and every reader resolves through
# resolve_artefact(), which prefers a run-scoped copy and falls back
# to the legacy project-level path.
# ---------------------------------------------------------------------

RUN_SCOPED_ARTEFACTS = frozenset(
    {
        "timeline.json",
        "asset_report.json",
        "rights_manifest.json",
        "visual_quality.json",
        "production_report.json",
        "run_manifest.json",
        "production_state.json",
        "pipeline_result.json",
        "documentary_final_output.mp4",
    }
)


def runs_root(project_id) -> Path:
    """Absolute path of one project's run-artefact root."""
    return project_dir(project_id) / "runs"


def run_dir(project_id, run_id) -> Path:
    """Absolute path of one run's artefact directory."""
    return runs_root(project_id) / str(run_id)


def run_subdir(project_dir_path, run_id) -> Path:
    """Run artefact directory relative to a project dir already in
    hand (the form writers use when a caller passed the project dir,
    which in tests is not necessarily the canonical media root)."""
    return Path(project_dir_path) / "runs" / str(run_id)


def resolve_artefact(project_dir_path, filename, run_id=None) -> Path:
    """Resolve where an artefact is actually read from.

    - With ``run_id``: that run's scoped copy when it exists, else
      the legacy project-level path (the run predates run-scoping,
      or the artefact was never written for it).
    - Without a run context (project-level views — the plan, the
      media index, status): the freshest run-scoped copy when any
      run wrote one, else the legacy project-level path. This is the
      "latest run" view D3.9 prescribes: resolved at read time, not
      a physical location other runs overwrite.

    The returned path may not exist; callers keep their existing
    absence handling (unparseable/missing -> not_recorded, never a
    fabricated value).
    """
    project_dir_path = Path(project_dir_path)
    if run_id:
        candidate = run_subdir(project_dir_path, run_id) / filename
        if candidate.is_file():
            return candidate
        return project_dir_path / filename
    runs_dir = project_dir_path / "runs"
    best = None
    best_mtime_ns = -1
    if runs_dir.is_dir():
        for candidate in runs_dir.glob(f"*/{filename}"):
            try:
                if not candidate.is_file():
                    continue
                mtime_ns = candidate.stat().st_mtime_ns
            except OSError:
                continue
            if mtime_ns > best_mtime_ns:
                best = candidate
                best_mtime_ns = mtime_ns
    if best is not None:
        return best
    return project_dir_path / filename
