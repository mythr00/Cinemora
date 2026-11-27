"""Phase 5 — deterministic render cache (chapter rendering support).

A content-addressed store that lives with a production's media
(``<project_dir>/render_cache/``) and backs the chapter renderer in
``services/renderer.py``:

- **Clips** (``clip-*``): one timeline item's encoded clip, keyed by
  everything that determines it — source content hash, kind, frame
  count, duration, motion / seek offset / retime, render settings
  and the render-code fingerprint.
- **Composed clips** (``clip-*`` namespace, separate key domain):
  a base clip with its composition plan applied, keyed by the base
  clip's content hash + the canonical plan.
- **Segments** (``seg-*``): trimmed mains and transition segments,
  keyed by the input clips' content hashes + the exact frame
  window/kind. The build record (frame counts, filtergraph) is kept
  in the sidecar so a cache hit reproduces the audit entry verbatim.
- **Chapters** (``chapter-*``): one scene's joined body file, keyed
  by the scene's full timeline slice (item fingerprints incl.
  source content hashes and classifications, frame allocations,
  edge/internal transition windows), the narration master hash,
  render settings and the render-code fingerprint. The sidecar
  carries the scene audit and the first/last clip content hashes,
  so a hit restores the chapter without re-rendering the scene.

Safety rules (the key derivation is the safety-critical part):

- When in doubt, an input is IN the key. A changed input always
  misses; a miss only costs a re-render.
- The render-code fingerprint hashes the actual source bytes of
  the modules that determine rendered output (renderer,
  transitions, composition, this module) plus
  ``CHAPTER_CACHE_VERSION`` — any render-code change invalidates
  every entry, as Phase 0 §11 requires ("input + render-code
  hashes").
- Every store operation is fail-soft: a cache problem degrades to
  an ordinary render, never to a failed production. Entries are
  published atomically (unique partial + ``os.replace``), so a
  process killed mid-store can never leave a partial file at a
  cache path; orphaned partials are swept by the pruner.
- Pruning is bounded per production: at render start the cache is
  evicted LRU (by mtime) down to ``CINEMORA_RENDER_CACHE_MAX_BYTES``
  (default 4 GiB). Nothing outside ``render_cache/`` is ever
  touched — final outputs belong to the retention system, not to
  this cache.
"""

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path

CHAPTER_CACHE_VERSION = 1

_DEFAULT_MAX_BYTES = 4 * 1024 * 1024 * 1024

_CODE_MODULES = ("renderer", "transitions", "composition", "render_cache")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def chapter_rendering_enabled():
    return os.getenv("CINEMORA_CHAPTER_RENDERING", "1") != "0"


def cache_enabled():
    return os.getenv("CINEMORA_RENDER_CACHE", "1") != "0"


def cache_max_bytes():
    raw = os.getenv("CINEMORA_RENDER_CACHE_MAX_BYTES")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return _DEFAULT_MAX_BYTES


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

_hash_memo = {}


