"""Pytest bootstrap for the CINEMORA backend suite.

Sets a safe, fully local environment BEFORE any backend module is imported:
- a throwaway SQLite database (never the production DATABASE_URL),
- dummy provider keys so import-time config checks pass without real secrets,
- a dummy Redis URL (RQ connects lazily; no server is contacted by imports).

External providers (SearXNG, yt-dlp, BrewKeg LLM, Piper, Whisper) are mocked in
the individual test modules that exercise them — the suite never performs
real network retrieval.
"""

import os
import sys
import tempfile

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

_TMP = tempfile.mkdtemp(prefix="cinemora-tests-")

os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMP, 'test.db')}")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("BREWKEG_API_KEY", "test-dummy-key")
os.environ.setdefault("SEARXNG_URL", "http://searxng.test")
os.environ.setdefault("SOURCE_INTEL_DB", os.path.join(_TMP, "source_intel.sqlite3"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_search_circuit_breaker():
    """The search provider's circuit breaker is module-global
    (per-process in production, by design). A test that makes a
    REAL failing search call (e.g. the assistant security suite,
    whose dummy instance URL cannot answer) trips it for 60 s of
    wall time, which would otherwise leak into unrelated later
    tests in the same process that stub the transport. Reset it
    around every test; the breaker's own behaviour is pinned by
    tests/test_searxng_provider.py."""
    try:
        from services import search_provider

        search_provider._breaker_reset()
    except Exception:
        pass
    yield
    try:
        from services import search_provider

        search_provider._breaker_reset()
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def _kb_default_transport_offline():
    """Keep the suite's no-real-network rule true after D-2.

    Founder decision D-2 was taken on 2026-11-22 (Transformation
    Plan §2.3), enabling kb_client's live Wikipedia/Wikidata
    lookups in production. Tests must stay deterministic and
    offline (see this file's docstring), so the DEFAULT transport
    is stubbed to unreachable for the whole session: KB lookups in
    tests resolve from cache or record "unreachable" and the
    typing cascade falls through, exactly as it does on an offline
    production box. Session scope because module-scoped fixtures
    (the golden-plan bibles) build before function-scoped fixtures
    run. Tests for the live path inject their own transport (the
    kb_client contract) and are unaffected by this stub.
    """
    from services import kb_client

    original = kb_client._default_transport

    def _offline_transport(url, params, timeout=None):
        raise ConnectionError("kb default transport disabled in tests")

    kb_client._default_transport = _offline_transport
    yield
    kb_client._default_transport = original
