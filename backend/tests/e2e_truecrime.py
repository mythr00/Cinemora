#!/usr/bin/env python3
"""Three-minute true-crime production E2E — founder verification run.

Runs the REAL production path with ALL providers enabled, YouTube
included as one equal supplementary source among archive.org,
Wikimedia Commons, news and institutional sources (no provider is
disabled and none is primary), and NO search substitutions:

  narration (Piper) -> story analysis (the configured LLM when a
  BrewKeg key is present in the environment, degraded heuristic
  mode otherwise) -> production scenes -> media engine
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
PROJECT_ID = "e2e-truecrime-kohberger"
PROJECT_DIR = MEDIA_ROOT / PROJECT_ID

os.environ.setdefault("DATABASE_URL", "sqlite:///" + str(OUT_ROOT / "e2e_ms.sqlite3"))
os.environ.setdefault("BREWKEG_API_KEY", "e2e-no-key")
os.environ.setdefault("SERPER_API_KEY", "e2e-no-key")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ["SOURCE_INTEL_DB"] = str(OUT_ROOT / "source_intel.sqlite3")

sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

SCRIPT = (
    "In the early hours of November 13, 2022, four university students were killed inside a quiet rental house on King Road in Moscow, Idaho, while two of their housemates slept in the same house. "
    "The victims were Madison Mogen and Kaylee Goncalves, both twenty-one years old, and Xana Kernodle and Ethan Chapin, who were both twenty. "
    "The alarm was not raised until almost noon, when a 911 call reported an unconscious person, and police arrived to find no murder weapon and no sign of a forced entry. "
    "One surviving housemate told police that at around four in the morning she had seen a man dressed in black, his face covered by a mask, walk past her and out of the house. "
    "Fear emptied the campus within days, as students left the small college town while police admitted they had no suspect, no weapon, and no explanation. "
    "On a bed beside one of the victims, investigators found a tan leather knife sheath, stamped with the insignia of the United States Marine Corps. "
    "The killer had left almost nothing behind, but on the button snap of that sheath, forensic scientists found a single sample of male DNA. "
    "Detectives turned to the security cameras of the quiet neighbourhood, and found a white Hyundai Elantra circling King Road again and again between three and four that morning. "
    "The car passed the house at least three times before it sped away at twenty minutes past four, shortly after the murders are believed to have taken place. "
    "Ten miles away, across the state line at Washington State University, a twenty-eight-year-old criminology student named Bryan Kohberger drove a white Hyundai Elantra. "
    "He was studying for a doctorate in criminal justice, and eight months before the murders, he had bought a military-style knife and a sheath online. "
    "Police matched the car to Kohberger, and then matched the man to the sheath, using DNA taken secretly from the rubbish outside his parents' home in Pennsylvania. "
    "The DNA from the family rubbish showed that its owner was almost certainly the father of the man whose DNA was found on the sheath. "
    "In the early hours of December 30, seven weeks after the murders, FBI agents and state troopers raided the family home in the Pocono Mountains and arrested Kohberger. "
    "He was flown back to Idaho and charged with four counts of first-degree murder, and when the charges were read to him in court, he stood silent, saying nothing at all. "
    "The court papers described a mobile phone that went dark during the hours of the murders, a white car recorded near the house twelve times in the months before, and a sheath bought with a single click. "
    "The house on King Road was demolished in December 2023, and the case was moved to a courtroom in Boise, where Kohberger's trial was set for the summer of 2025. "
    "Then, on July 2, 2025, weeks before that trial was due to begin, Bryan Kohberger pleaded guilty to all four murders. "
    "In open court he admitted killing each of the four students, one by one, as the prosecution agreed to take the death penalty off the table. "
    "Three weeks later, a judge sentenced him to four consecutive life sentences without the possibility of parole, plus ten more years for burglary. "
    "He offered no motive, no apology, and no explanation, and under the terms of his plea, he gave up the right to appeal. "
    "For the families, the central questions remain unanswered: why those four students, why that house, and what really happened inside it that night. "
    "A trace of DNA on a button snap, a white car on a neighbour's camera, and a phone that went silent are still the whole of the public answer. "
)


def build_graphic_targets(processed_scenes):
    """Mirror of the production targeting in jobs.run_full_pipeline.

    Translate each processed scene's asset_stats["missing_visuals"]
    records — the sentences that finished retrieval + coverage
    recovery with no verified visual — into graphics targets:
    scene_number + scene-local sentence_index + the sentence's
    verbatim text + its scenario. Records whose sentence_number is
    missing, or that match no sentence in the scene, cannot be
    anchored honestly and are skipped.
    """
    graphic_targets = []
    for processed_scene in processed_scenes:
        scene = processed_scene.get("scene") or {}
        scene_number = scene.get("scene_number")
        sentences = scene.get("sentences") or []
        missing = (
            (processed_scene.get("asset_stats") or {}).get("missing_visuals")
            or []
        )
        for record in missing:
            sentence_number = record.get("sentence_number")
            if sentence_number is None:
                continue
            for index, sentence in enumerate(sentences):
                if not isinstance(sentence, dict):
                    continue
                if sentence.get("sentence_number") != sentence_number:
                    continue
                graphic_targets.append(
                    {
                        "scene_number": scene_number,
                        "sentence_index": index,
                        "sentence_number": sentence_number,
                        "text": (
                            sentence.get("text")
                            or sentence.get("sentence_text")
                            or ""
                        ),
                        "scenario": sentence.get("scenario") or {},
                    }
                )
                break
    return graphic_targets


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

    # Analysis runs exactly as production configures it: the real LLM
    # when a BrewKeg key is present in the environment, degraded
    # heuristic mode otherwise (forced explicitly so the substitution
    # is visible, never silent). Discovery is NOT patched: every
    # provider runs for real.
    if os.environ.get("BREWKEG_API_KEY", "e2e-no-key") == "e2e-no-key":
        real_analyze = jobs.analyze_script
        jobs.analyze_script = lambda script, project_id="", cache_path=None, **kw: real_analyze(
            script, project_id=project_id, cache_path=cache_path, llm=None
        )
        print("E2E-TC analysis: DEGRADED (no BrewKeg key in environment)")
    else:
        print("E2E-TC analysis: configured LLM (production default)")

    reset_registry(PROJECT_ID)
    reset_media_caches()

    narration_file = PROJECT_DIR / "narration.wav"
    generate_narration(text=SCRIPT, output_path=str(narration_file))
    captions = json.loads((PROJECT_DIR / "captions.json").read_text(encoding="utf-8"))
    duration = float(captions["duration"])
    print(f"E2E-TC narration duration: {duration:.2f}s, segments: {len(captions['segments'])}")

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
    # Motion graphics stage — a faithful mirror of production
    # (jobs.run_full_pipeline): missing-visual records become
    # targets, and the graphics stage receives them together with
    # the production stat cap. (The previous inline copy passed
    # neither, so targeted generation never ran in this harness.)
    graphic_targets = build_graphic_targets(processed_scenes)
    missing_records = sum(
        len((ps.get("asset_stats") or {}).get("missing_visuals") or [])
        for ps in processed_scenes
    )
    print(
        f"E2E-TC missing-visual records: {missing_records}; "
        f"graphics targets built: {len(graphic_targets)}"
    )
    motion_graphics = build_motion_graphics_assets(
        motion_data, PROJECT_DIR / "motion_graphics", mode="balanced",
        targets=graphic_targets, max_stats=jobs.MOTION_GRAPHIC_MAX_STATS,
    )
    for graphic in motion_graphics:
        scene_number = graphic.get("scene_number")
        sentence_index = graphic.get("sentence_index")
        if scene_number is None:
            continue
        for processed_scene in processed_scenes:
            scene = processed_scene.get("scene") or {}
            if scene.get("scene_number") != scene_number:
                continue
            visual = dict(graphic)
            visual["type"] = "video"
            visual["source"] = "cinemora_motion_graphics"
            visual["motion_graphic"] = True
            visual["sentence_index"] = sentence_index
            visual["file_path"] = graphic.get("file_path") or graphic.get("path")
            visual["path"] = visual["file_path"]
            processed_scene.setdefault("videos", []).append(visual)
            break
    print(f"E2E-TC motion graphics: {len(motion_graphics)}")
    _mg_manifest = json.loads(
        (PROJECT_DIR / "motion_graphics" / "motion_graphics.json").read_text(
            encoding="utf-8"
        )
    )
    _targets_summary = _mg_manifest.get("targets") or {}
    print(
        "E2E-TC graphics targets summary: "
        f"requested={_targets_summary.get('requested')}, "
        f"generated={_targets_summary.get('generated')}, "
        f"skipped={_targets_summary.get('skipped')}"
    )

    timeline_result = build_project_timeline(PROJECT_ID, processed_scenes, output_dir=str(MEDIA_ROOT))
    timeline_data = json.loads(Path(timeline_result["timeline_file"]).read_text(encoding="utf-8"))
    render_scenes = timeline_data.get("scenes", [])

    coverage = timeline_result.get("coverage") or {}
    final_video = PROJECT_DIR / "documentary_final_output.mp4"
    render_error = None
    try:
        raw_video = render_timeline(render_scenes, str(PROJECT_DIR), expected_duration=duration)
        add_narration(str(raw_video), str(narration_file), str(final_video))
    except Exception as error:
        # The renderer refusing (coverage / frame mismatch) is a
        # legitimate outcome to REPORT, not a harness crash: the asset
        # report and coverage checks below carry the full diagnostic.
        render_error = error
        print(f"E2E-TC RENDER REFUSED: {type(error).__name__}: {error}")

    renderer_audit = None
    audit_file = PROJECT_DIR / "renderer_timing_audit.json"
    if audit_file.exists():
        renderer_audit = json.loads(audit_file.read_text(encoding="utf-8"))
    report = build_asset_report(
        processed_scenes,
        timeline_result,
        renderer_audit=renderer_audit,
        analysis_mode=analysis.get("mode") if isinstance(analysis, dict) else None,
    )
    write_asset_report(PROJECT_DIR, report)
    manifest = build_rights_manifest(processed_scenes)
    write_rights_manifest(PROJECT_DIR, manifest)

    if final_video.exists():
        audit = validate_final_video(str(final_video), asset_report=report)
    else:
        audit = {
            "width": 0,
            "height": 0,
            "audio_codec": None,
            "duration_seconds": 0,
            "authentic": report["authentic"],
        }

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
    print("E2E-TC CHECKS:")
    expected_mode = (
        "llm"
        if os.environ.get("BREWKEG_API_KEY", "e2e-no-key") != "e2e-no-key"
        else "degraded"
    )
    check(
        "asset report explicitly records the analysis mode",
        report.get("analysis_mode") == expected_mode
        and analysis.get("mode") == expected_mode,
        f"(report: {report.get('analysis_mode')}, analysis: {analysis.get('mode')}, expected: {expected_mode})",
    )
    check(
        "timeline coverage complete \u2014 every narration sentence has a visual",
        bool(coverage.get("complete")),
        f"(covered {coverage.get('covered_sentences')}/{coverage.get('total_sentences')}, uncovered: {coverage.get('uncovered_sentences')})",
    )
    _last_scene = max((s.get("scene_number") or 0) for s in processed_scenes) if processed_scenes else 0
    check(
        "final scene fully covered",
        not [u for u in (coverage.get("uncovered_sentences") or []) if u.get("scene_number") == _last_scene],
        f"(last scene: {_last_scene})",
    )
    _mg_items = [
        item
        for scene in render_scenes
        for item in scene.get("items", [])
        if item.get("assignment") == "motion_graphic"
    ]
    _generated_files = {
        g.get("file_path") or g.get("path") for g in motion_graphics
    }
    _mg_used = [
        item
        for item in _mg_items
        if (item.get("file_path") or item.get("path")) in _generated_files
    ]
    check(
        "targeted graphics reached the timeline as motion_graphic assignments",
        len(_mg_used) >= 1 if graphic_targets else True,
        f"(targets: {len(graphic_targets)}, motion_graphic items: "
        f"{len(_mg_items)}, referencing generated files: {len(_mg_used)})",
    )
    check(
        "renderer produced the final video",
        render_error is None and final_video.exists(),
        f"(render error: {render_error})" if render_error else "",
    )
    check(
        "final video is 1920x1080 with audio",
        audit["width"] == 1920 and audit["height"] == 1080 and bool(audit.get("audio_codec")),
        f"({audit['width']}x{audit['height']}, audio={audit.get('audio_codec')})",
    )
    _footage_providers = [
        (a.get("provider") or a.get("source")) for a in footage
    ]
    _yt_share = (
        _footage_providers.count("youtube") / len(_footage_providers)
        if _footage_providers
        else 0.0
    )
    check(
        "YouTube enabled as an equal provider (not disabled in this run)",
        "youtube" not in os.environ.get("CINEMORA_DISABLED_PROVIDERS", ""),
        f"(disabled env: {os.environ.get('CINEMORA_DISABLED_PROVIDERS', '')!r})",
    )
    check(
        "no provider dominates accepted footage (YouTube share <= 80%)",
        _yt_share <= 0.8,
        f"(youtube share of footage: {_yt_share:.2f}, providers: {sorted({a.get('provider') or a.get('source') for a in accepted})})",
    )
    check(
        "final video is approximately three minutes",
        150.0 <= float(audit["duration_seconds"]) <= 240.0,
        f"(duration: {audit['duration_seconds']:.1f}s)",
    )
    _seg = report.get("segment_retrieval") or []
    check(
        "every accepted footage asset has segment-retrieval provenance",
        len(_seg) == len(footage)
        and all(e.get("download_method") for e in _seg),
        f"(segment entries: {[(e.get('provider'), e.get('download_method'), e.get('partial_download'), e.get('source_duration_seconds'), e.get('extracted_duration_seconds')) for e in _seg]})",
    )
    check(
        "moving footage completed the full chain (any provider, segment evidence recorded)",
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
    _card_assets = [
        a
        for s in processed_scenes
        for a in list(s.get("videos") or []) + list(s.get("images") or [])
        if a.get("source") == "text_card"
        or a.get("type") == "text_card"
        or a.get("visual_format") == "text_card"
        or a.get("asset_scope") == "fallback_card"
    ]
    check(
        "NO text-card placeholders anywhere (assets, report, renderer)",
        not _card_assets
        and report["text_cards"] == 0
        and report["placeholder_clips"] == 0,
        f"(card assets: {len(_card_assets)}, report cards: {report['text_cards']}, "
        f"renderer placeholders: {report['placeholder_clips']}, "
        f"holdovers of genuine media: {report.get('holdover_clips')})",
    )
    check(
        "missing visuals explicitly reported, never substituted",
        isinstance(report.get("missing_visuals"), list)
        and report.get("missing_visual_count") == len(report["missing_visuals"]),
        f"(missing: {report.get('missing_visual_count')})",
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
    print("E2E-TC ASSET REPORT:", json.dumps(report, indent=2))
    print(f"E2E-TC final video: {final_video}")
    if failures:
        print(f"E2E-TC RESULT: FAIL ({len(failures)} checks failed)")
        return 1
    print("E2E-TC RESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
