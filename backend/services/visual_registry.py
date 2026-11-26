"""
Project-scoped visual uniqueness registry.

Sources are scoped per project.
A source may be reused when individual clips are different.
The same exact clip/segment cannot be reused, and two segments of the
same source may not overlap (plus a small padding).

All existing call signatures keep working. New optional arguments:
  can_use(..., start=, end=, source_duration=)
  register(..., start=, end=)

Quality Recovery Phase 2 (workstream C): SOURCE identity is canonical
(services.canonical_media) — the same YouTube video spelled as
youtu.be/X and as watch?v=X, or the same archive.org item as a details
and a download URL, is ONE source for usage counts, recency and
segment ranges. File/path/title identity is unchanged.
"""

try:
    from services.canonical_media import canonical_source_key
except ImportError:  # flat-layout fallback
    from canonical_media import canonical_source_key


class VisualRegistry:
    def __init__(self, project_id=""):
        self.project_id = str(project_id or "")
        # Master Directive Phase 2: the registry is reset per
        # production at the asset stage (jobs), so its creation
        # time anchors the tier ladder's wall-clock guard
        # (services.tier_ladder.EscalationLedger).
        import time as _time

        self.created_at = _time.monotonic()
        self.used_files = set()
        self.used_sources = set()
        self.used_clips = set()
        self.used_titles = set()
        self.recent_sources = []
        self.usage_counts = {}
        self.used_segments = {}  # scoped source url -> [(start, end), ...]
        self.max_recent = 12
        self.max_source_uses = 3

        # Seconds of padding required between two clips of one source.
        self.segment_pad = 3.0

        # Optional higher cap for long sources (documentaries/interviews).
        # None = disabled, so max_source_uses applies everywhere (current
        # behaviour). Example: long_source_cap = 6 with
        # long_source_min_duration = 1800 lets a 30+ minute video supply
        # up to 6 different, non-overlapping clips.
        self.long_source_cap = None
        self.long_source_min_duration = 1800

    def _norm(self, value):
        if not value:
            return ""
        return str(value).strip().lower()

    def _scoped(self, value):
        return f"{self.project_id}|{self._norm(value)}"

    def _scoped_source(self, url):
        """Project-scoped CANONICAL source key (Phase 2): respelled
        URLs of one source share usage counts and segment ranges."""
        return f"{self.project_id}|{canonical_source_key(url)}"

    # ------------------------------------------------------------ checks

    def is_file_used(self, path):
        return self._scoped(path) in self.used_files

    def is_source_used(self, url):
        return self._scoped_source(url) in self.used_sources

    def is_title_used(self, title):
        t = self._norm(title)
        if not t or len(t) < 8:
            return False
        return self._scoped(t) in self.used_titles

    def is_clip_used(self, clip_key):
        if not clip_key:
            return False
        return self._scoped(clip_key) in self.used_clips

    def is_recent_source(self, url, within=6):
        n = self._scoped_source(url)
        if not n.endswith("|") and n:
            return n in self.recent_sources[-within:]
        return False

    def segments_for(self, url):
        """Used (start, end) ranges for a source - lets the timestamp
        selector avoid them before it even proposes a segment."""
        return list(self.used_segments.get(self._scoped_source(url), []))

    def is_segment_used(self, url, start, end, pad=None):
        if not url or start is None or end is None:
            return False
        pad = self.segment_pad if pad is None else pad
        for s, e in self.used_segments.get(self._scoped_source(url), []):
            if start < e + pad and end > s - pad:
                return True
        return False

    def _cap_for(self, source_duration=None):
        if (
            self.long_source_cap
            and source_duration
            and source_duration >= self.long_source_min_duration
        ):
            return max(self.max_source_uses, self.long_source_cap)
        return self.max_source_uses

    def can_use(
        self,
        *,
        path=None,
        url=None,
        title=None,
        clip_key=None,
        allow_source_reuse=False,
        start=None,
        end=None,
        source_duration=None,
    ):
        segment_given = bool(url) and start is not None and end is not None

        if path and self.is_file_used(path):
            return False, "already_used_file"

        if clip_key and self.is_clip_used(clip_key):
            return False, "already_used_clip"

        if segment_given and self.is_segment_used(url, start, end):
            return False, "segment_overlap"

        if url:
            source_key = self._scoped_source(url)
            source_uses = self.usage_counts.get(source_key, 0)

            if source_uses >= self._cap_for(source_duration):
                return False, "source_usage_limit"

            # A different, non-overlapping segment is exactly the
            # "same source, different timestamp" reuse we want.
            if (
                self.is_source_used(url)
                and not allow_source_reuse
                and not segment_given
            ):
                return False, "already_used_source"

        if (
            url
            and self.is_recent_source(url, within=2 if segment_given else 8)
            and not allow_source_reuse
        ):
            return False, "recent_source"

        if title and self.is_title_used(title) and not allow_source_reuse and not segment_given:
            return False, "already_used_title"

        return True, "ok"

    def source_usage_count(self, url):
        if not url:
            return 0
        return self.usage_counts.get(self._scoped_source(url), 0)

    # ------------------------------------------------------------ writes

    def register(self, *, path=None, url=None, title=None, clip_key=None,
                 start=None, end=None):
        if path:
            self.used_files.add(self._scoped(path))

        if url:
            n = self._scoped_source(url)
            self.used_sources.add(n)
            self.recent_sources.append(n)
            self.usage_counts[n] = self.usage_counts.get(n, 0) + 1

            if start is not None and end is not None:
                self.used_segments.setdefault(n, []).append(
                    (float(start), float(end))
                )

            if len(self.recent_sources) > self.max_recent * 3:
                self.recent_sources = self.recent_sources[-self.max_recent * 2:]

        if title:
            t = self._norm(title)
            if t and len(t) >= 8:
                self.used_titles.add(self._scoped(t))

        if clip_key:
            self.used_clips.add(self._scoped(clip_key))

    def summary(self):
        return {
            "project_id": self.project_id,
            "used_files": len(self.used_files),
            "used_sources": len(self.used_sources),
            "used_titles": len(self.used_titles),
            "used_clips": len(self.used_clips),
            "used_segments": sum(len(v) for v in self.used_segments.values()),
        }

    # ------------------------------------------------------------ persistence

    def export_state(self):
        """The registry's usage state as a JSON-safe dict (AM-020,
        folded into Transformation Phase 5.1): everything can_use /
        register consult, so a restored registry enforces reuse
        exactly as the exporting one did. ``created_at`` is a
        monotonic clock reading and is NOT exported — a restored
        registry starts its own clock (the tier ladder's wall-clock
        guard measures the CURRENT run's asset stage).

        Phase 2 (discovery efficiency): when this registry already
        carries a tier-ladder EscalationLedger, its snapshot rides
        along under ``escalation_ledger`` so a same-run resume
        restores the D-B2 escalation accounts instead of restarting
        them from zero. The key is present ONLY when a ledger
        already exists — exporting must never create one as a side
        effect (ledger_for stays the single lazy creator)."""
        state = {
            "project_id": self.project_id,
            "used_files": sorted(self.used_files),
            "used_sources": sorted(self.used_sources),
            "used_clips": sorted(self.used_clips),
            "used_titles": sorted(self.used_titles),
            "recent_sources": list(self.recent_sources),
            "usage_counts": dict(self.usage_counts),
            "used_segments": {
                key: [list(pair) for pair in pairs]
                for key, pairs in self.used_segments.items()
            },
            "max_recent": self.max_recent,
            "max_source_uses": self.max_source_uses,
            "segment_pad": self.segment_pad,
            "long_source_cap": self.long_source_cap,
            "long_source_min_duration": self.long_source_min_duration,
        }
        ledger = getattr(self, "_tier_escalation_ledger", None)
        if ledger is not None:
            state["escalation_ledger"] = ledger.export_state()
        return state

    def restore_state(self, data):
        """Replace this registry's usage state with an exported one
        (see export_state). Malformed pieces fall back to empty —
        a partial restore must never invent usage that blocks a
        candidate, and the caller's artefact hashes have already
        vouched for the payload this state rode in on."""
        if not isinstance(data, dict):
            return self
        self.used_files = set(data.get("used_files") or [])
        self.used_sources = set(data.get("used_sources") or [])
        self.used_clips = set(data.get("used_clips") or [])
        self.used_titles = set(data.get("used_titles") or [])
        self.recent_sources = list(data.get("recent_sources") or [])
        self.usage_counts = {
            str(key): int(value)
            for key, value in (data.get("usage_counts") or {}).items()
        }
        self.used_segments = {
            str(key): [
                (float(pair[0]), float(pair[1]))
                for pair in pairs
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            ]
            for key, pairs in (data.get("used_segments") or {}).items()
        }
        if data.get("max_recent"):
            self.max_recent = int(data["max_recent"])
        if data.get("max_source_uses"):
            self.max_source_uses = int(data["max_source_uses"])
        if data.get("segment_pad") is not None:
            self.segment_pad = float(data["segment_pad"])
        self.long_source_cap = data.get("long_source_cap")
        if data.get("long_source_min_duration"):
            self.long_source_min_duration = data["long_source_min_duration"]
        # Phase 2: restore the tier-ladder escalation ledger when
        # the payload carries one, so the resumed production keeps
        # its D-B2 accounts (searches / downloads already spent)
        # instead of double-spending them. The restored ledger is
        # re-anchored to THIS registry's creation time — a restored
        # registry starts its own clock (see export_state) — and
        # attached where tier_ladder.ledger_for looks for it, so
        # the pipeline resumes on the restored ledger rather than
        # a fresh one. An absent (or non-dict) key leaves any
        # existing ledger untouched. tier_ladder is imported
        # lazily here, the established direction (media_pipeline
        # and tier_ladder import this module's consumers, never
        # the reverse at module scope).
        ledger_data = data.get("escalation_ledger")
        if isinstance(ledger_data, dict):
            try:
                from services import tier_ladder
            except ImportError:  # flat-layout fallback
                import tier_ladder
            ledger = tier_ladder.EscalationLedger()
            ledger.restore_state(ledger_data)
            ledger.asset_stage_started_at = self.created_at
            self._tier_escalation_ledger = ledger
        return self


_registries = {}


def get_registry(project_id=""):
    key = str(project_id or "_default")
    if key not in _registries:
        _registries[key] = VisualRegistry(project_id=key)
    return _registries[key]


def reset_registry(project_id=None):
    if project_id is None:
        _registries.clear()
        _registries["_default"] = VisualRegistry(project_id="_default")
        return _registries["_default"]

    key = str(project_id or "_default")
    _registries[key] = VisualRegistry(project_id=key)
    return _registries[key]