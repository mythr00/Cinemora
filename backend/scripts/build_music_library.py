"""Build the bundled cinematic music library (founder mandate
2026-11-27: CINEMATIC MUSIC LIBRARY).

Reads downloaded track files from a staging directory, probes
them, and writes the platform library under backend/cinemora_music/:

    cinemora_music/
        <category>/<track-slug>.mp3        full tracks
        previews/<track-slug>.mp3          30 s preview clips
        catalogue.json                     services.music_library
                                           catalogue (the live seam:
                                           CINEMORA_MUSIC_CATALOGUE)
        music_library_manifest.json        the mandate Part 2
                                           manifest (founder-facing)

The track spec below is the founder's supplied reference list,
deduplicated: 20 references, 17 distinct tracks (references 3/15/
20 are one Pixabay track, 409406; references 4/16 are one, 341438).
Titles and artists are recorded ONLY where the founder's mandate
itself names them — an unnamed artist stays null, never invented
(the music_library honesty rule). Licensing is the founder's,
handled separately; records carry no licence claim.

Usage:
    python scripts/build_music_library.py [--staging DIR]
                                          [--out DIR]

A spec track whose staging file is absent is reported as MISSING
and left out of the catalogue — the build never invents or
substitutes a file. Re-running is idempotent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STAGING = Path(
    "~/workspace/cinemora-audit/cinematic-music-library/staging"
).expanduser()
DEFAULT_OUT = BACKEND_ROOT / "cinemora_music"

CATEGORIES = (
    "epic_battles",
    "american_frontier",
    "destruction_tension",
    "military_drums",
    "cinematic_orchestral",
)

# staging: the file name expected in the staging directory.
# artist: only where the founder's mandate names one, else None.
TRACK_SPEC = [
    {
        "track_id": "epic-battle-speech-wayne-jones",
        "title": "Epic Battle Speech",
        "artist": "Wayne Jones",
        "source_url": "https://www.youtube.com/watch?v=emEmVbrLlQs",
        "category": "epic_battles",
        "staging": "youtube-emEmVbrLlQs.mp3",
        "moods": ["epic", "battle", "speech"],
    },
    {
        "track_id": "the-war",
        "title": "The War",
        "artist": None,
        "source_url": "https://pixabay.com/music/build-up-scenes-the-war-184745/",
        "category": "epic_battles",
        "staging": "pixabay-184745.mp3",
        "moods": ["epic", "war", "build-up"],
    },
    {
        "track_id": "epic-war-background-music",
        "title": "Epic War Background Music",
        "artist": "ViacheslavStarostin",
        "source_url": "https://pixabay.com/music/main-title-epic-war-background-music-409406/",
        "category": "epic_battles",
        "staging": "pixabay-409406.mp3",
        "moods": ["epic", "war", "main-title"],
    },
    {
        "track_id": "epic-military-army-war",
        "title": "Epic Military Army War Background Music",
        "artist": None,
        "source_url": "https://pixabay.com/music/main-title-epic-military-army-war-background-music-341438/",
        "category": "epic_battles",
        "staging": "pixabay-341438.mp3",
        "moods": ["epic", "military", "war"],
    },
    {
        "track_id": "the-battle-rages-on",
        "title": "The Battle Rages On",
        "artist": None,
        "source_url": "https://pixabay.com/music/main-title-the-battle-rages-on-cinematic-action-theme-135285/",
        "category": "epic_battles",
        "staging": "pixabay-135285.mp3",
        "moods": ["epic", "battle", "action"],
    },
    {
        "track_id": "greenskin-warrior",
        "title": "Greenskin Warrior",
        "artist": None,
        "source_url": "https://pixabay.com/music/build-up-scenes-greenskin-warrior-war-background-music-111203/",
        "category": "epic_battles",
        "staging": "pixabay-111203.mp3",
        "moods": ["epic", "war", "build-up"],
    },
    {
        "track_id": "countdown-to-destruction",
        "title": "Countdown to Destruction",
        "artist": None,
        "source_url": "https://pixabay.com/music/main-title-countdown-to-destruction-183346/",
        "category": "destruction_tension",
        "staging": "pixabay-183346.mp3",
        "moods": ["tension", "destruction", "dark"],
    },
    {
        "track_id": "epic-battle-133925",
        "title": "Epic Battle",
        "artist": None,
        "source_url": "https://pixabay.com/music/action-epic-battle-133925/",
        "category": "epic_battles",
        "staging": "pixabay-133925.mp3",
        "moods": ["epic", "battle", "action"],
    },
    {
        "track_id": "epic-battle-cinematic-soul",
        "title": "Epic Battle",
        "artist": "Cinematic-Soul",
        "source_url": "https://pixabay.com/music/action-epic-epic-battle-627481/",
        "category": "epic_battles",
        "staging": "pixabay-627481.mp3",
        "moods": ["epic", "battle", "cinematic"],
    },
    {
        "track_id": "epic-fight-alec-koff",
        "title": "Epic Fight",
        "artist": "Alec_Koff",
        "source_url": "https://pixabay.com/music/adventure-epic-fight-487416/",
        "category": "destruction_tension",
        "staging": "pixabay-487416.mp3",
        "moods": ["tension", "fight", "adventure"],
    },
    {
        "track_id": "war-drums",
        "title": "War Drums",
        "artist": None,
        "source_url": "https://pixabay.com/music/build-up-scenes-war-drums-173853/",
        "category": "military_drums",
        "staging": "pixabay-173853.mp3",
        "moods": ["drums", "war", "percussion"],
    },
    {
        "track_id": "battle-drums",
        "title": "Battle Drums",
        "artist": None,
        "source_url": "https://pixabay.com/music/adventure-battle-drums-485960/",
        "category": "military_drums",
        "staging": "pixabay-485960.mp3",
        "moods": ["drums", "battle", "percussion"],
    },
    {
        "track_id": "epic-drums",
        "title": "Epic Drums",
        "artist": None,
        "source_url": "https://pixabay.com/music/orchestral-battle-drums-630760/",
        "category": "military_drums",
        "staging": "pixabay-630760.mp3",
        "moods": ["drums", "epic", "orchestral"],
    },
    {
        "track_id": "epic-orchestral",
        "title": "Epic Orchestral",
        "artist": None,
        "source_url": "https://pixabay.com/music/main-title-epic-orchestral-274992/",
        "category": "cinematic_orchestral",
        "staging": "pixabay-274992.mp3",
        "moods": ["orchestral", "cinematic", "emotional"],
    },
    {
        "track_id": "epic-battle-music-tunetank",
        "title": "Epic Battle Music",
        "artist": "Tunetank",
        "source_url": "https://pixabay.com/music/adventure-epic-battle-music-347288/",
        "category": "epic_battles",
        "staging": "pixabay-347288.mp3",
        "moods": ["epic", "battle", "adventure"],
    },
    {
        "track_id": "battle-esvisionaria",
        "title": "Battle",
        "artist": "esvisionaria",
        "source_url": "https://pixabay.com/music/epic-classical-battle-175695/",
        "category": "epic_battles",
        "staging": "pixabay-175695.mp3",
        "moods": ["epic", "classical", "battle"],
    },
    {
        "track_id": "epic-battle-yevhen-astafiev",
        "title": "Epic Battle",
        "artist": "YevhenAstafiev",
        "source_url": "https://pixabay.com/music/orchestral-epic-epic-battle-607250/",
        "category": "epic_battles",
        "staging": "pixabay-607250.mp3",
        "moods": ["epic", "battle", "orchestral"],
    },
]

PREVIEW_SECONDS = 30.0


def _probe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    audio = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "audio"),
        {},
    )
    fmt = data.get("format", {})
    return {
        "codec": audio.get("codec_name"),
        "sample_rate": int(audio["sample_rate"]) if audio.get("sample_rate") else None,
        "channels": audio.get("channels"),
        "duration_seconds": round(float(fmt.get("duration", 0.0)), 3),
        "size_bytes": int(fmt.get("size", path.stat().st_size)),
        "format_name": fmt.get("format_name", ""),
    }


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _make_preview(source: Path, dest: Path, duration: float) -> None:
    length = min(PREVIEW_SECONDS, duration)
    fade_start = max(0.0, length - 2.0)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(source),
            "-af",
            f"atrim=0:{length:.3f},asetpts=N/SR/TB,"
            f"afade=t=out:st={fade_start:.3f}:d={min(2.0, length):.3f}",
            "-c:a", "libmp3lame", "-b:a", "128k", str(dest),
        ],
        check=True,
    )


def build(staging: Path, out: Path) -> dict:
    for category in CATEGORIES:
        (out / category).mkdir(parents=True, exist_ok=True)
    (out / "previews").mkdir(parents=True, exist_ok=True)

    catalogue_tracks = []
    manifest_tracks = []
    missing = []
    for spec in TRACK_SPEC:
        staged = staging / spec["staging"]
        if not staged.is_file():
            missing.append(spec["track_id"])
            continue
        probe = _probe(staged)
        full_rel = f"{spec['category']}/{spec['track_id']}.mp3"
        full_path = out / full_rel
        if staged.suffix.lower() == ".mp3" and probe["codec"] == "mp3":
            full_path.write_bytes(staged.read_bytes())
        else:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error", "-i", str(staged),
                    "-vn", "-c:a", "libmp3lame", "-q:a", "2",
                    str(full_path),
                ],
                check=True,
            )
            probe = _probe(full_path)
        preview_rel = f"previews/{spec['track_id']}.mp3"
        _make_preview(full_path, out / preview_rel, probe["duration_seconds"])
        digest = _sha256(full_path)
        catalogue_tracks.append({
            "track_id": spec["track_id"],
            "content_sha256": digest,
            "title": spec["title"],
            "composer": None,
            "performer": spec["artist"],
            "source_class": "bundled",
            "moods": spec["moods"],
            "genres": [spec["category"]],
            "duration_seconds": probe["duration_seconds"],
            "probe": {
                "codec": probe["codec"],
                "sample_rate": probe["sample_rate"],
                "channels": probe["channels"],
            },
            "assets": {"full": full_rel, "preview": preview_rel},
            "added_at": "2026-11-27",
            "notes": f"Founder-supplied reference. Source: {spec['source_url']}",
        })
        manifest_tracks.append({
            "track_id": spec["track_id"],
            "title": spec["title"],
            "artist": spec["artist"],
            "source_url": spec["source_url"],
            "local_location": full_rel,
            "preview_location": preview_rel,
            "duration_seconds": probe["duration_seconds"],
            "format": "mp3",
            "file_size_bytes": full_path.stat().st_size,
            "category": spec["category"],
            "content_sha256": digest,
        })

    catalogue = {"catalogue_version": 1, "tracks": catalogue_tracks}
    (out / "catalogue.json").write_text(
        json.dumps(catalogue, indent=2) + "\n", encoding="utf-8"
    )
    manifest = {
        "library": "cinemora_music",
        "generated_at": "2026-11-27",
        "distinct_tracks": len(manifest_tracks),
        "missing_track_ids": missing,
        "tracks": manifest_tracks,
    }
    (out / "music_library_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path, default=DEFAULT_STAGING)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    manifest = build(args.staging, args.out)

    # Verify what we wrote through the live catalogue loader,
    # hashes included — a library that does not verify is not built.
    sys.path.insert(0, str(BACKEND_ROOT))
    from services import music_library

    catalogue = music_library.load_catalogue(
        args.out / "catalogue.json", asset_root=args.out, verify_hashes=True
    )
    print(f"catalogue verified: {len(catalogue)} track(s)")
    print(f"distinct tracks built: {manifest['distinct_tracks']}")
    if manifest["missing_track_ids"]:
        print("MISSING from staging (not built, not substituted):")
        for track_id in manifest["missing_track_ids"]:
            print(f"  - {track_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
