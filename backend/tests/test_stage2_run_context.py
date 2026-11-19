"""Stage 2C — RunContext isolation tests for media_pipeline globals.

The design is REBINDING: media_pipeline's module globals are re-pointed
at the active RunContext's containers, so existing logic sites land in
the active context unchanged. The DEFAULT context wraps the original
container objects, so un-activated behaviour is identical to before.
"""

import pytest

from services import media_pipeline as mp
from services import run_context
from services.run_context import RunContext


@pytest.fixture(autouse=True)
def _clean_state():
    # Start and end every test on the default context with clean state.
    run_context.deactivate(None)
    mp.reset_media_caches()
    mp._search_failure_state["consecutive_failures"] = 0
    mp._search_failure_state["last_error"] = None
    run_context.default_context().missing_visual_count = 0
    yield
    run_context.deactivate(None)
    mp.reset_media_caches()
    mp._search_failure_state["consecutive_failures"] = 0
    mp._search_failure_state["last_error"] = None
    run_context.default_context().missing_visual_count = 0


def test_default_context_wraps_original_containers():
    default = run_context.default_context()
    # Same objects — mutating via the module is visible via the context.
    assert mp._FAILED_DOWNLOAD_URLS is default.failed_download_urls
    assert mp._DISCOVERY_FAILURES is default.discovery_failures
    assert mp._SEGMENT_BASIS is default.segment_basis
    assert mp._SEGMENT_ATTEMPTS is default.segment_attempts
    assert mp._SEGMENT_FAILURE_CAUSE is default.segment_failure_cause
    assert mp._yt_segment_uses is default.yt_segment_uses
    assert mp._youtube_metadata_cache is default.youtube_metadata_cache
    assert mp._youtube_transcript_cache is default.youtube_transcript_cache
    assert mp._yt_info_failed is default.yt_info_failed
    assert mp._yt_transcript_failed is default.yt_transcript_failed
    assert mp._search_failure_state is default.search_failure_state

    mp._FAILED_DOWNLOAD_URLS.add("https://example.com/dead")
    assert "https://example.com/dead" in default.failed_download_urls
    assert run_context.current_context() is default


def test_interleaved_contexts_are_isolated():
    ctx_a = RunContext(project_id="a")
    ctx_b = RunContext(project_id="b")

    previous = run_context.activate(ctx_a)
    assert previous is None
    assert mp._FAILED_DOWNLOAD_URLS is ctx_a.failed_download_urls
    mp._record_search_failure("x")  # 1 — well below the 8-failure breaker
    mp._FAILED_DOWNLOAD_URLS.add("https://a.example/1")
    mp._SEGMENT_BASIS["u"] = "verified_segment"

    previous = run_context.activate(ctx_b)
    assert previous is ctx_a
    assert mp._FAILED_DOWNLOAD_URLS is ctx_b.failed_download_urls
    assert mp._search_failure_state["consecutive_failures"] == 0
    assert "https://a.example/1" not in mp._FAILED_DOWNLOAD_URLS
    assert "u" not in mp._SEGMENT_BASIS
    mp._record_search_failure("y")
    mp._record_search_failure("z")
    mp._FAILED_DOWNLOAD_URLS.add("https://b.example/2")
    mp._SEGMENT_BASIS["v"] = "chapter"

    previous = run_context.activate(ctx_a)
    assert previous is ctx_b
    # A's state is intact; B's is absent.
    assert mp._search_failure_state["consecutive_failures"] == 1
    assert mp._search_failure_state["last_error"] == "x"
    assert mp._FAILED_DOWNLOAD_URLS == {"https://a.example/1"}
    assert mp._SEGMENT_BASIS == {"u": "verified_segment"}
    assert ctx_b.search_failure_state["consecutive_failures"] == 2
    assert ctx_b.failed_download_urls == {"https://b.example/2"}

    # Deactivating back to no-active restores the default context.
    run_context.deactivate(None)
    assert run_context.current_context() is run_context.default_context()
    assert mp._FAILED_DOWNLOAD_URLS is run_context.default_context().failed_download_urls
    assert "https://a.example/1" not in mp._FAILED_DOWNLOAD_URLS
    assert mp._search_failure_state["consecutive_failures"] == 0


def test_use_context_restores_previous_on_exit():
    ctx = RunContext(project_id="temp")
    default = run_context.default_context()
    with run_context.use_context(ctx) as active:
        assert active is ctx
        assert run_context.current_context() is ctx
        assert mp._FAILED_DOWNLOAD_URLS is ctx.failed_download_urls
    assert run_context.current_context() is default
    assert mp._FAILED_DOWNLOAD_URLS is default.failed_download_urls


def test_use_context_restores_previous_on_exception():
    ctx = RunContext(project_id="boom")
    default = run_context.default_context()
    with pytest.raises(RuntimeError):
        with run_context.use_context(ctx):
            raise RuntimeError("boom")
    assert run_context.current_context() is default
    assert mp._FAILED_DOWNLOAD_URLS is default.failed_download_urls


def test_use_context_nested_restores_outer():
    outer = RunContext(project_id="outer")
    inner = RunContext(project_id="inner")
    with run_context.use_context(outer):
        with run_context.use_context(inner):
            assert run_context.current_context() is inner
        assert run_context.current_context() is outer
        assert mp._FAILED_DOWNLOAD_URLS is outer.failed_download_urls


def test_note_missing_visual_counts_only_active_context():
    ctx = RunContext(project_id="counter")
    default = run_context.default_context()
    assert default.missing_visual_count == 0

    with run_context.use_context(ctx):
        run_context.note_missing_visual()
        run_context.note_missing_visual()
        assert ctx.missing_visual_count == 2
        assert default.missing_visual_count == 0

    run_context.note_missing_visual()
    assert default.missing_visual_count == 1
    assert ctx.missing_visual_count == 2


def test_run_context_fresh_defaults():
    ctx = RunContext(project_id="fresh")
    assert ctx.failed_download_urls == set()
    assert ctx.discovery_failures == []
    assert ctx.segment_basis == {}
    assert ctx.yt_segment_uses == {}
    assert ctx.yt_info_failed == set()
    assert ctx.search_failure_state == {
        "consecutive_failures": 0,
        "last_error": None,
    }
    assert ctx.missing_visual_count == 0
    assert ctx.rights_policy  # snapshot string, informational only
