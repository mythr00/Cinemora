"""Pytest bootstrap for the CINEMORA backend suite.

Sets a safe, fully local environment BEFORE any backend module is imported:
- a throwaway SQLite database (never the production DATABASE_URL),
- dummy provider keys so import-time config checks pass without real secrets,
- a dummy Redis URL (RQ connects lazily; no server is contacted by imports).

External providers (Serper, yt-dlp, BrewKeg LLM, Piper, Whisper) are mocked in
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
os.environ.setdefault("SERPER_API_KEY", "test-dummy-key")
os.environ.setdefault("SOURCE_INTEL_DB", os.path.join(_TMP, "source_intel.sqlite3"))
