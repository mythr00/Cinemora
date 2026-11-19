"""Cinematic Motion Graphics Engine v1.

A PIL compositor (core) + style tokens (styles) + data-driven
templates (templates). Templates render caller-supplied data only
and return (None, {"reason": ...}) when their data is missing —
the engine never invents geography, documents or evidence.
"""

from services.motion_engine.styles import STYLES, resolve_style

__all__ = ["STYLES", "resolve_style"]
