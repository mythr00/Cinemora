"""IA-05 — PERMANENT provider ban regression tests.

Founder directive (2026-11-19, standing/permanent): Pexels and Pixabay
retrieval must NEVER be (re)introduced into Cinemora. The dormant
provider implementations in ``services/licensed_media.py``
(``search_licensed``'s provider wiring + ``_pexels_*`` / ``_pixabay_*``)
and ``services/visual_guard.py`` (``search_licensed_images/videos`` +
``_pexels_*`` / ``_pixabay_*``) were removed under IA-05. Those were the
only executable references: ``search_licensed`` had zero callers in the
repo, ``visual_guard`` was imported nowhere, and ``licensed_media``'s
live callers (``media_pipeline.search_wikimedia_images/videos``) use
only its Wikimedia Commons functions, which remain intact.

These tests pin the ban so the providers cannot silently come back:

1. No LIVE backend module may contain an integration artefact for a
   banned provider: API endpoint, API-key env var, provider helper
   function name, quoted provider source id, or provider licence name.
2. The banned provider NAMES may appear in live code only as prose in
   the explicit ban-documentation allowlist below (statements of the
   ban itself) — never anywhere else.
3. The two stripped modules expose no banned provider functions, and
   ``search_licensed`` serves stills from Wikimedia Commons only.

"Live" excludes: the test suite itself (this file necessarily names
the banned artefacts it scans for), and the pre-existing frozen
snapshot/backup files under ``backend/`` (``*_before_*``, ``*_backup``,
``*_corrupted*``, ``*_recovered*``). Those snapshots are never imported
anywhere (verified by grep under IA-05) and their deletion is a
separate founder-gated decision (C-14), so they are out of scope here.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# Integration artefacts: any of these in a live module means a banned
# provider is wired in (or ready to be), regardless of surrounding prose.
INTEGRATION_PATTERNS = [
    re.compile(r"api\.pexels\.com"),
    re.compile(r"pixabay\.com/api"),
    re.compile(r"PEXELS_API_KEY"),
    re.compile(r"PIXABAY_API_KEY"),
    re.compile(r"_pexels_"),
    re.compile(r"_pixabay_"),
    re.compile(r"""["']pexels["']"""),
    re.compile(r"""["']pixabay["']"""),
    re.compile(r"Pexels License"),
    re.compile(r"Pixabay Content License"),
]

PROSE_PATTERN = re.compile(r"pexels|pixabay", re.IGNORECASE)

# Files allowed to NAME the banned providers in prose, solely to state
# the ban. Integration patterns above are NOT waived for these files.
BAN_DOCUMENTATION_ALLOWLIST = {
    "services/media_pipeline.py",   # docstrings: "no Pexels/Pixabay retrieval"
    "services/licensed_media.py",   # docstrings: providers "stay unwired" / removed
}


def _is_test_file(path: Path) -> bool:
    rel_parts = path.relative_to(BACKEND_ROOT).parts
    return (
        "tests" in rel_parts
        or path.name.startswith("test_")
        or path.name.startswith("e2e_")
        or path == Path(__file__).resolve()
    )


def _is_frozen_snapshot(path: Path) -> bool:
    name = path.name
    return (
        "_before_" in name
        or "_backup" in name
        or "_corrupted" in name
        or "_recovered" in name
    )


def _live_backend_py_files() -> list[Path]:
    files = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if _is_test_file(path) or _is_frozen_snapshot(path):
            continue
        files.append(path)
    return files


def _scan(patterns) -> dict[str, list[str]]:
    offenders: dict[str, list[str]] = {}
    for path in _live_backend_py_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        hits = [pat.pattern for pat in patterns if pat.search(text)]
        if hits:
            rel = path.relative_to(BACKEND_ROOT).as_posix()
            offenders[rel] = hits
    return offenders


def test_no_live_module_integrates_banned_providers():
    offenders = _scan(INTEGRATION_PATTERNS)
    assert not offenders, (
        "Banned Pexels/Pixabay integration artefacts found in live "
        f"backend modules (IA-05 permanent ban): {offenders}"
    )


def test_banned_names_appear_only_in_ban_documentation():
    offenders = {
        rel: hits
        for rel, hits in _scan([PROSE_PATTERN]).items()
        if rel not in BAN_DOCUMENTATION_ALLOWLIST
    }
    assert not offenders, (
        "Banned provider names appear outside the ban-documentation "
        f"allowlist {sorted(BAN_DOCUMENTATION_ALLOWLIST)}: {offenders}"
    )


def test_licensed_media_exposes_no_banned_provider_functions():
    from services import licensed_media

    for name in (
        "_pexels_photos", "_pexels_videos",
        "_pixabay_photos", "_pixabay_videos",
    ):
        assert not hasattr(licensed_media, name), (
            f"licensed_media.{name} exists — banned provider reintroduced"
        )
    # The live Wikimedia surface must still be there.
    assert callable(licensed_media.search_wikimedia)
    assert callable(licensed_media.search_wikimedia_videos)
    assert callable(licensed_media.search_licensed)
    assert callable(licensed_media.download_asset)


def test_search_licensed_serves_wikimedia_only(monkeypatch):
    from services import licensed_media
    from services.visual_policy import VisualRequest

    wikimedia_hit = {
        "kind": "image",
        "source": "wikimedia",
        "title": "Berlin Wall 1989",
        "download_url": "https://commons.wikimedia.org/example.jpg",
    }
    monkeypatch.setattr(
        licensed_media, "_wikimedia", lambda q, limit: [wikimedia_hit]
    )

    results = licensed_media.search_licensed(
        VisualRequest(query="berlin wall", kind="image"), limit=3
    )
    assert results == [wikimedia_hit]

    # Video requests return nothing: the only video providers wired into
    # this entry point were the banned ones (Commons video lives in
    # search_wikimedia_videos).
    assert licensed_media.search_licensed(
        VisualRequest(query="berlin wall", kind="video"), limit=3
    ) == []


def test_visual_guard_exposes_no_banned_provider_functions():
    from services import visual_guard

    for name in (
        "_pexels_photos", "_pexels_videos",
        "_pixabay_photos", "_pixabay_videos",
        "search_licensed_images", "search_licensed_videos",
    ):
        assert not hasattr(visual_guard, name), (
            f"visual_guard.{name} exists — banned provider reintroduced"
        )
    # The non-provider helpers that shared the file must be intact.
    assert visual_guard.looks_like_face("portrait headshot of a woman")
    assert not visual_guard.looks_like_face("empty courtroom benches")
    assert visual_guard.detect_beat("police arrest at night") == "police"
    assert callable(visual_guard.download_direct)


def test_env_example_defines_no_banned_api_keys():
    env_example = (BACKEND_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "PEXELS_API_KEY" not in env_example
    assert "PIXABAY_API_KEY" not in env_example
