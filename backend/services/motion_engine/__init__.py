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
# Phase 5 core treatments rebuild (2026-11-27): the v5 family
# (chronology / route / locator / relationship_network)
# self-registers into templates.TEMPLATES on import, like v2–v4.
from services.motion_engine import templates_v5 as _templates_v5  # noqa: E402,F401
# Animation Studio Expansion (2026-11-28): the v6 family
# (document_examination / title_sequence) self-registers into
# templates.TEMPLATES on import, like v2–v5.
from services.motion_engine import templates_v6 as _templates_v6  # noqa: E402,F401
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
# Animation Studio Expansion (2026-11-28): the Blender 3D
# department self-registers the same way; Blender itself is only
# ever invoked as a subprocess, so importing this module is cheap
# and safe on hosts without Blender (its templates then refuse
# with the typed "blender_unavailable" reason).
from services.motion_engine import blender_templates as _blender_templates  # noqa: E402,F401
# Animation Studio Expansion (2026-11-28): the web render
# engine (Three.js / GSAP / Lottie through headless Chromium)
# self-registers the same way; Node is only ever invoked as a
# subprocess, so importing this module is safe on hosts without
# the engine installed (its templates then refuse with the
# typed "web_engine_unavailable" reason).
from services.motion_engine import web_templates as _web_templates  # noqa: E402,F401

__all__ = ["STYLES", "resolve_style"]
