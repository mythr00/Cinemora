from pathlib import Path
import json
import os
import subprocess


def _ffprobe(args):
    result = subprocess.run(
        ["ffprobe", "-v", "error", *args],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "ffprobe failed:\n"
            + result.stderr.strip()
        )

    return result.stdout.strip()


def probe_final_video(video_file):
    video_file = Path(video_file)

    if not video_file.exists():
        raise FileNotFoundError(
            f"Final video not found: {video_file}"
        )

    if video_file.stat().st_size <= 0:
        raise RuntimeError(
            f"Final video is empty: {video_file}"
        )

    duration_text = _ffprobe([
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_file),
    ])

    streams_text = _ffprobe([
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,r_frame_rate",
        "-of",
        "json",
        str(video_file),
    ])

    try:
        duration = float(duration_text)
    except ValueError:
        raise RuntimeError(
            f"Invalid final video duration: {duration_text}"
        )

    data = json.loads(streams_text)

    video_stream = None
    audio_stream = None

    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and video_stream is None:
            video_stream = stream

        if stream.get("codec_type") == "audio" and audio_stream is None:
            audio_stream = stream

    if video_stream is None:
        raise RuntimeError(
            "Final video contains no video stream."
        )

    if audio_stream is None:
        raise RuntimeError(
            "Final video contains no audio stream."
        )

    width = video_stream.get("width")
    height = video_stream.get("height")

    if width != 1920 or height != 1080:
        raise RuntimeError(
            f"Final video resolution is {width}x{height}; "
            "expected 1920x1080."
        )

    return {
        "path": str(video_file),
        "size_bytes": video_file.stat().st_size,
        "duration_seconds": duration,
        "video_codec": video_stream.get("codec_name"),
        "audio_codec": audio_stream.get("codec_name"),
        "width": width,
        "height": height,
        "frame_rate": video_stream.get("r_frame_rate"),
    }


def _env_share_limit(name, default):
    """A share limit (0..1) from the environment, with a sane default.

    Invalid or out-of-range values fall back to the default rather
    than breaking the audit."""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, value))


