"""Brief Stage I (Milestone 19) — P3 music-library ARCHITECTURE
PREPARATION: the catalogue schema as code.

Covers services/music_library.py ONLY: the track metadata model,
the licence-evidence rules (verified is earned, never asserted),
catalogue load/validate (absent/empty = no library; duplicates,
unknown fields and hash drift rejected), and the preparation
boundary — the live system (jobs, api_v1, editor) must NOT import
the module until the founder's A/B/C sourcing decision activates
it. No catalogue ships; no endpoint exists; these tests exercise
validation against synthetic records in tmp dirs only.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from services import music_library as ml

BACKEND = Path(__file__).resolve().parent.parent

FAKE_SHA = "a" * 64


def _track(**overrides):
    record = {
        "track_id": "somber-strings-01",
        "content_sha256": FAKE_SHA,
        "title": "Somber Strings",
        "composer": "Recorded Composer",
        "performer": None,
        "licence": {"type": "CC0-1.0"},
        "source_class": "bundled",
        "moods": ["somber", "orchestral"],
        "genres": ["history"],
        "energy": 2,
        "duration_seconds": 94.5,
        "probe": {"codec": "mp3", "sample_rate": 44100, "channels": 2},
        "assets": {"full": "audio/somber-strings-01.mp3", "preview": None},
        "loop_start_seconds": None,
        "loop_end_seconds": None,
        "added_at": "2026-11-20",
        "notes": None,
    }
    record.update(overrides)
    return record


def _catalogue(*tracks):
    return {"catalogue_version": 1, "tracks": list(tracks)}


# ---------------------------------------------------------------
# valid records
# ---------------------------------------------------------------

def test_full_record_validates_and_round_trips():
    record = ml.TrackRecord.from_dict(_track())
    assert record.track_id == "somber-strings-01"
    assert record.licence_state == ml.LICENCE_STATE_CLAIMED_UNVERIFIED
    assert ml.validate_catalogue_data(_catalogue(_track())).by_id(
        "somber-strings-01"
    ) == record
    # to_dict -> from_dict is stable (the catalogue file format).
    assert ml.TrackRecord.from_dict(record.to_dict()) == record


def test_minimal_record_validates_with_honest_unknowns():
    record = ml.TrackRecord.from_dict({
        "track_id": "bare",
        "content_sha256": FAKE_SHA,
        "source_class": "provider",
        "assets": {"full": "audio/bare.mp3"},
    })
    assert record.title is None and record.composer is None
    assert record.licence.type == "unknown"
    assert record.licence_state == ml.LICENCE_STATE_UNKNOWN
    assert record.moods == () and record.genres == ()
    assert record.duration_seconds is None and record.probe is None


def test_verified_licence_with_full_evidence_validates():
    record = ml.TrackRecord.from_dict(_track(licence={
        "type": "CC0-1.0",
        "evidence_url": "https://example.org/licence/somber-strings",
        "evidence_note": None,
        "verified": True,
        "verified_by": "founder",
        "verified_at": "2026-11-20",
    }))
    assert record.licence_state == ml.LICENCE_STATE_VERIFIED


def test_attestation_is_a_claim_not_verification():
    # Founder option C: a user's attestation is recorded verbatim
    # as evidence of the CLAIM; verified stays False, so the state
    # is claimed_unverified — never upgraded by the schema.
    record = ml.TrackRecord.from_dict(_track(
        source_class="user_supplied",
        licence={
            "type": "user_attested",
            "evidence_note": "Attested own work at upload, wording v1.",
            "verified": False,
        },
    ))
    assert record.licence_state == ml.LICENCE_STATE_CLAIMED_UNVERIFIED


# ---------------------------------------------------------------
# licence-evidence rules: verified is earned, never asserted
# ---------------------------------------------------------------

def test_verified_without_evidence_rejected():
    with pytest.raises(ml.CatalogueValidationError) as excinfo:
        ml.TrackRecord.from_dict(_track(licence={
            "type": "CC0-1.0", "verified": True,
            "verified_by": "founder", "verified_at": "2026-11-20",
        }))
    assert any("evidence" in p for p in excinfo.value.problems)


def test_verified_without_verifier_or_date_rejected():
    with pytest.raises(ml.CatalogueValidationError) as excinfo:
        ml.TrackRecord.from_dict(_track(licence={
            "type": "CC0-1.0",
            "evidence_url": "https://example.org/deed",
            "verified": True,
        }))
    problems = " ".join(excinfo.value.problems)
    assert "verified_by" in problems and "verified_at" in problems


def test_verified_unknown_type_rejected():
    with pytest.raises(ml.CatalogueValidationError):
        ml.TrackRecord.from_dict(_track(licence={
            "type": "unknown",
            "evidence_url": "https://example.org/deed",
            "verified": True,
            "verified_by": "founder", "verified_at": "2026-11-20",
        }))


def test_misspelt_licence_field_rejected_not_silently_dropped():
    # "verifed" instead of "verified": strict keys mean the typo
    # fails loudly instead of silently downgrading the evidence.
    with pytest.raises(ml.CatalogueValidationError):
        ml.TrackRecord.from_dict(_track(licence={
            "type": "CC0-1.0", "verifed": True,
        }))


# ---------------------------------------------------------------
# structural rules
# ---------------------------------------------------------------

def test_duplicate_track_id_rejected():
    with pytest.raises(ml.CatalogueValidationError) as excinfo:
        ml.validate_catalogue_data(_catalogue(_track(), _track()))
    assert any("duplicate track_id" in p for p in excinfo.value.problems)


@pytest.mark.parametrize("bad", [
    {"track_id": "Bad Slug!"},
    {"track_id": ""},
    {"content_sha256": "xyz"},
    {"content_sha256": "A" * 64},          # uppercase hex rejected
    {"source_class": "pexels"},            # not a sourcing class
    {"duration_seconds": 0},
    {"duration_seconds": -3},
    {"energy": 9},
    {"loop_start_seconds": 10.0, "loop_end_seconds": 5.0},
    {"loop_start_seconds": 10.0},          # loop points: both or neither
    {"assets": {"full": "../escape.mp3"}},
    {"assets": {"full": "/absolute/path.mp3"}},
    {"assets": {"full": "audio\\windows.mp3"}},
    {"moods": ["ok", ""]},
    {"added_at": "not-a-date"},
])
def test_invalid_records_rejected(bad):
    with pytest.raises(ml.CatalogueValidationError):
        ml.TrackRecord.from_dict(_track(**bad))


def test_unknown_track_field_rejected():
    with pytest.raises(ml.CatalogueValidationError):
        ml.TrackRecord.from_dict(_track(licensee="someone"))


# ---------------------------------------------------------------
# catalogue loading: absent/empty means NO library
# ---------------------------------------------------------------

def test_absent_catalogue_loads_as_empty(tmp_path):
    catalogue = ml.load_catalogue(tmp_path / "does-not-exist.json")
    assert catalogue.empty and len(catalogue) == 0


def test_empty_catalogue_documents_load_as_empty(tmp_path):
    for content in ('{"catalogue_version": 1, "tracks": []}', "  \n"):
        path = tmp_path / "catalogue.json"
        path.write_text(content, encoding="utf-8")
        assert ml.load_catalogue(path).empty


def test_malformed_catalogue_rejected(tmp_path):
    path = tmp_path / "catalogue.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ml.CatalogueValidationError):
        ml.load_catalogue(path)
    path.write_text('{"tracks": {"not": "a list"}}', encoding="utf-8")
    with pytest.raises(ml.CatalogueValidationError):
        ml.load_catalogue(path)


def test_load_catalogue_from_file(tmp_path):
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps(_catalogue(_track())), encoding="utf-8")
    catalogue = ml.load_catalogue(path)
    assert len(catalogue) == 1
    assert catalogue.by_id("somber-strings-01") is not None
    assert catalogue.by_id("nope") is None


# ---------------------------------------------------------------
# hash verification: bytes must match the record
# ---------------------------------------------------------------

def test_hash_verification_accepts_matching_bytes(tmp_path):
    payload = b"fake-but-deterministic-audio-bytes"
    asset_root = tmp_path / "library"
    (asset_root / "audio").mkdir(parents=True)
    (asset_root / "audio" / "somber-strings-01.mp3").write_bytes(payload)
    track = _track(content_sha256=hashlib.sha256(payload).hexdigest())
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps(_catalogue(track)), encoding="utf-8")
    catalogue = ml.load_catalogue(path, asset_root=asset_root, verify_hashes=True)
    assert len(catalogue) == 1


def test_hash_mismatch_rejected_when_bytes_available(tmp_path):
    asset_root = tmp_path / "library"
    (asset_root / "audio").mkdir(parents=True)
    (asset_root / "audio" / "somber-strings-01.mp3").write_bytes(b"drifted bytes")
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps(_catalogue(_track())), encoding="utf-8")
    with pytest.raises(ml.CatalogueValidationError) as excinfo:
        ml.load_catalogue(path, asset_root=asset_root, verify_hashes=True)
    assert any("hash mismatch" in p for p in excinfo.value.problems)


def test_hash_verification_requires_asset_root_and_files(tmp_path):
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps(_catalogue(_track())), encoding="utf-8")
    with pytest.raises(ml.CatalogueValidationError):
        ml.load_catalogue(path, verify_hashes=True)
    with pytest.raises(ml.CatalogueValidationError) as excinfo:
        ml.load_catalogue(path, asset_root=tmp_path / "empty", verify_hashes=True)
    assert any("asset not found" in p for p in excinfo.value.problems)


def test_without_verification_missing_bytes_are_not_checked(tmp_path):
    # Plain load validates RECORDS only; byte checks are the
    # explicit ingest step (verify_hashes=True).
    path = tmp_path / "catalogue.json"
    path.write_text(json.dumps(_catalogue(_track())), encoding="utf-8")
    assert len(ml.load_catalogue(path)) == 1


# ---------------------------------------------------------------
# the preparation boundary: NOT wired into the live system
# ---------------------------------------------------------------

def test_live_system_does_not_import_music_library():
    import_pattern = re.compile(r"^\s*(from|import)\s+.*music_library", re.M)
    for relative in ("jobs.py", "api_v1.py", "services/editor.py",
                     "services/music.py"):
        source = (BACKEND / relative).read_text(encoding="utf-8")
        assert not import_pattern.search(source), (
            f"{relative} imports music_library — activation is "
            f"founder-gated (A/B/C) and must not happen in prep"
        )
