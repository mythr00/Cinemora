"""
Project-scoped visual uniqueness registry.

Sources are scoped per project.
A source may be reused when individual clips are different.
The same exact clip/segment cannot be reused, and two segments of the
same source may not overlap (plus a small padding).

All existing call signatures keep working. New optional arguments:
  can_use(..., start=, end=, source_duration=)
  register(..., start=, end=)
"""


class VisualRegistry:
    def __init__(self, project_id=""):
        self.project_id = str(project_id or "")
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

    # ------------------------------------------------------------ checks

    def is_file_used(self, path):
        return self._scoped(path) in self.used_files

    def is_source_used(self, url):
        return self._scoped(url) in self.used_sources

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
        n = self._scoped(url)
        if not n.endswith("|") and n:
            return n in self.recent_sources[-within:]
        return False

    def segments_for(self, url):
        """Used (start, end) ranges for a source - lets the timestamp
        selector avoid them before it even proposes a segment."""
        return list(self.used_segments.get(self._scoped(url), []))

    def is_segment_used(self, url, start, end, pad=None):
        if not url or start is None or end is None:
            return False
        pad = self.segment_pad if pad is None else pad
        for s, e in self.used_segments.get(self._scoped(url), []):
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
            source_key = self._scoped(url)
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
        return self.usage_counts.get(self._scoped(url), 0)

    # ------------------------------------------------------------ writes

    def register(self, *, path=None, url=None, title=None, clip_key=None,
                 start=None, end=None):
        if path:
            self.used_files.add(self._scoped(path))

        if url:
            n = self._scoped(url)
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