def _audit_verdict(asset_report):
    """MA-14: a real verdict over the Asset Manager report.

    Before this, the auditor relayed the report's facts as warnings
    and printed "FINAL VIDEO AUDIT PASSED" regardless — a film with
    substitutions or uncovered sentences shipped under a pass. The
    verdict separates FAIL conditions (the production must be
    recorded as failed) from WARNINGS (honest limitations that do
    not fake anything):

    FAIL when —
    - coverage is incomplete: a required sentence has no timeline
      item at all (report["coverage"]["complete"] is False);
    - a prohibited substitution is present: text_cards > 0 or
      placeholder_clips > 0 in the report's substitution counts;
    - accepted assets are missing mandatory evidence: an accepted
      footage asset with no source URL in segment_retrieval, or
      genuine accepted assets unaccounted for by the rights data
      (recorded + unknown licences cover fewer assets than were
      accepted, i.e. an asset is absent from the rights manifest
      accounting).

    WARN (never fail) when —
    - the rotation share of timeline assignments exceeds
      CINEMORA_MAX_ROTATION_SHARE (default 0.5);
    - the positional (evidence-free) share of assignments exceeds
      CINEMORA_MAX_POSITIONAL_SHARE (default 0.25) (MA-35);
    - the placeholder count is unknown (renderer audit missing), so
      the absence of placeholders could not be confirmed;
    - sentences had no verified visual (missing_visuals) — an
      incompleteness fact; the timeline may still cover them with
      identified graphics or labelled rotation.

    Calibration (pinned by tests): the Berlin v5 profile (43/43
    coverage, 0 substitutions, rotation 18/43 ≈ 0.42, rights
    complete) PASSES; the Kohberger profile (23/23 coverage reached
    via ≈0.91 rotation, 0 substitutions, rights complete) PASSES
    with a rotation warning. Rotation is a legitimate, honestly
    labelled mechanism — it warns, it never fails a film by itself.
    """
    failures = []
    warnings = []
    checks = {}

    if not isinstance(asset_report, dict):
        return {
            "passed": True,
            "evaluated": False,
            "failures": failures,
            "warnings": warnings,
            "checks": checks,
        }

    # ---- coverage -------------------------------------------------
    coverage = asset_report.get("coverage")
    if isinstance(coverage, dict):
        uncovered = coverage.get("uncovered_sentences") or []
        complete = coverage.get("complete")
        if complete is None:
            complete = not uncovered
        checks["coverage"] = {
            "complete": bool(complete),
            "uncovered_count": len(uncovered),
        }
        if not complete:
            numbers = ", ".join(
                str(u.get("sentence_number"))
                for u in uncovered[:12]
                if isinstance(u, dict)
            )
            failures.append(
                "coverage_incomplete: "
                f"{len(uncovered)} required sentence(s) uncovered"
                + (f" (sentences {numbers})" if numbers else "")
            )
    else:
        checks["coverage"] = {"complete": None, "uncovered_count": None}

    # ---- prohibited substitutions ---------------------------------
    authenticity = asset_report.get("authenticity") or {}
    substitutions = (
        asset_report.get("substitutions")
        or authenticity.get("substitutions")
        or {}
    )
    text_cards = substitutions.get("text_cards") or 0
    placeholders = substitutions.get("placeholder_clips")
    checks["substitutions"] = {
        "text_cards": text_cards,
        "placeholder_clips": placeholders,
    }
    if int(text_cards) > 0:
        failures.append(
            f"prohibited_substitution: text_cards={int(text_cards)}"
        )
    if placeholders is not None and int(placeholders) > 0:
        failures.append(
            f"prohibited_substitution: placeholder_clips={int(placeholders)}"
        )
    if placeholders is None:
        warnings.append(
            "placeholder_clips unknown (renderer audit unavailable); "
            "absence of placeholders could not be confirmed"
        )

    # ---- mandatory evidence ---------------------------------------
    evidence = {}
    missing_url = [
        entry
        for entry in (asset_report.get("segment_retrieval") or [])
        if isinstance(entry, dict)
        and not str(entry.get("url") or "").strip()
    ]
    evidence["footage_without_source_url"] = len(missing_url)
    if missing_url:
        failures.append(
            "missing_mandatory_evidence: "
            f"{len(missing_url)} accepted footage asset(s) have no source URL"
        )
    total_accepted = asset_report.get("total_accepted_assets")
    rights = asset_report.get("rights") or {}
    if isinstance(total_accepted, int) and rights:
        genuine = (
            total_accepted
            - int(text_cards or 0)
            - int(asset_report.get("generated_graphics") or 0)
        )
        accounted = int(rights.get("recorded_licences") or 0) + int(
            rights.get("unknown_licences") or 0
        )
        evidence["rights_accounted"] = accounted
        evidence["genuine_accepted"] = genuine
        if accounted < genuine:
            failures.append(
                "missing_mandatory_evidence: "
                f"{genuine - accounted} accepted asset(s) absent from "
                "rights manifest accounting"
            )
    checks["evidence"] = evidence

    # ---- rotation share (warning only) -----------------------------
    total_items = int(asset_report.get("timeline_items") or 0)
    rotation = int(asset_report.get("rotation_assignments") or 0)
    rotation_share = (rotation / total_items) if total_items else 0.0
    rotation_limit = _env_share_limit("CINEMORA_MAX_ROTATION_SHARE", 0.5)
    checks["rotation"] = {
        "assignments": rotation,
        "share": round(rotation_share, 4),
        "limit": rotation_limit,
    }
    if total_items and rotation_share > rotation_limit:
        warnings.append(
            f"rotation_share {rotation_share:.2f} exceeds limit "
            f"{rotation_limit:.2f} ({rotation}/{total_items} assignments)"
        )

    # ---- positional share (warning only, MA-35) --------------------
    segment_dims = (
        (asset_report.get("verification_dimensions") or {}).get(
            "segment_evidence"
        )
        or {}
    )
    positional = segment_dims.get("positional_assignments")
    if positional is None:
        # Reports built before assignment-level stamps existed: fall
        # back to the per-asset positional count.
        positional = segment_dims.get("positional_no_contract") or 0
    positional = int(positional or 0)
    positional_share = (positional / total_items) if total_items else 0.0
    positional_limit = _env_share_limit("CINEMORA_MAX_POSITIONAL_SHARE", 0.25)
    checks["positional"] = {
        "assignments": positional,
        "share": round(positional_share, 4),
        "limit": positional_limit,
    }
    if total_items and positional_share > positional_limit:
        warnings.append(
            f"positional_share {positional_share:.2f} exceeds limit "
            f"{positional_limit:.2f} ({positional}/{total_items} "
            "assignments rest on positional_no_contract segments)"
        )

    # ---- missing visuals (informational) ---------------------------
    missing = asset_report.get("missing_visuals") or []
    if missing:
        warnings.append(
            f"missing_visuals: {len(missing)} sentence(s) had no "
            "verified visual (reported, never substituted)"
        )

    return {
        "passed": not failures,
        "evaluated": True,
        "failures": failures,
        "warnings": warnings,
        "checks": checks,
    }


