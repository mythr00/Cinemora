"""Stage 6 test fixture: a small SYNTHETIC production.

Builds a real (tiny) completed production in a temp media root —
ffmpeg lavfi clips, a PIL still, a motion-engine graphic with its
inputs recorded, real Piper narration (Stage 2D parts), captions
through the real single writer (proportional path), a soundtrack
through the real music stage, a rights manifest, a story cache, a
ledger run with decision rows, and a documentary_final_output.mp4 —
so editor imports, operations and renders are exercised against
genuine artefacts. Nothing here touches a real project directory.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from uuid import uuid4

SENTENCES = [
    "The river crossing began at dawn.",
    "By noon the convoy reached the far bank.",
    "The gauge showed forty two percent remaining.",
]


def _run(command):
    subprocess.run(command, check=True, capture_output=True)


def _sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_clip(path, pattern, seconds=6):
    _run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"{pattern}=size=640x360:rate=30",
            "-t", str(seconds), "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(path),
        ]
    )
    return path


def make_audio(path, frequency=440, seconds=8):
    _run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"sine=frequency={frequency}:sample_rate=44100",
            "-t", str(seconds), str(path),
        ]
    )
    return path


def hash_tree(project_dir) -> dict:
    """sha256 of every file under the project dir, keyed by
    project-relative path."""
    project_dir = Path(project_dir)
    result = {}
    for path in sorted(project_dir.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(project_dir))] = _sha256(path)
    return result


def build_synthetic_production(media_root, project_id="proj-editor",
                               graphic_inputs=True):
    from database import SessionLocal, engine
    from models import AssetDecision, Base, Project

    Base.metadata.create_all(engine)  # same startup step main.py runs
    from services import captions as captions_service
    from services import music as music_service
    from services import narration as narration_service
    from services import run_ledger
    from services.motion_engine import templates as engine

    project_dir = Path(media_root) / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    clip_a = make_clip(project_dir / "clip_a.mp4", "testsrc2")
    clip_b = make_clip(project_dir / "clip_b.mp4", "testsrc")
    clip_c = make_clip(project_dir / "clip_c.mp4", "smptebars")
    replacement = make_clip(project_dir / "replacement.mp4", "testsrc2", 5)

    from PIL import Image

    still = project_dir / "still.png"
    Image.new("RGB", (640, 360), (40, 90, 160)).save(still)

    graphic = project_dir / "graphic.mp4"
    inputs = {"percent": 42, "label": "SUPPLIES", "sublabel": "REMAINING"}
    rendered, _meta = engine.data_gauge(
        inputs, "history", graphic, duration=2.0,
        sentence_index=2, scene_number=1,
    )
    assert rendered, "fixture graphic failed to render"

    music_file = make_audio(project_dir / "music.wav", 440)
    music_alt = make_audio(project_dir / "music_alt.wav", 660)

    script = " ".join(SENTENCES)
    narration_info = narration_service.generate_narration(
        script, project_dir / "narration.wav"
    )
    manifest = json.loads(
        (project_dir / "narration_parts" / "parts_manifest.json").read_text()
    )
    windows = []
    cursor = 0.0
    for index, part in enumerate(manifest["parts"]):
        duration = narration_service._audio_duration(
            project_dir / "narration_parts" / part["file"]
        )
        windows.append(
            {"number": index + 1, "text": SENTENCES[index],
             "start": round(cursor, 3), "end": round(cursor + duration, 3)}
        )
        cursor += duration
    total = round(cursor, 3)

    def win(number):
        return windows[number - 1]

    def item(kind_type, path, number, start, end, **extra):
        base = {
            "type": kind_type,
            "scene_number": 1,
            "sentence_index": number - 1,
            "sentence": SENTENCES[number - 1],
            "start": start,
            "end": end,
            "duration": round(end - start, 3),
            "file_path": str(path),
            "path": str(path),
            "source": "synthetic",
            "provider": "synthetic",
            "source_url": f"https://example.test/{Path(path).name}",
            "title": Path(path).stem,
            "assignment": "sentence",
            "relevance_status": "verified",
            "motion": "static",
            "transition": "cut",
            "clip_start": 0,
            "video_url": str(path) if kind_type == "video" else None,
            "image_url": str(path) if kind_type == "image" else None,
        }
        base.update(extra)
        return base

    w1, w2, w3 = win(1), win(2), win(3)
    split = round(w1["start"] + (w1["end"] - w1["start"]) * 0.6, 3)
    clips = [
        item("video", clip_a, 1, w1["start"], split,
             segment_evidence="verified_segment"),
        item("image", still, 1, split, w1["end"]),
        item("video", clip_b, 2, w2["start"], w2["end"],
             segment_evidence="verified_segment"),
        item("video", graphic, 3, w3["start"], w3["end"],
             assignment="motion_graphic",
             generator="motion_engine", template="data_gauge",
             family="data",
             engine_inputs=inputs if graphic_inputs else None),
    ]
    timeline = {
        "scenes": [{"scene_number": 1, "items": clips}],
        "duration_seconds": total,
    }
    (project_dir / "timeline.json").write_text(json.dumps(timeline, indent=2))

    rights_assets = []
    for path, licence in (
        (clip_a, "CC BY 4.0"), (clip_b, "CC BY 4.0"),
        (still, "CC0 1.0"), (graphic, None),
    ):
        if licence is None:
            continue
        rights_assets.append(
            {
                "file_path": str(path),
                "source": "synthetic",
                "source_url": f"https://example.test/{Path(path).name}",
                "license": licence,
                "license_url": "https://example.test/licence",
                "creator": "Fixture Creator",
                "rights_status": "recorded",
                "retrieved_at": "2026-11-19T00:00:00+00:00",
            }
        )
    (project_dir / "rights_manifest.json").write_text(
        json.dumps(
            {"project_id": project_id, "generated_at": "2026-11-19",
             "assets": rights_assets},
            indent=2,
        )
    )

    plans = [
        {"sentence": {"text": text}} for text in SENTENCES
    ]
    (project_dir / "story_understanding_cache.json").write_text(
        json.dumps({"bible": {"title": "Fixture"}, "plans": plans}, indent=2)
    )

    original_transcribe = captions_service.transcribe_audio

    def _no_whisper(*args, **kwargs):
        raise RuntimeError("whisper unavailable in fixture")

    captions_service.transcribe_audio = _no_whisper
    try:
        captions_service.build_captions(
            project_dir / "narration.wav",
            script,
            project_dir / "captions.json",
        )
    finally:
        captions_service.transcribe_audio = original_transcribe

    soundtrack = music_service.prepare_soundtrack(
        project_dir / "narration.wav",
        project_dir / "soundtrack.wav",
        music_file=str(music_file),
    )
    (project_dir / "soundtrack.json").write_text(json.dumps(soundtrack, indent=2))
    (project_dir / "production_state.json").write_text(
        json.dumps(
            {
                "voice": {
                    "voice_id_used": narration_info.get("voice_id_used"),
                    "voice_fallback": False,
                },
                "captions_mode": "burn",
                "soundtrack": soundtrack,
            },
            indent=2,
        )
    )

    final_film = project_dir / "documentary_final_output.mp4"
    final_film.write_bytes(clip_a.read_bytes())

    db = SessionLocal()
    try:
        db.add(
            Project(
                id=project_id, script=script, category="history",
                status="completed",
            )
        )
        db.commit()
    finally:
        db.close()
    run_id = run_ledger.begin_run(project_id, script, options={})
    run_ledger.finish_run(run_id, "completed")

    verification = {
        "accepted": True, "confidence": 0.9, "specificity": "exact",
        "reason": "fixture_verified",
    }
    rights = {
        "license": "CC BY 4.0",
        "license_url": "https://example.test/licence",
        "rights_status": "recorded",
        "creator": "Fixture Creator",
        "source": "synthetic",
        "retrieved_at": "2026-11-19T00:00:00+00:00",
    }
    db = SessionLocal()
    try:
        for path, decision_type, extra in (
            (clip_a, "selected", {"sentence_number": 1}),
            (still, "selected", {"sentence_number": 1}),
            (clip_b, "selected", {"sentence_number": 2}),
            (graphic, "generated", {"generated": True, "sentence_number": 3}),
        ):
            is_graphic = decision_type == "generated"
            db.add(
                AssetDecision(
                    id=str(uuid4()),
                    run_id=run_id,
                    project_id=project_id,
                    scene_number=1,
                    sentence_number=extra.get("sentence_number"),
                    asset_key=str(path),
                    content_hash=_sha256(path),
                    provider="cinemora_motion_engine" if is_graphic else "synthetic",
                    source_url=None if is_graphic else f"https://example.test/{Path(path).name}",
                    evidence_class=None if is_graphic else "verified_segment",
                    verification=None if is_graphic else verification,
                    rights=None if is_graphic else rights,
                    decision_type=decision_type,
                )
            )
        db.commit()
    finally:
        db.close()

    return {
        "project_id": project_id,
        "project_dir": project_dir,
        "run_id": run_id,
        "script": script,
        "windows": windows,
        "total_duration": total,
        "clip_a": clip_a,
        "clip_b": clip_b,
        "clip_c": clip_c,
        "replacement": replacement,
        "still": still,
        "graphic": graphic,
        "graphic_inputs": inputs,
        "music": music_file,
        "music_alt": music_alt,
    }
