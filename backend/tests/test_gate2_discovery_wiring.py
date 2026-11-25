"""Gate 2 (footage-discovery directive, 2026-11-26) — workstream 2:
source-independent discovery wiring.

Behaviour pinned here:

- (a) _uve_scenario_search queries the supplementary independent
  providers (Archive.org + Wikimedia Commons video) for EVERY
  unique scenario query, not only the primary, at limit 5 each,
  with per-query failure isolation;
- (b) the true_crime / history / general genre packs now carry a
  source_hints block that validates, resolves and renders against
  a sample need's anchors;
- (c) Wikimedia Commons requests retry with bounded delay
  (honouring Retry-After) before any backoff accounting: a
  429-then-200 response succeeds via the retry path and records
  NO rate limit, while a persistent 429 still records rate limits
  and backs the Commons scope off.
"""

import zlib

import fakeredis
import pytest
import requests

import jobs  # noqa: F401  (ensures backend modules import in suite order)
from services import genre_packs, licensed_media, provider_health
from services import media_pipeline as mp


@pytest.fixture(autouse=True)
def _clean_provider_state():
    provider_health.set_redis_client(
        fakeredis.FakeRedis(decode_responses=True)
    )
    provider_health.reset()
    yield
    provider_health.reset()
    provider_health.reset_redis_client()


# ---------------------------------------------------------------------------
# (a) Supplementary providers on every unique scenario query
# ---------------------------------------------------------------------------

_QUERIES = [
    "harbour bridge steel arch construction",
    " ferry passengers crossing the harbour",
    "workers riveting steel beams high above water",
]


def _stub_scenario_providers(monkeypatch, *, fail_archive_for=()):
    calls = {"archive": [], "wikimedia_videos": []}

    monkeypatch.setattr(mp, "search_videos_multi", lambda q, limit=8: [])
    monkeypatch.setattr(mp, "search_web_sources", lambda q, limit=5: [])
    monkeypatch.setattr(mp, "search_image_sources", lambda q, limit=5: [])
    monkeypatch.setattr(mp, "search_wikimedia_images", lambda q, limit=3: [])

    def archive(query, limit=5):
        calls["archive"].append((query, limit))
        if query in fail_archive_for:
            raise RuntimeError("archive.org unreachable")
        slug = zlib.crc32(query.encode())
        return [{
            "title": "archive hit", "video_url": f"https://archive.org/details/{slug}",
            "url": f"https://archive.org/details/{slug}",
            "provider": "archive.org", "source": "archive.org",
        }]

    def wikimedia_videos(query, limit=5):
        calls["wikimedia_videos"].append((query, limit))
        slug = zlib.crc32(query.encode())
        return [{
            "title": "commons hit", "video_url": f"https://upload.wikimedia.org/{slug}.mp4",
            "url": f"https://upload.wikimedia.org/{slug}.mp4",
            "provider": "wikimedia", "source": "wikimedia",
        }]

    monkeypatch.setattr(mp, "search_archive_org_videos", archive)
    monkeypatch.setattr(mp, "search_wikimedia_videos", wikimedia_videos)
    return calls


def test_supplementary_providers_run_on_every_unique_query(monkeypatch):
    calls = _stub_scenario_providers(monkeypatch)
    scenario = {"queries": list(_QUERIES), "visual": "harbour bridge"}
    videos, _web, _images = mp._uve_scenario_search(scenario, {}, "gate2-a")

    expected = {" ".join(q.split()) for q in _QUERIES}
    # Every unique query — not just the primary — reached both
    # supplementary providers, at the raised Archive limit of 5
    # (parity with Wikimedia video).
    assert {q for q, _limit in calls["archive"]} == expected
    assert {q for q, _limit in calls["wikimedia_videos"]} == expected
    assert all(limit == 5 for _q, limit in calls["archive"])
    assert all(limit == 5 for _q, limit in calls["wikimedia_videos"])
    # Their hits merged into the scenario's video pool.
    providers = {v.get("provider") for v in videos}
    assert "archive.org" in providers
    assert "wikimedia" in providers


def test_supplementary_failure_is_isolated_per_query(monkeypatch):
    second = " ".join(_QUERIES[1].split())
    calls = _stub_scenario_providers(
        monkeypatch, fail_archive_for={second}
    )
    scenario = {"queries": list(_QUERIES), "visual": "harbour bridge"}
    videos, _web, _images = mp._uve_scenario_search(scenario, {}, "gate2-a2")

    # The failing query's Archive call died alone: the other
    # queries' Archive calls and every Wikimedia-video call ran.
    assert {q for q, _l in calls["archive"]} == {
        " ".join(q.split()) for q in _QUERIES
    }
    assert len(calls["wikimedia_videos"]) == len(_QUERIES)
    assert any(v.get("provider") == "wikimedia" for v in videos)
    failures = mp.drain_discovery_failures()
    assert any(f["provider"] == "archive.org" for f in failures)


# ---------------------------------------------------------------------------
# (b) source_hints for true_crime / history / general
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pack_id", ["true_crime", "history", "general"])
def test_formerly_hintless_packs_now_expose_source_hints(pack_id):
    pack = genre_packs.load_pack(pack_id)
    assert pack is not None
    assert pack.get("source_hints"), f"{pack_id} still has no source_hints"
    assert genre_packs.validate_source_hints(pack["source_hints"]) == []
    resolved = genre_packs.resolve_source_hints(pack)
    assert resolved["failures"] == []
    hints = resolved["hints"]
    for channel in genre_packs.SOURCE_HINT_CHANNELS:
        assert hints.get(channel), f"{pack_id}: no {channel} hints"