def hash_file(path):
    """sha256 of a file's bytes, memoised per (path, size, mtime).

    Returns None when the file cannot be read."""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return None
    memo_key = (str(path), stat.st_size, stat.st_mtime_ns)
    cached = _hash_memo.get(memo_key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    value = digest.hexdigest()
    if len(_hash_memo) > 4096:
        _hash_memo.clear()
    _hash_memo[memo_key] = value
    return value


def hash_bytes(data):
    return hashlib.sha256(data).hexdigest()


def hash_json(value):
    """sha256 of a value's canonical JSON (sorted keys)."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_code_fingerprint = None


def render_code_fingerprint():
    """Hash of the render path's own source code + the cache schema
    version. Any change to the code that produces rendered output
    changes this fingerprint and therefore every cache key."""
    global _code_fingerprint
    if _code_fingerprint is not None:
        return _code_fingerprint
    digest = hashlib.sha256()
    digest.update(f"chapter-cache-version:{CHAPTER_CACHE_VERSION}\n".encode())
    services_dir = Path(__file__).resolve().parent
    for name in _CODE_MODULES:
        module_path = services_dir / f"{name}.py"
        try:
            digest.update(module_path.read_bytes())
        except OSError:
            digest.update(f"<unreadable:{name}>".encode())
        digest.update(b"\n")
    _code_fingerprint = digest.hexdigest()
    return _code_fingerprint


# ---------------------------------------------------------------------------
# Canonicalisation for key derivation
# ---------------------------------------------------------------------------

def _looks_like_file(value):
    if not isinstance(value, str) or not value:
        return False
    if "/" not in value and "\\" not in value:
        return False
    try:
        return Path(value).is_file()
    except OSError:
        return False


def canonicalise(value, _depth=0):
    """Canonical, JSON-safe form of a timeline fragment for keying.

    - dict keys sorted by the JSON encoder downstream;
    - any string that names an existing file is replaced by that
      file's content hash (a path is not an input — its bytes are);
    - a composition plan's ``render`` sub-record (an OUTPUT the
      renderer stamps onto the item after rendering) is dropped, so
      re-rendering the same in-process timeline keys identically;
    - everything else is carried verbatim: when in doubt, the
      field is in the key.
    """
    if _depth > 12:
        return str(value)
    if isinstance(value, dict):
        out = {}
        for key in sorted(value, key=str):
            if key == "render" and _depth > 0:
                # Only composition plans gain a "render" record, and
                # it is an output, never an input.
                continue
            out[str(key)] = canonicalise(value[key], _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [canonicalise(entry, _depth + 1) for entry in value]
    if isinstance(value, str) and _looks_like_file(value):
        return {"__file_sha256__": hash_file(value)}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def item_fingerprint(item):
    """Canonical form of one timeline item for chapter/clip keys.

    The item's own media references (``file_path``/``path``) are
    represented by content hash: identical bytes at a different
    path are the same input; changed bytes at the same path are a
    different one."""
    if not isinstance(item, dict):
        return {"__non_dict_item__": str(item)}
    out = {}
    for key in sorted(item, key=str):
        if key in ("file_path", "path"):
            raw = item.get(key)
            if raw:
                out[key] = {"__file_sha256__": hash_file(raw)}
            else:
                out[key] = None
            continue
        out[str(key)] = canonicalise(item[key], _depth=1)
    return out


def chapter_key(slice_payload):
    return hash_json({
        "domain": "chapter",
        "version": CHAPTER_CACHE_VERSION,
        "code": render_code_fingerprint(),
        "slice": slice_payload,
    })


def clip_key(payload):
    return hash_json({
        "domain": "clip",
        "version": CHAPTER_CACHE_VERSION,
        "code": render_code_fingerprint(),
        "clip": payload,
    })


def composed_key(payload):
    return hash_json({
        "domain": "composed-clip",
        "version": CHAPTER_CACHE_VERSION,
        "code": render_code_fingerprint(),
        "clip": payload,
    })


def segment_key(payload):
    return hash_json({
        "domain": "segment",
        "version": CHAPTER_CACHE_VERSION,
        "code": render_code_fingerprint(),
        "segment": payload,
    })


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

class CacheStore:
    """Content-addressed artefact store under one project dir.

    All methods are fail-soft: they return False/None on any
    problem and never raise into the render path."""

    def __init__(self, project_dir):
        self.project_dir = Path(project_dir)
        self.dir = self.project_dir / "render_cache"
        self.stats = {
            "clip_hits": 0, "clip_misses": 0,
            "segment_hits": 0, "segment_misses": 0,
            "chapter_hits": 0, "chapter_misses": 0,
        }

    # -- paths ---------------------------------------------------

    def _entry_path(self, kind, key):
        return self.dir / f"{kind}-{key}.mp4"

    def _sidecar_path(self, kind, key):
        return self.dir / f"{kind}-{key}.json"

    # -- core operations ------------------------------------------

    def lookup(self, kind, key):
        """Path of a cached entry, or None. An entry counts only
        when the media file exists and is non-trivial."""
        if not cache_enabled() or not key:
            return None
        path = self._entry_path(kind, key)
        try:
            if path.is_file() and path.stat().st_size >= 1000:
                return path
        except OSError:
            pass
        return None

    def metadata(self, kind, key):
        path = self._sidecar_path(kind, key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def place(self, kind, key, dest_path):
        """Copy a cached entry onto ``dest_path`` atomically
        (partial sibling + os.replace, the Phase 1 discipline).
        Returns True on success."""
        source = self.lookup(kind, key)
        if source is None:
            return False
        dest_path = Path(dest_path)
        partial = dest_path.with_name(
            f"{dest_path.stem}.partial-{os.getpid()}-"
            f"{uuid.uuid4().hex[:8]}{dest_path.suffix}"
        )
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, partial)
            os.replace(partial, dest_path)
            return True
        except OSError:
            try:
                partial.unlink()
            except OSError:
                pass
            return False

    def put(self, kind, key, source_path, metadata=None):
        """Publish ``source_path``'s bytes as the entry for ``key``
        (atomic partial + os.replace inside the cache dir), with an
        optional JSON sidecar. Returns True on success."""
        if not cache_enabled() or not key:
            return False
        source_path = Path(source_path)
        try:
            if not source_path.is_file() or source_path.stat().st_size < 1000:
                return False
        except OSError:
            return False
        entry = self._entry_path(kind, key)
        partial = self.dir / (
            f"{entry.stem}.partial-{os.getpid()}-"
            f"{uuid.uuid4().hex[:8]}{entry.suffix}"
        )
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, partial)
            os.replace(partial, entry)
        except OSError:
            try:
                partial.unlink()
            except OSError:
                pass
            return False
        if metadata is not None:
            sidecar = self._sidecar_path(kind, key)
            tmp = sidecar.with_name(
                f"{sidecar.name}.partial-{os.getpid()}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            try:
                tmp.write_text(
                    json.dumps(metadata, ensure_ascii=True, default=str),
                    encoding="utf-8",
                )
                os.replace(tmp, sidecar)
            except OSError:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return True

    # -- pruning ---------------------------------------------------

    def prune(self, max_bytes=None):
        """Bound the cache: sweep orphaned partials, then evict
        least-recently-modified entries (media + sidecar together)
        until the total size is within ``max_bytes``. Only files
        inside the cache dir are ever touched. Fail-soft; returns
        the number of entries evicted."""
        if max_bytes is None:
            max_bytes = cache_max_bytes()
        evicted = 0
        try:
            if not self.dir.is_dir():
                return 0
            entries = []
            total = 0
            for path in self.dir.iterdir():
                if ".partial-" in path.name:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                if path.suffix != ".mp4" or not path.is_file():
                    continue
                try:
                    size = path.stat().st_size
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                sidecar = path.with_suffix(".json")
                sidecar_size = 0
                if sidecar.is_file():
                    try:
                        sidecar_size = sidecar.stat().st_size
                    except OSError:
                        sidecar_size = 0
                entries.append((mtime, path, sidecar, size + sidecar_size))
                total += size + sidecar_size
            entries.sort(key=lambda entry: entry[0])
            for _mtime, path, sidecar, size in entries:
                if total <= max_bytes:
                    break
                try:
                    path.unlink()
                except OSError:
                    continue
                try:
                    sidecar.unlink()
                except OSError:
                    pass
                total -= size
                evicted += 1
        except OSError:
            pass
        return evicted
