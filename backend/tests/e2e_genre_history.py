"""CINEMORA Stage 5E — genre E2E harness: the Yangtze Incident
(HMS Amethyst, 1949) produced through jobs.run_full_pipeline with
the HISTORY genre pack selected via run options.

This is the ready-to-run acceptance production for the Stage 5
"proven genre" gate on the history side. It is NOT run as part of
the test suite: it needs the founder-supplied BrewKeg key + a SearXNG instance
in the environment (they are run-only and never stored). Run:

    BREWKEG_API_KEY=... SEARXNG_URL=... \
        python tests/e2e_genre_history.py

``--checks-only`` re-runs the artefact checks against an existing
production directory without producing anything.

Beyond the standard production contract (coverage, no text cards,
rights manifest, real 1080p output), the checks verify the genre
pack actually governed the run: the asset report, the run manifest
options, the motion-graphics manifest and the case bible's
research_meta all record the history pack.
"""

import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

os.environ.setdefault("RIGHTS_POLICY", "allow_all")
# Import-time only (llm_client refuses to import without a key);
# a live run supplies the real founder keys via the environment,
# which setdefault never overrides.
os.environ.setdefault("BREWKEG_API_KEY", "dummy")
os.environ.setdefault("CINEMORA_REQUIRE_MEDIA_PROBE", "1")
os.environ.setdefault("CINEMORA_MEDIA_ROOT",
                      str(Path.home() / "workspace" / "cinemora-audit" / "e2e-media"))

PROJECT_ID = "e2e-genre-history-yangtze"

# ~3 minutes at narration speed (23 sentences). Every literal ends
# with a trailing space — the true-crime harness's OOM lesson:
# without them the script fuses into ONE giant "sentence".
SCRIPT = (
    "In April 1949, China was in the final months of a civil war, and the Yangtze River had become the front line. "
    "British warships had patrolled the river for a century under old treaty rights, protecting trade and foreign nationals. "
    "On the morning of 20 April 1949, the frigate HMS Amethyst left Shanghai and sailed upriver towards Nanking. "
    "Her mission was peaceful: to relieve the destroyer on guard duty and to protect the British embassy there. "
    "Near Rose Island, shore batteries of the People's Liberation Army opened fire on the ship without warning. "
    "The shelling killed and wounded men across the ship, and her captain, Lieutenant Commander Skinner, was mortally wounded. "
    "Amethyst ran aground on the mud bank of Rose Island, trapped in the middle of the river. "
    "That same week, Communist forces were crossing the Yangtze in strength, and the old order on the river was collapsing. "
    "Lieutenant Commander John Kerens reached the ship overland and took command of the survivors. "
    "The destroyer HMS Consort raced down from Nanking to help, but heavy fire forced her back with casualties. "
    "Two days later, HMS London and HMS Black Swan tried to fight their way through, and they too were driven off. "
    "The rescue attempts had failed, and Amethyst settled into a siege that would last 101 days. "
    "Three British warships now lay damaged, and the cost of forcing a rescue had been counted in sailors' lives. "
    "The Admiralty in London faced an impossible choice between a humiliating negotiation and another bloody attempt. "
    "Negotiations dragged on through the summer while the crew endured heat, short rations, and constant watchfulness. "
    "The communists demanded an apology and an admission that the British ships were the aggressors. "
    "London refused to apologise for a lawful passage, and the talks circled the same ground for weeks. "
    "By July, only 50 percent of the rations remained, and fuel and drinking water were strictly rationed. "
    "The ship's cat, Simon, survived his wounds, kept hunting rats in the stores, and became a hero to the crew. "
    "Kerens knew the river would soon fall further, and a ship aground at low water would never escape. "
    "On the night of 30 July 1949, under cover of darkness, Amethyst slipped her cable and ran downriver. "
    "She hid in the shadow of a passenger steamer and passed the Woosung forts at speed in the dark. "
    "Searchlights found her and guns opened fire, but the little frigate outran the batteries. "
    "Behind her, the steamer she had hidden behind was caught in the fire, a reminder of how narrow the margin had been. "
    "Every man aboard knew that one shell in the engine room would have ended the escape in the mud. "
    "In the early hours of 31 July, Kerens sent the signal that became famous around the world. "
    "The signal read: Have rejoined the fleet south of Woosung. God save the King. "
    "Amethyst reached Hong Kong to a hero's welcome, and Simon the cat was later awarded the Dickin Medal. "
    "Seven years later, the escape became a feature film, and the Yangtze Incident passed into naval legend. "
)

