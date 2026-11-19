#!/usr/bin/env python3
"""Stage 10 — End-to-end documentary proof for the corrected Asset Manager.

Runs the REAL production path on a real short script:
  narration (Piper, committed model) -> story analysis -> production
  scenes -> media engine (search, gate, verify, pick, download, probe)
  -> motion graphics -> timeline -> renderer -> narration mux ->
  asset report + rights manifest -> final auditor.

Outputs land in ~/workspace/cinemora-audit/e2e-media/ (outside the repo).

Documented environment substitutions (credential boundaries only — every
Asset Manager code path itself is the production code):
  * Script analysis runs with llm=None (the codebase's own degraded mode):
    no BrewKeg key exists in this environment.
  * search_provider.search_videos is backed by yt-dlp's ytsearch: no
    Serper key exists in this environment. The substitute performs a REAL
    YouTube search and returns the provider's dict shape; gating,
    verification, segment selection, download and probing are untouched
    production code. Serper web/image search run for real and return
    nothing without a key — Wikimedia Commons (keyless, real) carries
    image retrieval alongside.
  * Whisper re-transcription is skipped; narration's own proportional
    captions provide timing (a caption-quality step, not an Asset
    Manager step).

Exit code 0 = all acceptance assertions passed.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
OUT_ROOT = Path.home() / "workspace" / "cinemora-audit" / "e2e-media"
MEDIA_ROOT = OUT_ROOT / "media"
PROJECT_ID = "e2e-berlin-wall"
PROJECT_DIR = MEDIA_ROOT / PROJECT_ID

os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/cinemora-e2e.db")
os.environ.setdefault("BREWKEG_API_KEY", "e2e-no-key")
os.environ.setdefault("SERPER_API_KEY", "e2e-no-key")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ["SOURCE_INTEL_DB"] = str(OUT_ROOT / "source_intel.sqlite3")

sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

SCRIPT = (
    "On November 9, 1989, crowds of East Berliners gathered at the Berlin Wall "
    "demanding to cross into the West. "
    "Border guards opened the checkpoints, and thousands of people streamed "
    "through Bornholmer Strasse into West Berlin. "
    "At the Brandenburg Gate, families separated for twenty eight years embraced "
    "in tears as the world watched. "
    "Within days, ordinary citizens climbed the wall with hammers and chisels "
    "and began tearing it down piece by piece."
)


def ytsearch_videos(query, limit=15):
    """Real YouTube search via yt-dlp (see module docstring)."""
    import shutil

    ytdlp = shutil.which("yt-dlp") or str(Path(sys.executable).parent / "yt-dlp")
    try:
        proc = subprocess.run(
            [ytdlp, "--flat-playlist", "-J", f"ytsearch{limit}:{query}"],
            capture_output=True, text=True, timeout=120,
        )
        data = json.loads(proc.stdout)
    except Exception as error:
        print(f"ytsearch failed for {query!r}: {error}")
        return []
    results = []
    for entry in data.get("entries") or []:
        video_id = entry.get("id")
        if not video_id:
            continue
        url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"
        if not str(url).startswith("http"):
            url = f"https://www.youtube.com/watch?v={video_id}"
        results.append({
            "title": entry.get("title", ""),
            "video_url": url,
            "url": url,
            "snippet": (entry.get("description") or "")[:500],
            "source": entry.get("channel") or entry.get("uploader") or "",
            "date": "",
            "duration": entry.get("duration") or "",
        })
    return results


def main():
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)

    import jobs
    from services import search_provider
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

    # --- credential-boundary substitutions (documented above) ----------
    real_analyze = jobs.analyze_script
    jobs.analyze_script = lambda script, project_id="", cache_path=None, **kw: real_analyze(
        script, project_id=project_id, cache_path=cache_path, llm=None
    )
    search_provider.search_videos = ytsearch_videos

    reset_registry(PROJECT_ID)
    reset_media_caches()

    # --- narration (real Piper) ----------------------------------------
    narration_file = PROJECT_DIR / "narration.wav"
    generate_narration(text=SCRIPT, output_path=str(narration_file))
    captions = json.loads((PROJECT_DIR / "captions.json").read_text(encoding="utf-8"))
    duration = float(captions["duration"])
    print(f"E2E narration duration: {duration:.2f}s, segments: {len(captions['segments'])}")

    # --- story analysis + production scenes (real jobs code) ------------
    built = jobs.build_production_scenes(SCRIPT, PROJECT_ID, PROJECT_DIR)
    scenes = built["scenes"]
    analysis = built["analysis"]

    # --- media engine (real) --------------------------------------------
    processed_scenes = []
    for scene in scenes:
        result = process_scene_media(scene, str(PROJECT_DIR))
        result["duration_seconds"] = duration
        result["scene"] = scene
        processed_scenes.append(result)

    # --- motion graphics (real, failure-isolated) ------------------------
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
    print(f"E2E motion graphics: {len(motion_graphics)}")

    # --- timeline (real) ---------------------------------------------------
    timeline_result = build_project_timeline(PROJECT_ID, processed_scenes, output_dir=str(MEDIA_ROOT))
    timeline_data = json.loads(Path(timeline_result["timeline_file"]).read_text(encoding="utf-8"))
    render_scenes = timeline_data.get("scenes", [])

    # --- render (real) ------------------------------------------------------
    raw_video = render_timeline(render_scenes, str(PROJECT_DIR), expected_duration=duration)
    final_video = PROJECT_DIR / "documentary_final_output.mp4"
    add_narration(str(raw_video), str(narration_file), str(final_video))

    # --- asset report + rights manifest (real) ------------------------------
    renderer_audit = None
    audit_file = PROJECT_DIR / "renderer_timing_audit.json"
    if audit_file.exists():
        renderer_audit = json.loads(audit_file.read_text(encoding="utf-8"))
    report = build_asset_report(processed_scenes, timeline_result, renderer_audit=renderer_audit)
    write_asset_report(PROJECT_DIR, report)
    manifest = build_rights_manifest(processed_scenes)
    write_rights_manifest(PROJECT_DIR, manifest)

    audit = validate_final_video(str(final_video), expected_duration=duration, asset_report=report)

    # --- acceptance assertions ------------------------------------------------
    failures = []

    def check(name, condition, detail=""):
        print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")
        if not condition:
            failures.append(name)

    items = [item for scene in render_scenes for item in scene.get("items", [])]
    check("timeline has items", len(items) >= 1, f"({len(items)} items)")
    check(
        "at least one VERIFIED real asset (footage or image)",
        report["verified_moving_footage"] + report["verified_images"] >= 1,
        f"(footage={report['verified_moving_footage']}, images={report['verified_images']})",
    )
    check(
        "sentence assignment accuracy > 0",
        (report["sentence_assignment_accuracy"] or 0) > 0,
        f"({report['sentence_assignment_accuracy']})",
    )

    assigned = {}
    for scene_result in processed_scenes:
        for sentence in scene_result.get("sentences", []):
            visuals = sentence.get("visuals") or []
            if visuals:
                assigned[sentence.get("sentence_number")] = (
                    visuals[0].get("file_path") or visuals[0].get("path")
                )
    preserved = all(
        item.get("file_path") == assigned.get((item.get("sentence_index") or 0) + 1)
        for item in items
        if item.get("assignment") == "sentence"
    )
    check("timeline preserved verified sentence assignments", preserved)

    check(
        "rights manifest covers every accepted asset",
        len(manifest) == report["total_accepted_assets"],
        f"({len(manifest)} vs {report['total_accepted_assets']})",
    )
    manifest_text = (PROJECT_DIR / "rights_manifest.json").read_text(encoding="utf-8")
    check("no fabricated 'royalty-free' licence anywhere", "royalty-free" not in manifest_text)
    _subs = report["authenticity"]["substitutions"]
    check(
        "authenticity consistent with substitution counts",
        report["authentic"]
        == (
            _subs["placeholder_clips"] == 0
            and _subs["text_cards"] == 0
            and _subs["unverified_assets"] == 0
        ),
        f"(authenticity={report['authenticity']})",
    )
    check(
        "report describes composition (format mix present)",
        isinstance(report.get("format_mix", {}).get("accepted"), dict)
        and sum(report["format_mix"]["accepted"].values()) == report["total_accepted_assets"],
        f"(format_mix={report['format_mix']['accepted']})",
    )
    check(
        "final video is 1920x1080 with audio",
        audit["width"] == 1920 and audit["height"] == 1080 and bool(audit.get("audio_codec")),
        f"({audit['width']}x{audit['height']}, audio={audit.get('audio_codec')})",
    )
    check(
        "auditor consumed the report",
        audit.get("authentic") == report["authentic"],
    )

    print()
    print("E2E ASSET REPORT:", json.dumps(report, indent=2))
    print(f"E2E final video: {final_video}")
    if failures:
        print(f"E2E RESULT: FAILED — {failures}")
        return 1
    print("E2E RESULT: ALL ACCEPTANCE ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