@pytest.mark.parametrize("pack_id", ["true_crime", "history", "general"])
def test_new_source_hints_render_with_sample_need(pack_id):
    resolved = genre_packs.resolve_source_hints(
        genre_packs.load_pack(pack_id)
    )
    formatted = genre_packs.format_source_hint_queries(
        resolved["hints"],
        subject="Harbour Bridge",
        place="Sydney",
        era="1932",
        query="harbour bridge opening ceremony",
    )
    assert formatted, f"{pack_id}: no hint rendered for a full need"
    channels = {entry["channel"] for entry in formatted}
    assert "web" in channels
    assert "archive" in channels
    # No placeholder survives formatting.
    assert all("{" not in entry["query"] for entry in formatted)
    assert all(entry["template"] for entry in formatted)


def test_true_crime_hints_are_public_record_scoped():
    resolved = genre_packs.resolve_source_hints(
        genre_packs.load_pack("true_crime")
    )
    web = " ".join(resolved["hints"]["web"])
    assert "site:" in web
    assert any(
        domain in web
        for domain in ("archives.gov", "loc.gov", "fbi.gov")
    )


# ---------------------------------------------------------------------------
# (c) Commons retry-before-backoff
# ---------------------------------------------------------------------------

class _ScriptedResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Client Error", response=self
            )

    def json(self):
        return self._payload


class _ScriptedSession:
    """A requests.Session stand-in replaying a status script."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.headers = {}

    def get(self, url, params=None, timeout=None, stream=False):
        self.calls += 1
        return self._responses.pop(0)


_VIDEO_PAYLOAD = {
    "query": {"pages": {"1": {
        "title": "File:Bridge.webm",
        "videoinfo": [{
            "url": "https://upload.wikimedia.org/bridge.webm",
            "descriptionurl": "https://commons.wikimedia.org/wiki/File:Bridge.webm",
            "derivatives": [],
            "extmetadata": {
                "LicenseShortName": {"value": "CC BY 4.0"},
                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"},
                "Artist": {"value": "Filmer"},
                "ImageDescription": {"value": "bridge"},
            },
        }],
    }}}
}

COMMONS_SCOPE = "commons.wikimedia.org"


def test_commons_429_then_200_succeeds_via_retry(monkeypatch):
    session = _ScriptedSession([
        _ScriptedResponse(429, headers={"Retry-After": "0"}),
        _ScriptedResponse(200, payload=_VIDEO_PAYLOAD),
    ])
    monkeypatch.setattr(licensed_media, "_session", lambda: session)

    results = licensed_media.search_wikimedia_videos("harbour bridge", limit=3)

    assert len(results) == 1
    assert session.calls == 2
    # The transient 429 was ridden out by the delayed retry: it
    # never reached the backoff accounting.
    snap = provider_health.snapshot().get(COMMONS_SCOPE, {})
    assert snap.get("delayed_retries", 0) == 1
    assert snap.get("rate_limited", 0) == 0
    assert not provider_health.is_backed_off(COMMONS_SCOPE)


def test_commons_persistent_429_still_backs_off(monkeypatch):
    session = _ScriptedSession([
        _ScriptedResponse(429, headers={"Retry-After": "0"})
        for _ in range(60)
    ])
    monkeypatch.setattr(licensed_media, "_session", lambda: session)

    # Each search exhausts its bounded retries (1 + 2 attempts) and
    # records ONE rate limit; the scope trips only at its trip
    # count of exhausted requests — persistent throttling still
    # backs off, a single burst does not.
    attempts_per_search = 1 + provider_health.WIKIMEDIA_RETRY_ATTEMPTS
    for index in range(provider_health.WIKIMEDIA_RATE_LIMIT_TRIP_COUNT):
        with pytest.raises(requests.HTTPError):
            licensed_media.search_wikimedia_videos("harbour bridge", limit=3)
        if index == 0:
            assert not provider_health.is_backed_off(COMMONS_SCOPE)
    assert session.calls == (
        attempts_per_search * provider_health.WIKIMEDIA_RATE_LIMIT_TRIP_COUNT
    )
    assert provider_health.is_backed_off(COMMONS_SCOPE)
    snap = provider_health.snapshot()[COMMONS_SCOPE]
    assert snap["rate_limited"] == (
        provider_health.WIKIMEDIA_RATE_LIMIT_TRIP_COUNT
    )
    assert snap["delayed_retries"] == (
        provider_health.WIKIMEDIA_RETRY_ATTEMPTS
        * provider_health.WIKIMEDIA_RATE_LIMIT_TRIP_COUNT
    )


def test_non_wikimedia_scope_trip_count_unchanged():
    assert provider_health.trip_count_for_scope("youtube.com") == (
        provider_health.RATE_LIMIT_TRIP_COUNT
    )
    assert provider_health.trip_count_for_scope(COMMONS_SCOPE) == (
        provider_health.WIKIMEDIA_RATE_LIMIT_TRIP_COUNT
    )
    # Retry-After is honoured but bounded.
    assert provider_health.wikimedia_retry_delay(1, 3600) == (
        provider_health.WIKIMEDIA_MAX_RETRY_DELAY_SECONDS
    )
    assert provider_health.wikimedia_retry_delay(1, 0.25) == 0.25
