"""Stage 2E — run manifests: structure, hash integrity, secret hygiene.

A manifest makes a run reproducible on paper: the exact script
(sha256), the options, the provider configuration snapshot (names +
booleans only — never credentials), the verification thresholds, the
LLM model name, and the prompt version ("unversioned" until the
codebase defines one). validate_manifest must catch structural gaps
and any secret-shaped value that ever sneaks in.
"""

import hashlib
import json

from services import run_ledger


def test_initial_manifest_shape_and_hash():
    script = "First sentence. Second sentence! Third one?"
    manifest = run_ledger.build_initial_manifest(
        "run-123",
        "proj-9",
        script,
        options={"entrypoint": "test"},
        sentence_count=3,
    )

    assert manifest["run_id"] == "run-123"
    assert manifest["project_id"] == "proj-9"
    assert manifest["script_hash"] == hashlib.sha256(
        script.encode("utf-8")
    ).hexdigest()
    assert manifest["sentence_count"] == 3
    assert manifest["status"] == "running"
    assert manifest["finished_at"] is None

    # Provider snapshot: names + configuration booleans, no values
    # that could be credentials.
    providers = manifest["providers"]
    assert set(providers) == {
        "searxng",
        "archive_org",
        "wikimedia_commons",
        "youtube",
    }
    assert isinstance(providers["searxng"]["enabled"], bool)

    # The REAL verifier thresholds and LLM model name, read from the
    # modules that own them.
    from services.llm_client import BREWKEG_MODEL
    from services.story_understanding import ACCEPT_THRESHOLD

    assert manifest["verification_thresholds"] == dict(ACCEPT_THRESHOLD)
    assert manifest["llm_model"] == BREWKEG_MODEL
    # story_understanding defines no prompt-version constant; the
    # honest record is "unversioned".
    assert manifest["prompt_version"] == "unversioned"
    assert manifest["rights_policy"] == "allow_all"

    assert run_ledger.validate_manifest(manifest) == []


def test_validate_manifest_flags_structure_and_hash():
    problems = run_ledger.validate_manifest({"run_id": "x"})
    assert any("missing required key" in p for p in problems)

    manifest = run_ledger.build_initial_manifest(
        "r", "p", "Script.", sentence_count=1
    )
    manifest["script_hash"] = "abc123"  # truncated display hash
    problems = run_ledger.validate_manifest(manifest)
    assert any("script_hash" in p for p in problems)


def test_validate_manifest_flags_planted_secrets():
    manifest = run_ledger.build_initial_manifest(
        "r", "p", "Script.", sentence_count=1
    )
    assert run_ledger.validate_manifest(manifest) == []

    planted = dict(manifest)
    planted["options"] = {"note": "key is bk_live_0123456789abcdefXYZ"}
    problems = run_ledger.validate_manifest(planted)
    assert any("secret-shaped" in p for p in problems)

    planted_hex = dict(manifest)
    planted_hex["options"] = {
        # An API-key-style 40-char hex key.
        "api": "0123456789abcdef0123456789abcdef01234567"
    }
    problems = run_ledger.validate_manifest(planted_hex)
    assert any("secret-shaped" in p for p in problems)

    # The script_hash itself is a 64-char hex digest and must NOT be
    # flagged (it is checked by format, exempt from the scan).
    assert run_ledger.validate_manifest(manifest) == []


def test_enrich_and_write_round_trip(tmp_path):
    manifest = run_ledger.build_initial_manifest(
        "run-7", "proj-7", "Some script.", sentence_count=1
    )
    report = {
        "total_accepted_assets": 3,
        "missing_visual_count": 1,
        "candidate_records": {
            "by_outcome": {"accepted": 1, "rejected": 2},
            "records": [
                {"candidate_id": "cand-a", "outcome": "accepted"},
                {"candidate_id": "cand-b", "outcome": "rejected"},
                {"candidate_id": None, "outcome": "accepted"},
            ],
        },
    }
    enriched = run_ledger.enrich_manifest(
        manifest,
        status="completed",
        execution={"wall_time_seconds": 42.0},
        asset_report=report,
        stage_outputs=["narration.wav", "timeline.json"],
    )
    assert enriched["status"] == "completed"
    assert enriched["finished_at"] is not None
    assert enriched["execution"] == {"wall_time_seconds": 42.0}
    assert enriched["accepted_candidate_ids"] == ["cand-a"]
    assert enriched["verification_decisions"] == {
        "accepted": 1,
        "rejected": 2,
    }
    assert enriched["stage_outputs"] == ["narration.wav", "timeline.json"]
    # The initial manifest is not mutated.
    assert manifest["status"] == "running"
    assert run_ledger.validate_manifest(enriched) == []

    written = run_ledger.write_manifest(tmp_path, enriched)
    assert written is not None and written.exists()
    on_disk = json.loads(written.read_text(encoding="utf-8"))
    assert on_disk == enriched
    # Atomic write: no temp files are left behind.
    assert list(tmp_path.glob(".run_manifest.*.tmp")) == []
