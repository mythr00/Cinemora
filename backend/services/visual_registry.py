"""
Project-scoped visual uniqueness registry.

Sources are scoped per project.
A source may be reused when individual clips are different.
The same exact clip/segment cannot be reused.
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
        self.max_recent = 12

    def _norm(self, value):
        if not value:
            return ""
        return str(value).strip().lower()

    def _scoped(self, value):
        return f"{self.project_id}|{self._norm(value)}"

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

    def can_use(
        self,
        *,
        path=None,
        url=None,
        title=None,
        clip_key=None,
        allow_source_reuse=False,
    ):
        if path and self.is_file_used(path):
            return False, "already_used_file"

        if clip_key and self.is_clip_used(clip_key):
            return False, "already_used_clip"

        if url and self.is_source_used(url) and not allow_source_reuse:
            return False, "already_used_source"

        if (
            url
            and self.is_recent_source(url, within=8)
            and not allow_source_reuse
        ):
            return False, "recent_source"

        if title and self.is_title_used(title) and not allow_source_reuse:
            return False, "already_used_title"

        return True, "ok"

    def register(self, *, path=None, url=None, title=None, clip_key=None):
        if path:
            self.used_files.add(self._scoped(path))

        if url:
            n = self._scoped(url)
            self.used_sources.add(n)
            self.recent_sources.append(n)
            self.usage_counts[n] = self.usage_counts.get(n, 0) + 1

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
