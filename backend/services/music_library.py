"""Music library — catalogue schema & validation (Brief Stage I,
Milestone 19: P3 ARCHITECTURE PREPARATION ONLY).

This module is the PREPARATION half of the music library. It
defines the track metadata model, the licence-evidence rules and
a catalogue-file loader/validator — and nothing else:

- NO catalogue ships with the codebase (no catalogue file, no
  tracks, no assets). An absent catalogue is valid and means
  "no library".
- NOTHING imports this module in the live system: jobs.py,
  api_v1.py and services/editor.py do not reference it (a test
  pins that). There is no live endpoint, no selection logic, no
  frontend picker.
- ACTIVATION is founder-gated on the sourcing decision
  (A: bundled CC0/public-domain starter set; B: commercial
  provider; C: first-class user-supplied with attestation) — see
  ~/workspace/cinemora-audit/MUSIC_LIBRARY_DESIGN.md. This module
  deliberately does not pre-decide any of the three: the schema
  carries all three source classes and no provider code exists.

The honesty rules mirror the rest of the system (M6, the Asset
Manager directive): title/composer are RECORDED metadata, nullable
and never invented; a licence is a claim plus evidence, and a
claim without evidence can never validate as verified; unknown
stays unknown.

Catalogue file format (JSON):

    {
      "catalogue_version": 1,
      "tracks": [ {track record}, ... ]
    }

Track record fields (unknown keys are REJECTED — a typo in a
licence field must fail loudly, never silently downgrade the
evidence):

    track_id          required, stable slug ^[a-z0-9][a-z0-9_-]{0,63}$
    content_sha256    required, 64 lowercase hex chars
    title             recorded title or null (never invented)
    composer          recorded composer or null
    performer         recorded performer/artist or null
    licence           object, see LicenceRecord (absent = unknown)
    source_class      "bundled" | "provider" | "user_supplied"
    moods             list of tag strings (may be empty)
    genres            list of tag strings (may be empty)
    energy            optional int 1..5 or null (catalogue metadata
                      for P4 mix presets; unused by selection v1)
    duration_seconds  probed duration > 0, or null when unprobed
    probe             {codec, sample_rate, channels} or null —
                      same fact shape as services.music.probe_
                      audio_file; nulls are honest unknowns
    assets            {"full": relative/path.mp3,
                       "preview": relative/path.mp3 | null}
                      paths are relative to the catalogue asset
                      root, never absolute, never with ".."
    loop_start_seconds / loop_end_seconds
                      optional loop points for P4 crossfaded
                      loops; both or neither, end > start
    added_at          ISO date/datetime the record was catalogued
                      or null
    notes             free-text recorded note or null
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath

CATALOGUE_FORMAT_VERSION = 1

# The three sourcing classes — one per founder option (A/B/C).
# The schema carries all three; activation decides which exist.
SOURCE_BUNDLED = "bundled"            # founder option A
SOURCE_PROVIDER = "provider"          # founder option B
SOURCE_USER_SUPPLIED = "user_supplied"  # founder option C
SOURCE_CLASSES = (SOURCE_BUNDLED, SOURCE_PROVIDER, SOURCE_USER_SUPPLIED)

# The value services.music's SOURCE_* family gains at activation:
# a soundtrack identity built from a catalogue selection carries
# source_type == LIBRARY_SOURCE_TYPE (design doc §3).
LIBRARY_SOURCE_TYPE = "library_track"

# Licence display states (design doc §1): exactly what the API and
# frontend may claim about a track's licence.
LICENCE_STATE_VERIFIED = "verified"
LICENCE_STATE_CLAIMED_UNVERIFIED = "claimed_unverified"
LICENCE_STATE_UNKNOWN = "unknown"

_TRACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_TRACK_KEYS = {
    "track_id", "content_sha256", "title", "composer", "performer",
    "licence", "source_class", "moods", "genres", "energy",
    "duration_seconds", "probe", "assets",
    "loop_start_seconds", "loop_end_seconds", "added_at", "notes",
}
_LICENCE_KEYS = {
    "type", "evidence_url", "evidence_note",
    "verified", "verified_by", "verified_at",
}
_ASSET_KEYS = {"full", "preview"}
_PROBE_KEYS = {"codec", "sample_rate", "channels"}
_CATALOGUE_KEYS = {"catalogue_version", "tracks"}


class CatalogueValidationError(ValueError):
    """A catalogue or track record failed validation. Carries the
    full problem list — ingest should surface every problem at
    once, not fail one track at a time."""

    def __init__(self, problems):
        self.problems = [str(p) for p in problems]
        super().__init__("; ".join(self.problems))


# ---------------------------------------------------------------
# small validation helpers
# ---------------------------------------------------------------

def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid_iso_datetime(value):
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    try:
        date.fromisoformat(text)
        return True
    except ValueError:
        pass
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _check_unknown_keys(data, allowed, context, problems):
    for key in sorted(set(data) - allowed):
        problems.append(f"{context}: unknown field {key!r} (rejected — "
                        f"a misspelt evidence field must not pass silently)")


def _optional_text(data, key, context, problems):
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        problems.append(f"{context}: {key} must be a string or null")
        return None
    return value


def _tag_list(data, key, context, problems):
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append(f"{context}: {key} must be a list of strings")
        return []
    tags = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            problems.append(f"{context}: {key} entries must be non-empty strings")
            return []
        tags.append(item.strip())
    return tags


def _safe_relative_path(value, context, problems):
    """An asset path must be relative, POSIX-style and contained:
    no absolute paths, no '..', no backslashes, no empty parts."""
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{context}: asset path must be a non-empty string")
        return None
    if "\\" in value:
        problems.append(f"{context}: asset path {value!r} must use '/' separators")
        return None
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        problems.append(
            f"{context}: asset path {value!r} must be relative and contained "
            f"(no leading '/', no '..')"
        )
        return None
    return value


def _file_sha256(path):
    """Local copy of the hashing loop (kept dependency-free so this
    module never imports the live music service — preparation must
    not couple to the pipeline it is not yet part of)."""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


# ---------------------------------------------------------------
# model
# ---------------------------------------------------------------

@dataclass(frozen=True)
class LicenceRecord:
    """A licence CLAIM plus its evidence. `verified` is earned,
    never asserted: validation refuses verified=True without a
    concrete licence type, evidence (URL or note), a verifier and
    a verification date. An attestation (option C) is recorded as
    evidence_note with verified left False — a user's claim about
    their own track is evidence of the claim, not verification."""

    type: str = "unknown"
    evidence_url: str | None = None
    evidence_note: str | None = None
    verified: bool = False
    verified_by: str | None = None
    verified_at: str | None = None

    @property
    def state(self):
        if self.verified:
            return LICENCE_STATE_VERIFIED
        if self.type and self.type != "unknown":
            return LICENCE_STATE_CLAIMED_UNVERIFIED
        return LICENCE_STATE_UNKNOWN

    def to_dict(self):
        return {
            "type": self.type,
            "evidence_url": self.evidence_url,
            "evidence_note": self.evidence_note,
            "verified": self.verified,
            "verified_by": self.verified_by,
            "verified_at": self.verified_at,
        }


@dataclass(frozen=True)
class TrackAssets:
    full: str
    preview: str | None = None

    def to_dict(self):
        return {"full": self.full, "preview": self.preview}


@dataclass(frozen=True)
class TrackProbe:
    codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None

    def to_dict(self):
        return {
            "codec": self.codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
        }


@dataclass(frozen=True)
class TrackRecord:
    track_id: str
    content_sha256: str
    source_class: str
    assets: TrackAssets
    title: str | None = None
    composer: str | None = None
    performer: str | None = None
    licence: LicenceRecord = field(default_factory=LicenceRecord)
    moods: tuple = ()
    genres: tuple = ()
    energy: int | None = None
    duration_seconds: float | None = None
    probe: TrackProbe | None = None
    loop_start_seconds: float | None = None
    loop_end_seconds: float | None = None
    added_at: str | None = None
    notes: str | None = None

    @property
    def licence_state(self):
        """The only licence claim downstream surfaces may make."""
        return self.licence.state

    def to_dict(self):
        return {
            "track_id": self.track_id,
            "content_sha256": self.content_sha256,
            "title": self.title,
            "composer": self.composer,
            "performer": self.performer,
            "licence": self.licence.to_dict(),
            "source_class": self.source_class,
            "moods": list(self.moods),
            "genres": list(self.genres),
            "energy": self.energy,
            "duration_seconds": self.duration_seconds,
            "probe": self.probe.to_dict() if self.probe else None,
            "assets": self.assets.to_dict(),
            "loop_start_seconds": self.loop_start_seconds,
            "loop_end_seconds": self.loop_end_seconds,
            "added_at": self.added_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data):
        record, problems = _validate_track(data, context="track")
        if problems:
            raise CatalogueValidationError(problems)
        return record


@dataclass(frozen=True)
class Catalogue:
    tracks: tuple = ()
    version: int = CATALOGUE_FORMAT_VERSION

    def __len__(self):
        return len(self.tracks)

    @property
    def empty(self):
        return not self.tracks

    def by_id(self, track_id):
        for track in self.tracks:
            if track.track_id == track_id:
                return track
        return None

    def to_dict(self):
        return {
            "catalogue_version": self.version,
            "tracks": [track.to_dict() for track in self.tracks],
        }


# ---------------------------------------------------------------
# validation
# ---------------------------------------------------------------

def _validate_licence(data, context, problems):
    if data is None:
        return LicenceRecord()
    if not isinstance(data, dict):
        problems.append(f"{context}: licence must be an object or null")
        return LicenceRecord()
    _check_unknown_keys(data, _LICENCE_KEYS, f"{context}.licence", problems)
    licence_type = data.get("type")
    if licence_type is None:
        licence_type = "unknown"
    if not isinstance(licence_type, str) or not licence_type.strip():
        problems.append(f"{context}.licence: type must be a non-empty string")
        licence_type = "unknown"
    verified = data.get("verified")
    if verified is None:
        verified = False
    if not isinstance(verified, bool):
        problems.append(f"{context}.licence: verified must be a boolean")
        verified = False
    record = LicenceRecord(
        type=licence_type,
        evidence_url=_optional_text(data, "evidence_url", f"{context}.licence", problems),
        evidence_note=_optional_text(data, "evidence_note", f"{context}.licence", problems),
        verified=verified,
        verified_by=_optional_text(data, "verified_by", f"{context}.licence", problems),
        verified_at=_optional_text(data, "verified_at", f"{context}.licence", problems),
    )
    if record.verified:
        # THE licence-evidence rule: verified is earned, never
        # asserted. All four supports must be present.
        if record.type == "unknown":
            problems.append(
                f"{context}.licence: verified=True requires a concrete "
                f"licence type (got 'unknown')"
            )
        if not (record.evidence_url or record.evidence_note):
            problems.append(
                f"{context}.licence: verified=True requires evidence "
                f"(evidence_url or evidence_note)"
            )
        if not record.verified_by:
            problems.append(
                f"{context}.licence: verified=True requires verified_by"
            )
        if not record.verified_at:
            problems.append(
                f"{context}.licence: verified=True requires verified_at"
            )
        elif not _valid_iso_datetime(record.verified_at):
            problems.append(
                f"{context}.licence: verified_at must be an ISO date/datetime"
            )
    elif record.verified_at is not None and not _valid_iso_datetime(record.verified_at):
        problems.append(
            f"{context}.licence: verified_at must be an ISO date/datetime"
        )
    return record


def _validate_track(data, context="track"):
    problems = []
    if not isinstance(data, dict):
        return None, [f"{context}: track record must be an object"]
    track_id = data.get("track_id")
    if isinstance(track_id, str) and track_id:
        context = f"track {track_id!r}"
    _check_unknown_keys(data, _TRACK_KEYS, context, problems)

    if not isinstance(track_id, str) or not _TRACK_ID_RE.match(track_id or ""):
        problems.append(
            f"{context}: track_id must match ^[a-z0-9][a-z0-9_-]{{0,63}}$ "
            f"(stable slug; got {track_id!r})"
        )
    content_sha256 = data.get("content_sha256")
    if not isinstance(content_sha256, str) or not _SHA256_RE.match(content_sha256 or ""):
        problems.append(
            f"{context}: content_sha256 must be 64 lowercase hex chars"
        )
    source_class = data.get("source_class")
    if source_class not in SOURCE_CLASSES:
        problems.append(
            f"{context}: source_class must be one of {list(SOURCE_CLASSES)} "
            f"(got {source_class!r})"
        )

    licence = _validate_licence(data.get("licence"), context, problems)

    assets_data = data.get("assets")
    assets = None
    if not isinstance(assets_data, dict):
        problems.append(f"{context}: assets must be an object with a 'full' path")
    else:
        _check_unknown_keys(assets_data, _ASSET_KEYS, f"{context}.assets", problems)
        full = _safe_relative_path(assets_data.get("full"), f"{context}.assets.full", problems)
        preview = assets_data.get("preview")
        if preview is not None:
            preview = _safe_relative_path(preview, f"{context}.assets.preview", problems)
        if full:
            assets = TrackAssets(full=full, preview=preview)

    probe = None
    probe_data = data.get("probe")
    if probe_data is not None:
        if not isinstance(probe_data, dict):
            problems.append(f"{context}: probe must be an object or null")
        else:
            _check_unknown_keys(probe_data, _PROBE_KEYS, f"{context}.probe", problems)
            sample_rate = probe_data.get("sample_rate")
            channels = probe_data.get("channels")
            if sample_rate is not None and (not isinstance(sample_rate, int) or isinstance(sample_rate, bool) or sample_rate <= 0):
                problems.append(f"{context}.probe: sample_rate must be a positive int or null")
                sample_rate = None
            if channels is not None and (not isinstance(channels, int) or isinstance(channels, bool) or channels <= 0):
                problems.append(f"{context}.probe: channels must be a positive int or null")
                channels = None
            probe = TrackProbe(
                codec=_optional_text(probe_data, "codec", f"{context}.probe", problems),
                sample_rate=sample_rate,
                channels=channels,
            )

    duration = data.get("duration_seconds")
    if duration is not None:
        if not _is_number(duration) or duration <= 0:
            problems.append(f"{context}: duration_seconds must be a number > 0 or null")
            duration = None
        else:
            duration = float(duration)

    energy = data.get("energy")
    if energy is not None:
        if not isinstance(energy, int) or isinstance(energy, bool) or not (1 <= energy <= 5):
            problems.append(f"{context}: energy must be an int 1..5 or null")
            energy = None

    loop_start = data.get("loop_start_seconds")
    loop_end = data.get("loop_end_seconds")
    for key, value in (("loop_start_seconds", loop_start), ("loop_end_seconds", loop_end)):
        if value is not None and (not _is_number(value) or value < 0):
            problems.append(f"{context}: {key} must be a number >= 0 or null")
    if (loop_start is None) != (loop_end is None):
        problems.append(f"{context}: loop_start_seconds and loop_end_seconds must be set together")
    elif loop_start is not None and loop_end is not None and _is_number(loop_start) and _is_number(loop_end) and loop_end <= loop_start:
        problems.append(f"{context}: loop_end_seconds must be greater than loop_start_seconds")

    added_at = _optional_text(data, "added_at", context, problems)
    if added_at is not None and not _valid_iso_datetime(added_at):
        problems.append(f"{context}: added_at must be an ISO date/datetime or null")

    title = _optional_text(data, "title", context, problems)
    composer = _optional_text(data, "composer", context, problems)
    performer = _optional_text(data, "performer", context, problems)
    notes = _optional_text(data, "notes", context, problems)
    moods = _tag_list(data, "moods", context, problems)
    genres = _tag_list(data, "genres", context, problems)

    if problems:
        return None, problems
    return TrackRecord(
        track_id=track_id,
        content_sha256=content_sha256,
        source_class=source_class,
        assets=assets,
        title=title,
        composer=composer,
        performer=performer,
        licence=licence,
        moods=tuple(moods),
        genres=tuple(genres),
        energy=energy,
        duration_seconds=duration,
        probe=probe,
        loop_start_seconds=float(loop_start) if _is_number(loop_start) else None,
        loop_end_seconds=float(loop_end) if _is_number(loop_end) else None,
        added_at=added_at,
        notes=notes,
    ), []


def validate_catalogue_data(data):
    """Validate an already-parsed catalogue document. Raises
    CatalogueValidationError listing every problem found."""
    if not isinstance(data, dict):
        raise CatalogueValidationError(["catalogue: document must be a JSON object"])
    problems = []
    _check_unknown_keys(data, _CATALOGUE_KEYS, "catalogue", problems)
    version = data.get("catalogue_version", CATALOGUE_FORMAT_VERSION)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        problems.append("catalogue: catalogue_version must be a positive int")
        version = CATALOGUE_FORMAT_VERSION
    raw_tracks = data.get("tracks")
    if raw_tracks is None:
        raw_tracks = []
    if not isinstance(raw_tracks, list):
        problems.append("catalogue: tracks must be a list")
        raw_tracks = []
    tracks = []
    seen_ids = {}
    for index, raw in enumerate(raw_tracks):
        record, track_problems = _validate_track(raw, context=f"tracks[{index}]")
        problems.extend(track_problems)
        if record is None:
            continue
        if record.track_id in seen_ids:
            problems.append(
                f"track {record.track_id!r}: duplicate track_id "
                f"(first at tracks[{seen_ids[record.track_id]}])"
            )
            continue
        seen_ids[record.track_id] = index
        tracks.append(record)
    if problems:
        raise CatalogueValidationError(problems)
    return Catalogue(tracks=tuple(tracks), version=version)


def load_catalogue(path, *, asset_root=None, verify_hashes=False):
    """Load and validate a catalogue JSON file.

    - An ABSENT file is valid: it returns an empty Catalogue
      ("no library"). A whitespace-only file likewise. NO default
      catalogue ships with the codebase, and none is implied.
    - Malformed JSON, a non-object document or any invalid record
      raises CatalogueValidationError with every problem listed.
    - With verify_hashes=True (the ingest path), asset_root is
      required and every track's `assets.full` must exist under it
      with bytes whose SHA-256 equals the recorded content_sha256
      — a catalogue whose bytes drifted from its records is
      rejected, never silently trusted.
    """
    path = Path(path)
    if not path.is_file():
        return Catalogue()
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return Catalogue()
    try:
        data = json.loads(text)
    except ValueError as error:
        raise CatalogueValidationError([f"catalogue: invalid JSON ({error})"]) from error
    catalogue = validate_catalogue_data(data)
    if verify_hashes:
        if asset_root is None:
            raise CatalogueValidationError(
                ["catalogue: verify_hashes=True requires asset_root"]
            )
        asset_root = Path(asset_root)
        problems = []
        for track in catalogue.tracks:
            asset_path = asset_root / track.assets.full
            if not asset_path.is_file():
                problems.append(
                    f"track {track.track_id!r}: asset not found under asset root: "
                    f"{track.assets.full}"
                )
                continue
            actual = _file_sha256(asset_path)
            if actual != track.content_sha256:
                problems.append(
                    f"track {track.track_id!r}: content hash mismatch — record "
                    f"claims {track.content_sha256}, bytes hash to {actual}"
                )
        if problems:
            raise CatalogueValidationError(problems)
    return catalogue
