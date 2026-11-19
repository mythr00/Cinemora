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