failures = []
executed = [0]


def check(label, ok, detail=""):
    executed[0] += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


def project_dir() -> Path:
    from services.media_paths import project_dir as _project_dir
    return _project_dir(PROJECT_ID)


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def run_checks() -> int:
    pdir = project_dir()
    print(f"\n== artefact checks: {pdir} ==")
    result = load_json(pdir / "pipeline_result.json") or {}
    status = load_json(pdir / "production_status.json") or {}
    report = load_json(pdir / "asset_report.json") or {}
    timeline = load_json(pdir / "timeline.json") or []
    manifest = load_json(pdir / "run_manifest.json") or {}
    graphics = load_json(pdir / "motion_graphics" / "motion_graphics.json") or {}
    bible = load_json(pdir / "case_bible.json") or {}

    cov = result.get("coverage") or {}
    total = cov.get("total_sentences") or 0
    check("coverage recorded", total > 0, f"total={total}")
    check("coverage complete", cov.get("uncovered") == 0 and total > 0,
          f"{cov.get('covered')}/{total}")
    check("no text-card visuals", (report.get("text_cards") or 0) == 0)
    check("analysis mode recorded (not fabricated)",
          result.get("analysis_mode") in ("llm", "partial_llm", "degraded"),
          f"mode={result.get('analysis_mode')}")

    # Genre pack governance — the Stage 5 point of this harness.
    pack = report.get("genre_pack") or {}
    check("asset report records the history pack",
          pack.get("id") == "history", f"genre_pack={pack}")
    check("run manifest options carry the history pack",
          (manifest.get("options") or {}).get("genre_pack") == "history",
          f"options.genre_pack={(manifest.get('options') or {}).get('genre_pack')}")
    check("graphics manifest records the history pack",
          graphics.get("genre_pack") == "history",
          f"graphics genre_pack={graphics.get('genre_pack')}")
    research_meta = bible.get("research_meta") or {}
    check("case bible research used the history pack",
          research_meta.get("genre_pack") == "history",
          f"research genre_pack={research_meta.get('genre_pack')}")

    rights = report.get("rights") or {}
    check("rights manifest covers every accepted asset",
          rights.get("accepted_total") is not None
          and rights.get("with_manifest_entry") == rights.get("accepted_total"),
          f"{rights.get('with_manifest_entry')}/{rights.get('accepted_total')}")
    blob = json.dumps(report).lower()
    check("no fabricated 'cleared' rights", "cleared" not in blob)
    check("no fabricated 'royalty-free' licence", "royalty-free" not in blob
          and "royalty_free" not in blob)

    final_video = result.get("final_video") or (pdir / "final_video.mp4")
    fv = Path(final_video)
    check("final video exists", fv.exists(), str(fv))
    if fv.exists():
        from services.media_probe import probe_video
        info = probe_video(fv)
        check("final video is real 1080p video",
              bool(info) and info["width"] == 1920 and info["height"] == 1080)
        check("final video has audio", bool(info and info["has_audio"]))
        duration = (info or {}).get("duration_seconds") or 0
        check("final video duration ~3 minutes", 150 <= duration <= 240,
              f"{duration:.1f}s")
    check("production status completed", status.get("status") == "completed",
          f"status={status.get('status')}")

    passed = executed[0] - len(failures)
    print(f"\nE2E-GENRE-HISTORY RESULT: "
          f"{'PASS' if not failures else 'FAIL'} "
          f"({passed}/{executed[0]} checks passed)")
    return 0 if not failures else 1


def main() -> int:
    if "--checks-only" in sys.argv:
        return run_checks()
    from services import jobs
    print(f"Running genre-history production for project {PROJECT_ID} ...")
    result = jobs.run_full_pipeline(PROJECT_ID, SCRIPT, options={"genre": "history"})
    print(f"pipeline result: status={result.get('status')}")
    return run_checks()


if __name__ == "__main__":
    raise SystemExit(main())
