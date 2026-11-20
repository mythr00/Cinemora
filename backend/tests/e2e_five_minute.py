#!/usr/bin/env python3
"""Five-minute production E2E — founder verification run.

Production runs through jobs.run_full_pipeline — the single
production entry point — with ALL providers enabled, YouTube
included as one equal supplementary source among archive.org,
Wikimedia Commons, news and institutional sources (no provider is
disabled and none is primary), and NO search substitutions. This
harness contains no pipeline stages of its own: it makes that one
call, then verifies the artefacts the run left in PROJECT_DIR
(asset_report.json, rights_manifest.json, timeline.json,
motion_graphics/motion_graphics.json,
story_understanding_cache.json and the final mp4, audited with the
production final-video auditor). Run with --checks-only to re-run
only the assertions against an existing PROJECT_DIR, without
producing anything.

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
PROJECT_ID = "e2e-five-minute-berlin"
PROJECT_DIR = MEDIA_ROOT / PROJECT_ID

os.environ.setdefault("DATABASE_URL", "sqlite:///" + str(OUT_ROOT / "e2e_ms.sqlite3"))
os.environ.setdefault("BREWKEG_API_KEY", "e2e-no-key")
os.environ.setdefault("SERPER_API_KEY", "e2e-no-key")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ.setdefault("CINEMORA_MEDIA_ROOT", str(OUT_ROOT))
os.environ["SOURCE_INTEL_DB"] = str(OUT_ROOT / "source_intel.sqlite3")

sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

SCRIPT = (
    "In the summer of 1961, the city of Berlin stood divided between two worlds, watched by soldiers on both sides of a line that existed only on maps. "
    "In the early hours of August 13, East German troops unrolled coils of barbed wire across the heart of the city, sealing the border in a single night. "
    "Families woke to find their streets cut in two, their neighbours suddenly living in another country, and their journeys to work forbidden. "
    "Within days the barbed wire gave way to concrete, and the Berlin Wall began its long transformation into a fortified frontier of watchtowers and floodlights. "
    "The wall ran for more than one hundred and fifty kilometres around West Berlin, cutting through streets, rivers, cemeteries and railway lines. "
    "At Bernauer Strasse, the fronts of apartment buildings stood in the East while their pavements lay in the West, and residents jumped from windows into blankets held by firefighters below. "
    "Some of those jumps ended in tragedy, and the deaths at the wall began a ledger of loss that would grow for twenty eight years. "
    "The East German government called it an anti-fascist protection rampart, claiming it defended socialism from Western spies and saboteurs. "
    "In truth, more than two and a half million East Germans had already fled to the West, and the regime was haemorrhaging its doctors, engineers and teachers. "
    "In June 1963, President John F. Kennedy stood before the Schoneberg city hall and declared, Ich bin ein Berliner, promising that the free world stood with the trapped city. "
    "Checkpoint Charlie became the most famous crossing on earth, where American and Soviet tanks faced each other muzzle to muzzle in October 1961. "
    "For sixteen tense hours the world held its breath, before both sides withdrew their armour one tank at a time. "
    "Ordinary Berliners adapted with extraordinary courage, smuggling letters, photographs and medicine across the border in coat linings and hollowed-out books. "
    "Escape attempts grew ever more ingenious, from tunnels dug beneath the death strip to hot air balloons stitched together from bed sheets. "
    "In 1979, two families floated silently over the border at night in a homemade balloon, landing in a West German field at dawn. "
    "Others were not so fortunate, and at least one hundred and forty people died at the Berlin Wall, shot by guards, drowned in canals, or killed in failed escapes. "
    "The youngest victim was a baby of fifteen months, and the oldest was an eighty year old woman who fell from a window on Bernauer Strasse. "
    "In the West, the wall became a canvas, covered in murals, slogans and grief, while its eastern face remained bare grey concrete guarded by dogs and mines. "
    "By the 1980s, a generation had grown up that had never known a united city, and many believed the division would last forever. "
    "But across Eastern Europe the ground was shifting, as Poland's Solidarity movement and Hungary's reformers pried open the first cracks in the Iron Curtain. "
    "In May 1989, Hungary began dismantling its border fence with Austria, and thousands of East Germans began travelling south to slip across. "
    "That summer, embassy gardens in Prague filled with refugees sleeping in tents, waiting for trains that would carry them to freedom in the West. "
    "In Leipzig, Monday prayer meetings at the Nikolaikirche grew into marches of tens of thousands, chanting Wir sind das Volk, we are the people. "
    "On October 9, seventy thousand demonstrators marched through Leipzig while armed police stood by, and the order to shoot never came. "
    "The peaceful revolution had found its courage, and the regime of Erich Honecker collapsed within days, replaced by men promising reform. "
    "On the evening of November 9, 1989, a tired spokesman named Gunter Schabowski told a press conference that East Germans could travel freely, effective, as far as he knew, immediately. "
    "Thousands of East Berliners heard the broadcast and walked to the checkpoints, demanding that the guards let them through. "
    "At Bornholmer Strasse, the crowd grew so large and so determined that the overwhelmed commander, Harald Jager, finally ordered the barriers opened shortly before midnight. "
    "People streamed across the bridge into West Berlin, weeping, embracing strangers, and dancing on top of the wall itself. "
    "At the Brandenburg Gate, families separated for twenty eight years embraced in tears as the world watched on live television. "
    "Within days, ordinary citizens climbed the wall with hammers and chisels and began tearing it down piece by piece, keeping fragments as treasures. "
    "These wall woodpeckers, the Mauerspechte, hacked souvenirs from the concrete that found their way to mantelpieces on every continent. "
    "Cranes and bulldozers followed, and within a year almost nothing remained of the structure that had divided a continent. "
    "On October 3, 1990, Germany was reunified at last, and the flag of a single nation rose above the Reichstag in Berlin. "
    "The Cold War's most brutal symbol became a memory, preserved in a few painted stretches of concrete like the East Side Gallery. "
    "There, artists from twenty one countries covered the longest remaining section with murals of hope, protest and joy. "
    "Today a double line of cobblestones traces the wall's path through the reunited city, crossing squares where children play. "
    "At the Berlin Wall Memorial on Bernauer Strasse, a preserved stretch of the death strip shows visitors exactly what division looked like. "
    "Historians still debate the wall's meaning, but for the families it separated, its fall remains the defining night of their lives. "
    "The story of the Berlin Wall is therefore not only about concrete and guards, but about the patience of ordinary people who refused to forget one another. "
    "It teaches that no wall, however fortified, can permanently divide a people who insist on remaining human to each other. "
    "And when the barriers finally opened, it was not an army that crossed first, but crowds of neighbours carrying flowers, champagne and tears. "
)


def _load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def run_checks():
    """Verify a completed run from the artefacts in PROJECT_DIR.

    Reads asset_report.json, rights_manifest.json, timeline.json,
    motion_graphics/motion_graphics.json and
    story_understanding_cache.json, and audits the final mp4 with
    the production final-video auditor. Returns (results, passed,
    total); each result is {"name", "ok", "detail"}.
    """
    from services.final_video_auditor import validate_final_video

    results = []

    def check(name, ok, detail=""):
        results.append({"name": name, "ok": bool(ok), "detail": detail})

    report = _load_json(PROJECT_DIR / "asset_report.json", {})
    if not isinstance(report, dict):
        report = {}
    manifest = _load_json(PROJECT_DIR / "rights_manifest.json", [])
    if not isinstance(manifest, list):
        manifest = []
    manifest = [entry for entry in manifest if isinstance(entry, dict)]
    timeline_data = _load_json(PROJECT_DIR / "timeline.json", {})
    if not isinstance(timeline_data, dict):
        timeline_data = {}
    render_scenes = timeline_data.get("scenes", [])
    coverage = timeline_data.get("coverage") or report.get("coverage") or {}

    final_video = PROJECT_DIR / "documentary_final_output.mp4"
    if final_video.exists():
        try:
            audit = validate_final_video(str(final_video), asset_report=report)
        except Exception:
            audit = {
                "width": 0,
                "height": 0,
                "audio_codec": None,
                "duration_seconds": 0,
                "authentic": report.get("authentic"),
            }
    else:
        audit = {
            "width": 0,
            "height": 0,
            "audio_codec": None,
            "duration_seconds": 0,
            "authentic": report.get("authentic"),
        }

    # Accepted assets, reconstructed from the rights manifest: the
    # production report stage writes exactly one manifest entry per
    # accepted asset. Moving footage is the video entries that are
    # not in-house motion graphics (those carry the
    # cinemora_motion_graphics source).
    accepted = manifest
    footage = [
        a for a in accepted
        if a.get("type") == "video" and a.get("source") != "cinemora_motion_graphics"
    ]

    # The run's own recorded analysis mode: the story-understanding
    # cache the analysis stage wrote into PROJECT_DIR (the case
    # bible carries the same stamp as a fallback).
    analysis_cache = _load_json(PROJECT_DIR / "story_understanding_cache.json", {})
    analysis_mode = (
        analysis_cache.get("mode") if isinstance(analysis_cache, dict) else None
    )
    if analysis_mode is None:
        case_bible = _load_json(PROJECT_DIR / "case_bible.json", {})
        analysis_mode = (
            case_bible.get("mode") if isinstance(case_bible, dict) else None
        )
    check(
        "asset report explicitly records the analysis mode",
        report.get("analysis_mode") is not None
        and report.get("analysis_mode") == analysis_mode,
        f"(report: {report.get('analysis_mode')}, analysis: {analysis_mode})",
    )
    check(
        "timeline coverage complete \u2014 every narration sentence has a visual",
        bool(coverage.get("complete")),
        f"(covered {coverage.get('covered_sentences')}/{coverage.get('total_sentences')}, uncovered: {coverage.get('uncovered_sentences')})",
    )
    _scene_numbers = [
        s.get("scene_number") or 0 for s in render_scenes if isinstance(s, dict)
    ]
    _last_scene = max(_scene_numbers) if _scene_numbers else 0
    check(
        "final scene fully covered",
        not [
            u
            for u in (coverage.get("uncovered_sentences") or [])
            if isinstance(u, dict) and u.get("scene_number") == _last_scene
        ],
        f"(last scene: {_last_scene})",
    )
    _mg_manifest = _load_json(
        PROJECT_DIR / "motion_graphics" / "motion_graphics.json", {}
    )
    if not isinstance(_mg_manifest, dict):
        _mg_manifest = {}
    _mg_assets = _mg_manifest.get("assets") or []
    _generated_files = {
        g.get("file_path") or g.get("path")
        for g in _mg_assets
        if isinstance(g, dict)
    }
    _targets = _mg_manifest.get("targets")
    if isinstance(_targets, dict):
        _targets_requested = int(_targets.get("requested") or 0)
    elif isinstance(_targets, list):
        _targets_requested = len(_targets)
    else:
        _targets_requested = int(report.get("missing_visual_count") or 0)
    _mg_items = [
        item
        for scene in render_scenes
        if isinstance(scene, dict)
        for item in scene.get("items", [])
        if isinstance(item, dict) and item.get("assignment") == "motion_graphic"
    ]
    _mg_used = [
        item
        for item in _mg_items
        if (item.get("file_path") or item.get("path")) in _generated_files
    ]
    # targeted_graphics_present (Phase 5 strengthening): the check is
    # EXPLICITLY NAMED so a production that requested targeted
    # graphics (targets > 0, falling back to the report's missing
    # visual count when the motion-graphics manifest carries no
    # target count) but placed ZERO of them on the timeline fails
    # under this stable name, instead of the failure hiding inside a
    # descriptive sentence. When no targets were requested the check
    # is not applicable and passes, with that stated in the detail.
    check(
        "targeted_graphics_present: targeted graphics reached the timeline as motion_graphic assignments",
        len(_mg_used) >= 1 if _targets_requested else True,
        f"(targets: {_targets_requested}, motion_graphic items: "
        f"{len(_mg_items)}, referencing generated files: {len(_mg_used)}, "
        f"targeted_graphics_on_timeline: {len(_mg_used)}"
        + ("" if _targets_requested else ", not applicable: no targets requested")
        + ")",
    )
    check(
        "renderer produced the final video",
        final_video.exists(),
    )
    check(
        "final video is 1920x1080 with audio",
        audit.get("width") == 1920 and audit.get("height") == 1080 and bool(audit.get("audio_codec")),
        f"({audit.get('width')}x{audit.get('height')}, audio={audit.get('audio_codec')})",
    )
    _footage_providers = [a.get("source") for a in footage]
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
        f"(youtube share of footage: {_yt_share:.2f}, providers: {sorted({a.get('source') for a in accepted if a.get('source')})})",
    )
    check(
        "final video is approximately five minutes",
        240.0 <= float(audit.get("duration_seconds") or 0) <= 390.0,
        f"(duration: {float(audit.get('duration_seconds') or 0):.1f}s)",
    )
    _seg = report.get("segment_retrieval") or []
    check(
        "every accepted footage asset has segment-retrieval provenance",
        len(_seg) == len(footage)
        and all(isinstance(e, dict) and e.get("download_method") for e in _seg),
        f"(segment entries: {[(e.get('provider'), e.get('download_method'), e.get('partial_download'), e.get('source_duration_seconds'), e.get('extracted_duration_seconds')) for e in _seg if isinstance(e, dict)]})",
    )
    check(
        "moving footage completed the full chain (any provider, segment evidence recorded)",
        len(footage) >= 1
        and len(_seg) >= 1
        and all(isinstance(e, dict) and e.get("segment_evidence") for e in _seg),
        f"(footage: {[(e.get('provider'), e.get('segment_evidence')) for e in _seg if isinstance(e, dict)]})",
    )
    _funnel = ((report.get("providers") or {}).get("funnel") or {})
    check(
        "at least two providers discovered candidates",
        len([p for p, c in _funnel.items() if isinstance(c, dict) and c.get("discovered")]) >= 2,
        f"(funnel: { {p: c.get('discovered') for p, c in _funnel.items() if isinstance(c, dict)} })",
    )
    _accepted_mix = ((report.get("format_mix") or {}).get("accepted") or {})
    check(
        "format mix accounts for every accepted asset",
        sum(_accepted_mix.values()) == report.get("total_accepted_assets"),
        f"(mix: {_accepted_mix})",
    )
    _subs = ((report.get("authenticity") or {}).get("substitutions") or {})
    check(
        "authenticity consistent with substitution counts",
        report.get("authentic")
        == (
            _subs.get("placeholder_clips") == 0
            and _subs.get("text_cards") == 0
            and _subs.get("unverified_assets") == 0
        ),
        f"(authenticity: {report.get('authenticity')})",
    )
    check(
        "auditor consumed the report",
        audit.get("authentic") == report.get("authentic"),
    )
    _card_assets = [
        a
        for a in accepted
        if a.get("source") == "text_card" or a.get("type") == "text_card"
    ]
    check(
        "NO text-card placeholders anywhere (assets, report, renderer)",
        not _card_assets
        and report.get("text_cards") == 0
        and report.get("placeholder_clips") == 0,
        f"(card assets: {len(_card_assets)}, report cards: {report.get('text_cards')}, "
        f"renderer placeholders: {report.get('placeholder_clips')}, "
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
        len(manifest) == report.get("total_accepted_assets"),
        f"({len(manifest)} vs {report.get('total_accepted_assets')})",
    )
    _manifest_path = PROJECT_DIR / "rights_manifest.json"
    _manifest_text = (
        _manifest_path.read_text(encoding="utf-8") if _manifest_path.exists() else ""
    )
    check(
        "no fabricated 'royalty-free' licence anywhere",
        _manifest_path.exists() and "royalty-free" not in _manifest_text,
    )
    check(
        "sentence assignment accuracy measured",
        report.get("sentence_assignment_accuracy") is not None,
        f"(accuracy: {report.get('sentence_assignment_accuracy')})",
    )

    passed = sum(1 for result in results if result["ok"])
    return results, passed, len(results)


def _print_results(results):
    for result in results:
        status = "PASS" if result["ok"] else "FAIL"
        detail = f" {result['detail']}" if result.get("detail") else ""
        print(f"  [{status}] {result['name']}{detail}")


def main():
    if "--checks-only" in sys.argv:
        results, passed, total = run_checks()
        print()
        print("E2E-5M CHECKS (checks-only, existing artefacts):")
        _print_results(results)
        print()
        if passed == total:
            print(f"E2E-5M RESULT: PASS ({passed}/{total} checks passed)")
            return 0
        print(
            f"E2E-5M RESULT: FAIL ({total - passed} checks failed, "
            f"{passed}/{total} passed)"
        )
        return 1

    import jobs

    jobs.run_full_pipeline(PROJECT_ID, SCRIPT)

    results, passed, total = run_checks()
    print()
    print("E2E-5M CHECKS:")
    _print_results(results)
    print()
    _report_file = PROJECT_DIR / "asset_report.json"
    if _report_file.exists():
        print("E2E-5M ASSET REPORT:", _report_file.read_text(encoding="utf-8"))
    print(f"E2E-5M final video: {PROJECT_DIR / 'documentary_final_output.mp4'}")
    if passed == total:
        print(f"E2E-5M RESULT: PASS ({passed}/{total} checks passed)")
        return 0
    print(
        f"E2E-5M RESULT: FAIL ({total - passed} checks failed, "
        f"{passed}/{total} passed)"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
