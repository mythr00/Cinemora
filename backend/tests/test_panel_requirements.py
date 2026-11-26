"""Packaging guard for the panel (Pterodactyl/generic-python) host.

On 2026-11-26 the Nodaroo server failed to start with
``ModuleNotFoundError: No module named 'alembic'``: the panel startup
installs the requirements file into a bare Python image, and the full
``backend/requirements.txt`` cannot install there — manimpango and
pycairo publish no prebuilt wheels (they compile against system
Pango/Cairo libraries the image does not carry), so pip aborted the
whole install partway and left the environment half-empty.

``requirements-panel.txt`` at the repository root is the panel-host
manifest: exactly the backend set minus the Manim source-build tree
(the motion engine's Manim templates degrade to the typed
``manim_unavailable`` refusal without it, by design). This test keeps
the two manifests from drifting apart silently.
"""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PANEL_FILE = BACKEND.parent / "requirements-panel.txt"

EXCLUDED = {
    "manim",
    "manimpango",
    "pycairo",
    "moderngl",
    "moderngl-window",
    "glcontext",
    "pyglet",
    "pyglm",
    "skia-pathops",
    "isosurfaces",
    "svgelements",
    "mapbox-earcut",
    "screeninfo",
    "cloup",
    "pydub",
}


def _pins(path):
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[=<>!~\[]", line)[0].strip().lower()
        pins[name] = line
    return pins


def test_panel_requirements_match_backend_minus_manim_tree():
    full = _pins(BACKEND / "requirements.txt")
    panel = _pins(PANEL_FILE)
    assert set(panel) == set(full) - EXCLUDED
    for name, line in panel.items():
        assert full[name] == line, f"pin drift for {name}"


def test_panel_requirements_carry_the_startup_critical_packages():
    panel = _pins(PANEL_FILE)
    for name in ("alembic", "sqlalchemy", "fastapi", "uvicorn", "redis", "rq"):
        assert name in panel
