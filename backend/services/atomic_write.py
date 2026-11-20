"""The single atomic file-write path for stored artefacts (DATA-101 / R3).

Before Stage 4, atomicity was inconsistent across the artefact store:
a handful of writers used the MA-11 pattern (temp file in the SAME
directory + flush + fsync + ``os.replace``) as private inline copies,
while the rest wrote verdict-bearing JSON artefacts with bare
``write_text`` / ``open(..., "w")`` — a crash mid-write truncated
exactly the files that verdicts, certificates and reports are read
back from (Giant E-7b, Ultimate §7.2, Master Review D3.8).

Every text/JSON artefact writer in the live backend now goes through
this module:

- ``write_text_atomic(path, text)`` — the generalised MA-11 pattern.
- ``write_json_atomic(path, obj)`` — serialise FIRST (an
  unserialisable object raises before any file is touched), then the
  same atomic replace.

Guarantees: a reader (or a crash) can only ever observe the previous
complete file or the new complete file; a failed write leaves the
previous file byte-identical and no temp leftovers behind.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def write_text_atomic(path, text, *, encoding="utf-8") -> Path:
    """Write ``text`` to ``path`` atomically (temp + fsync + replace).

    The temp file is created in the destination directory so the
    final ``os.replace`` is a same-filesystem rename. Parent
    directories are created as needed. On any failure the temp file
    is removed and the previous destination (if any) is untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def write_json_atomic(path, obj, *, indent=2, ensure_ascii=False) -> Path:
    """Serialise ``obj`` as JSON and write it atomically.

    Serialisation happens before the filesystem is touched, so an
    unserialisable payload raises with the previous file intact.
    """
    payload = json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii)
    return write_text_atomic(path, payload)
