#!/usr/bin/env python3
"""MS-7 / F-6 — Multi-source, multi-format end-to-end proof.

Runs the REAL production path with YOUTUBE DISCOVERY DISABLED
(CINEMORA_DISABLED_PROVIDERS=youtube) and NO search substitutions of
any kind:

  narration (Piper) -> story analysis (degraded mode, llm=None — no
  BrewKeg key exists here) -> production scenes -> media engine
  (multi-source discovery: Serper [fails closed without a key],
  archive.org, Wikimedia Commons images + video, targeted
  institutional search -> gate -> verify -> unified format selection
  -> segment evidence -> download -> probe) -> motion graphics ->
  timeline -> renderer -> narration mux -> asset report (authenticity
  / format mix / provider funnel / rights) -> final auditor.

The script mixes visual needs: crowd events (footage), geography
(map), reunification (short archival clip), and human moments
(photographs). Assertions are structural and honest: whatever the
live sources yield is reported; the checks verify the CONTRACT —
YouTube supplied nothing, non-YouTube moving footage completed the
chain with recorded segment evidence, composition and authenticity
are reported separately, and rights evidence covers every asset.

Outputs land in ~/workspace/cinemora-audit/e2e-media/ (outside the repo).
Exit code 0 = all assertions passed.
"""

import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
OUT_ROOT = Path.home() / "workspace" / "cinemora-audit" / "e2e-media"
MEDIA_ROOT = OUT_ROOT / "media"
PROJECT_ID = "e2e-multisource-berlin"
PROJECT_DIR = MEDIA_ROOT / PROJECT_ID

os.environ.setdefault("DATABASE_URL", "sqlite:///" + str(OUT_ROOT / "e2e_ms.sqlite3"))
os.environ.setdefault("BREWKEG_API_KEY", "e2e-no-key")
os.environ.setdefault("SERPER_API_KEY", "e2e-no-key")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ["SOURCE_INTEL_DB"] = str(OUT_ROOT / "source_intel.sqlite3")
os.environ["CINEMORA_DISABLED_PROVIDERS"] = "youtube"

sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

SCRIPT = (
    "On November 9, 1989, crowds of East Berliners gathered at the Berlin Wall "
    "demanding to cross into the West. "
    "Border guards opened the checkpoints, and thousands of people streamed "
    "through Bornholmer Strasse into West Berlin. "
    "The wall formed a border around West Berlin, cutting through streets, "
    "rivers and railway lines. "
    "At the Brandenburg Gate, families separated for twenty eight years embraced "
    "in tears as the world watched. "
    "In 1990 the peaceful revolution swept across Eastern Europe, and Germany "
    "was reunified at last. "
    "Within days, ordinary citizens climbed the wall with hammers and chisels "
    "and began tearing it down piece by piece."
)


