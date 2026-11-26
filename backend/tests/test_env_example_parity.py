"""B-17 drift guard: backend/.env.example must cover every env var
the code reads.

If you add a new ``os.environ``/``os.getenv`` read (or a new
``CINEMORA_*`` / ``BREWKEG_*`` literal passed to a
config helper) without documenting the variable in
``backend/.env.example``, this test fails and names the variable.
Document it there — purpose, actual code default, required/optional,
SECRET marker where applicable — instead of weakening this test.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent
EXAMPLE = BACKEND_DIR / ".env.example"

_READ_PATTERNS = [
    # os.environ.get("X") / os.getenv("X") / environ.get / env.get /
    # setdefault / the _value/_env/_env_float config helpers.
    re.compile(
        r"(?:os\.environ|os\.getenv|environ\.get|environ\.setdefault|"
        r"getenv|env\.get|_value|_env_float|_env)\s*\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"
    ),
    # os.environ["X"]
    re.compile(r"os\.environ\[['\"]([A-Z][A-Z0-9_]*)['\"]\]"),
    # NAME_ENV = "X" constants (DEFAULT_RATE_LIMIT_ENV, WEB_ONLY_ENV)
    re.compile(r"_ENV\s*=\s*['\"]([A-Z][A-Z0-9_]*)['\"]"),
    # Any CINEMORA_*/BREWKEG_* string literal in backend code
    # (catches indirect reads: the music mix env map, admin-settings
    # display names, the assistant redaction denylist).
    re.compile(r"['\"]((?:CINEMORA|BREWKEG)_[A-Z0-9_]+)['\"]"),
]

_DYNAMIC_RPS = re.compile(r"CINEMORA_\{[^}]*\}_RPS")

# Documented in .env.example but never read by backend code, on
# purpose: TMPDIR is honoured by the OS/Python runtime and steered by
# the Docker image; CINEMORA_BACKEND_URL is frontend-only and listed
# to prevent the recurring confusion (domain B annex §2).
EXTRA_DOCUMENTED = {"TMPDIR", "CINEMORA_BACKEND_URL"}


def _discover_env_vars() -> set[str]:
    found: set[str] = set()
    sources = [
        p
        for p in BACKEND_DIR.rglob("*.py")
        if "tests" not in p.parts and "__pycache__" not in p.parts
    ]
    root_launcher = REPO_ROOT / "app.py"
    if root_launcher.exists():
        sources.append(root_launcher)
    for path in sources:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in _READ_PATTERNS:
            found.update(pattern.findall(text))
        if _DYNAMIC_RPS.search(text):
            found.add("CINEMORA_<SCOPE>_RPS")
    return found


def _documented_env_vars() -> set[str]:
    names: set[str] = set()
    for line in EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*#?\s*([A-Z][A-Z0-9_<>]+)=", line)
        if match:
            names.add(match.group(1))
    return names


def test_env_example_covers_every_env_var_read_in_code():
    discovered = _discover_env_vars()
    documented = _documented_env_vars()
    missing = discovered - documented
    assert not missing, (
        "env vars read in code but missing from backend/.env.example "
        f"(B-17): {sorted(missing)}"
    )
    stale = documented - discovered - EXTRA_DOCUMENTED
    assert not stale, (
        "backend/.env.example documents variables no code reads "
        f"(remove them or add them to EXTRA_DOCUMENTED with a reason): "
        f"{sorted(stale)}"
    )


def test_env_example_exists_and_is_substantial():
    documented = _documented_env_vars()
    # The audit census (domain B annex §2) found ~60 variables; the
    # schema must not silently shrink back to the old partial example.
    assert len(documented) >= 60, f"only {len(documented)} vars documented"