def validate_final_video(
    video_file,
    expected_duration=None,
    max_duration_delta=5.0,
    asset_report=None,
):
    result = probe_final_video(video_file)

    # AUD-23 + founder format correction: the auditor consumes the Asset
    # Manager report. A technically valid file is NOT the same as an
    # authentic documentary — when the report shows substitutions (text
    # cards, placeholders, unverified assets), the audit says so.
    # Photographs, documents, maps and identified generated graphics are
    # legitimate visuals: the audit reports the format mix as
    # composition, never as a failure, and never judges the film by its
    # percentage of moving footage.
    if asset_report is not None:
        authenticity = asset_report.get("authenticity") or {}
        substitutions = authenticity.get("substitutions") or {}
        result["asset_report"] = {
            "total_accepted_assets": asset_report.get("total_accepted_assets"),
            "verified_moving_footage": asset_report.get("verified_moving_footage"),
            "verified_images": asset_report.get("verified_images"),
            "unknown_licensing": asset_report.get("unknown_licensing"),
            "format_mix": (asset_report.get("format_mix") or {}).get("accepted"),
            "authenticity": authenticity,
        }
        result["authentic"] = bool(authenticity.get("authentic"))
        result["format_mix"] = (asset_report.get("format_mix") or {}).get("accepted")
        # Missing visuals are an incompleteness fact, not an
        # authenticity failure: since the placeholder fallback was
        # removed, uncovered sentences are reported, never disguised.
        result["missing_visuals"] = asset_report.get("missing_visuals") or []
        if result["missing_visuals"]:
            print()
            print("!" * 70)
            print(
                "VISUAL COVERAGE: "
                f"{len(result['missing_visuals'])} sentence(s) had no verified "
                "visual (reported in the asset report; no placeholder used): "
                + ", ".join(
                    f"sentence {m.get('sentence_number')}"
                    for m in result["missing_visuals"][:12]
                )
            )
            print("!" * 70)
        if not result["authentic"]:
            result["authenticity_warnings"] = [
                f"{name}={count}"
                for name, count in substitutions.items()
                if count
            ]
            print()
            print("!" * 70)
            print(
                "ASSET AUTHENTICITY: NOT FULLY AUTHENTIC — substitutions present: "
                + ", ".join(result["authenticity_warnings"])
            )
            print("!" * 70)

    # MA-14: the verdict is computed from the report whether or not
    # the technical checks pass — callers (jobs.py) honour it by
    # recording the production as failed when it does not pass.
    result["verdict"] = _audit_verdict(asset_report)
    if result["verdict"]["warnings"]:
        print()
        print("!" * 70)
        print("FINAL VIDEO AUDIT WARNINGS:")
        for warning in result["verdict"]["warnings"]:
            print(f"  - {warning}")
        print("!" * 70)

    if expected_duration is not None:
        expected_duration = float(expected_duration)

        delta = abs(
            result["duration_seconds"] - expected_duration
        )

        result["expected_duration_seconds"] = expected_duration
        result["duration_delta_seconds"] = delta

        if delta > max_duration_delta:
            raise RuntimeError(
                "Final video duration mismatch: "
                f"{result['duration_seconds']:.3f}s vs "
                f"expected {expected_duration:.3f}s "
                f"(delta {delta:.3f}s)."
            )

    print()
    print("=" * 70)
    if result["verdict"]["evaluated"] and not result["verdict"]["passed"]:
        # MA-14: a failed verdict is printed as a failure — the word
        # PASSED is never attached to a film the report condemns.
        print("FINAL VIDEO AUDIT FAILED")
        for failure in result["verdict"]["failures"]:
            print(f"  - {failure}")
    else:
        print("FINAL VIDEO AUDIT PASSED")
    print("=" * 70)
    print(f"Video: {result['path']}")
    print(f"Size: {result['size_bytes']} bytes")
    print(f"Duration: {result['duration_seconds']:.3f}s")

    if expected_duration is not None:
        print(
            f"Expected: {result['expected_duration_seconds']:.3f}s"
        )
        print(
            f"Duration delta: "
            f"{result['duration_delta_seconds']:.3f}s"
        )

    print(
        f"Video: {result['video_codec']} "
        f"{result['width']}x{result['height']}"
    )
    print(f"Frame rate: {result['frame_rate']}")
    print(f"Audio: {result['audio_codec']}")

    return result