def main():
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)

    import jobs
    from services.asset_report import (
        build_asset_report,
        build_rights_manifest,
        write_asset_report,
        write_rights_manifest,
    )
    from services.audio_mixer import add_narration
    from services.final_video_auditor import validate_final_video
    from services.graphics_pipeline import build_motion_graphics_assets
    from services.media_pipeline import process_scene_media, reset_media_caches
    from services.narration import generate_narration
    from services.renderer import render_timeline
    from services.timeline import build_project_timeline
    from services.visual_registry import reset_registry

    # Degraded analysis only (no LLM key in this environment). Discovery
    # is NOT patched: every provider runs for real.
    real_analyze = jobs.analyze_script
    jobs.analyze_script = lambda script, project_id="", cache_path=None, **kw: real_analyze(
        script, project_id=project_id, cache_path=cache_path, llm=None
    )

    reset_registry(PROJECT_ID)
    reset_media_caches()

    narration_file = PROJECT_DIR / "narration.wav"
    generate_narration(text=SCRIPT, output_path=str(narration_file))
    captions = json.loads((PROJECT_DIR / "captions.json").read_text(encoding="utf-8"))
    duration = float(captions["duration"])
    print(f"E2E-MS narration duration: {duration:.2f}s, segments: {len(captions['segments'])}")

    built = jobs.build_production_scenes(SCRIPT, PROJECT_ID, PROJECT_DIR)
    scenes = built["scenes"]
    analysis = built["analysis"]
    for sentence in analysis.get("sentences") or []:
        for scenario in sentence.get("scenarios") or []:
            print(
                "  intent:",
                scenario.get("format_intent"),
                scenario.get("format_intent_confidence"),
                "|",
                (sentence.get("text") or "")[:60],
            )

    processed_scenes = []
    for scene in scenes:
        result = process_scene_media(scene, str(PROJECT_DIR))
        result["duration_seconds"] = duration
        result["scene"] = scene
        processed_scenes.append(result)

    motion_data = {
        "case_bible": analysis.get("bible") or {},
        "scenes": scenes,
        "processed_scenes": processed_scenes,
    }
    motion_graphics = build_motion_graphics_assets(
        motion_data, PROJECT_DIR / "motion_graphics", mode="balanced"
    )
    for graphic in motion_graphics:
        for processed_scene in processed_scenes:
            scene = processed_scene.get("scene") or {}
            if scene.get("scene_number") != graphic.get("scene_number"):
                continue
            visual = dict(graphic)
            visual["type"] = "video"
            visual["source"] = "cinemora_motion_graphics"
            visual["motion_graphic"] = True
            visual["file_path"] = graphic.get("file_path") or graphic.get("path")
            visual["path"] = visual["file_path"]
            processed_scene.setdefault("videos", []).append(visual)
            break
    print(f"E2E-MS motion graphics: {len(motion_graphics)}")

    timeline_result = build_project_timeline(PROJECT_ID, processed_scenes, output_dir=str(MEDIA_ROOT))
    timeline_data = json.loads(Path(timeline_result["timeline_file"]).read_text(encoding="utf-8"))
    render_scenes = timeline_data.get("scenes", [])

    raw_video = render_timeline(render_scenes, str(PROJECT_DIR), expected_duration=duration)
    final_video = PROJECT_DIR / "documentary_final_output.mp4"
    add_narration(str(raw_video), str(narration_file), str(final_video))

    renderer_audit = None
    audit_file = PROJECT_DIR / "renderer_timing_audit.json"
    if audit_file.exists():
        renderer_audit = json.loads(audit_file.read_text(encoding="utf-8"))
    report = build_asset_report(processed_scenes, timeline_result, renderer_audit=renderer_audit)
    write_asset_report(PROJECT_DIR, report)
    manifest = build_rights_manifest(processed_scenes)
    write_rights_manifest(PROJECT_DIR, manifest)

    audit = validate_final_video(str(final_video), asset_report=report)

    accepted = []
    for scene in processed_scenes:
        accepted.extend(scene.get("videos") or [])
        accepted.extend(scene.get("images") or [])
    footage = [
        a for a in accepted
        if (a.get("type") or a.get("visual_type")) == "video" and not a.get("motion_graphic")
    ]

    failures = []

    def check(name, ok, detail=""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")
        if not ok:
            failures.append(name)

    print()
    print("E2E-MS CHECKS:")
    check(
        "final video is 1920x1080 with audio",
        audit["width"] == 1920 and audit["height"] == 1080 and bool(audit.get("audio_codec")),
        f"({audit['width']}x{audit['height']}, audio={audit.get('audio_codec')})",
    )
    check(
        "YouTube supplied ZERO accepted assets (discovery disabled)",
        all(
            (a.get("provider") or a.get("source")) != "youtube" for a in accepted
        ),
        f"(providers: {sorted({a.get('provider') or a.get('source') for a in accepted})})",
    )
    check(
        "non-YouTube moving footage completed the full chain",
        len(footage) >= 1
        and all(a.get("segment_evidence") for a in footage),
        f"(footage: {[(a.get('provider') or a.get('source'), a.get('segment_evidence')) for a in footage]})",
    )
    check(
        "at least two providers discovered candidates",
        len([p for p, c in report["providers"]["funnel"].items() if c.get("discovered")]) >= 2,
        f"(funnel: { {p: c.get('discovered') for p, c in report['providers']['funnel'].items()} })",
    )
    check(
        "format mix accounts for every accepted asset",
        sum(report["format_mix"]["accepted"].values()) == report["total_accepted_assets"],
        f"(mix: {report['format_mix']['accepted']})",
    )
    _subs = report["authenticity"]["substitutions"]
    check(
        "authenticity consistent with substitution counts",
        report["authentic"]
        == (
            _subs["placeholder_clips"] == 0
            and _subs["text_cards"] == 0
            and _subs["unverified_assets"] == 0
        ),
        f"(authenticity: {report['authenticity']})",
    )
    check(
        "auditor consumed the report",
        audit.get("authentic") == report["authentic"],
    )
    check(
        "rights manifest covers every accepted asset",
        len(manifest) == report["total_accepted_assets"],
        f"({len(manifest)} vs {report['total_accepted_assets']})",
    )
    manifest_text = (PROJECT_DIR / "rights_manifest.json").read_text(encoding="utf-8")
    check("no fabricated 'royalty-free' licence anywhere", "royalty-free" not in manifest_text)
    check(
        "sentence assignment accuracy measured",
        report["sentence_assignment_accuracy"] is not None,
        f"(accuracy: {report['sentence_assignment_accuracy']})",
    )

    print()
    print("E2E-MS ASSET REPORT:", json.dumps(report, indent=2))
    print(f"E2E-MS final video: {final_video}")
    if failures:
        print(f"E2E-MS RESULT: FAIL ({len(failures)} checks failed)")
        return 1
    print("E2E-MS RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
