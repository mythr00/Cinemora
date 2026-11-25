"""Cinematic Motion Graphics Engine v1.

A PIL compositor (core) + style tokens (styles) + data-driven
templates (templates). Templates render caller-supplied data only
and return (None, {"reason": ...}) when their data is missing —
the engine never invents geography, documents or evidence.
"""

from services.motion_engine.styles import STYLES, resolve_style

# Importing the package loads both template modules in order, so
# the v2 family is registered into templates.TEMPLATES no matter
# which submodule a caller imports first (Stage 5).
from services.motion_engine import templates as _templates  # noqa: E402,F401
from services.motion_engine import templates_v2 as _templates_v2  # noqa: E402,F401
from services.motion_engine import templates_v3 as _templates_v3  # noqa: E402,F401
from services.motion_engine import templates_v4 as _templates_v4  # noqa: E402,F401
# GitHub Skills Programme (2026-11-25): the Manim render engine's
# templates self-register into templates.TEMPLATES on import. The
# module never imports manim itself, so this stays cheap and safe
# on hosts where Manim is not installed (its templates then refuse
# with the typed "manim_unavailable" reason).
from services.motion_engine import manim_templates as _manim_templates  # noqa: E402,F401
# GitHub Skills Programme (2026-11-25): the matplotlib chart
# templates self-register the same way; matplotlib itself is
# imported lazily inside the renders.
from services.motion_engine import mpl_templates as _mpl_templates  # noqa: E402,F401

__all__ = ["STYLES", "resolve_style"]
