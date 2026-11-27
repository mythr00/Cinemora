import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from services import render_cache
from services.atomic_write import write_json_atomic, write_text_atomic


FPS = 30
WIDTH = 1920
HEIGHT = 1080

FFMPEG_TIMEOUT = 180

VIDEO_CODEC = "libx264"
PRESET = "ultrafast"
CRF = "23"

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".webm",
    ".mkv",
    ".avi",
}


def run_ffmpeg(command, timeout=FFMPEG_TIMEOUT):
    print()
    print("FFmpeg:")
    print(" ".join(str(x) for x in command))

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )

        if result.returncode != 0:
            print()
            print("FFMPEG FAILED:")
            print(result.stderr[-6000:])
            return False

        return True

    except subprocess.TimeoutExpired:
        print("FFMPEG TIMEOUT")
        return False

    except Exception as error:
        print(f"FFMPEG ERROR: {error}")
        return False


def _partial_path(final_path):
    """Sibling temp path for an in-progress encode of ``final_path``.

    Phase 1 durability: ffmpeg always encodes to a ``.partial-*``
    sibling in the SAME directory (so the publish rename is a
    same-filesystem rename) and the result is moved onto the final
    path with ``os.replace()`` only after the encode succeeds and
    validates. A process killed mid-encode can therefore never
    leave an unplayable file (e.g. one missing its moov atom, the
    known failure mode of ``-movflags +faststart`` written straight
    to the final path) sitting at a final path.
    """
    final_path = Path(final_path)
    # The media suffix stays LAST (clip_0001.partial-<pid>-<rand>.mp4):
    # ffmpeg infers the output muxer from the file extension, so a
    # trailing .partial-* suffix would make every encode fail.
    return final_path.with_name(
        f"{final_path.stem}.partial-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        f"{final_path.suffix}"
    )


def _discard_partial(partial_path):
    try:
        Path(partial_path).unlink()
    except OSError:
        pass


def _publish_partial(partial_path, final_path):
    os.replace(partial_path, final_path)


def probe_media(path):
    path = Path(path)

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries",
                "stream=duration,avg_frame_rate,r_frame_rate,nb_frames,width,height,pix_fmt:format=duration",
                "-of", "json",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            return None

        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        fmt = data.get("format") or {}
        if not streams:
            return None

        stream = streams[0]

        duration = None
        for raw in (stream.get("duration"), fmt.get("duration")):
            if raw not in (None, "N/A"):
                try:
                    duration = float(raw)
                    break
                except (TypeError, ValueError):
                    pass

        frame_count = None
        frame_count_estimated = False
        value = stream.get("nb_frames")
        if value not in (None, "N/A"):
            try:
                frame_count = int(value)
            except (TypeError, ValueError):
                frame_count = None

        if frame_count is None and duration:
            # AUD-22: this is an ESTIMATE (duration x nominal FPS), not a
            # measurement. It is flagged as such so audit_clip can recount
            # exactly instead of aborting a render over an estimate.
            frame_count = int(round(duration * FPS))
            frame_count_estimated = True

        def parse_rate(value):
            if not value or value == "N/A":
                return None
            try:
                if "/" in value:
                    numerator, denominator = value.split("/", 1)
                    denominator = float(denominator)
                    if denominator == 0:
                        return None
                    return float(numerator) / denominator
                return float(value)
            except (TypeError, ValueError, ZeroDivisionError):
                return None

        return {
            "duration": duration,
            "frame_count": frame_count,
            "frame_count_estimated": frame_count_estimated,
            "avg_fps": parse_rate(stream.get("avg_frame_rate")),
            "r_fps": parse_rate(stream.get("r_frame_rate")),
            "width": stream.get("width"),
            "height": stream.get("height"),
            "pix_fmt": stream.get("pix_fmt"),
        }

    except Exception:
        return None

def count_frames_exact(path):
    """Count a video's frames by decoding the headers (ffprobe
    -count_frames). Used by audit_clip when the container does not declare
    nb_frames, so an estimate never decides a frame-lock verdict (AUD-22).
    Returns an int, or None when counting fails."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-count_frames",
                "-show_entries", "stream=nb_read_frames",
                "-of", "json",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams:
            return None
        value = streams[0].get("nb_read_frames")
        if value in (None, "N/A"):
            return None
        return int(value)
    except Exception:
        return None


def probe_duration(path):
    info = probe_media(path)
    if not info:
        return None
    return info.get("duration")


def probe_frame_count(path):
    info = probe_media(path)
    if not info:
        return None
    return info.get("frame_count")


def valid_media(path):
    if not path:
        return False
    path = Path(path)
    if not path.exists():
        return False
    try:
        if path.stat().st_size < 1000:
            return False
    except OSError:
        return False
    return True


def seconds_to_frame(seconds):
    value = float(seconds)
    if value <= 0:
        return 0
    return int(round(value * FPS))


def frame_to_seconds(frame):
    return float(frame) / FPS


def duration_to_frames(duration):
    return max(seconds_to_frame(duration), 1)


def exact_duration_from_frames(frame_count):
    return frame_to_seconds(frame_count)


def item_start_seconds(item):
    for key in ("project_start", "start"):
        value = item.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return None


def item_end_seconds(item):
    for key in ("project_end", "end"):
        value = item.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    return None


def scene_items(scene):
    return scene.get("items") or scene.get("visuals") or []


def scene_first_start_frame(scene):
    """Frame where a scene's first item says it starts, or None."""
    items = scene_items(scene)
    if not items:
        return None
    start = item_start_seconds(items[0])
    if start is None:
        return None
    return seconds_to_frame(start)


def allocate_item_frames(items, starting_frame=0, final_frame=None, next_start_frame=None):
    """
    Allocate frames so clips are CONTIGUOUS with no gaps and no overlaps.

    Rules:
      - every clip starts exactly where the previous clip ended
      - a clip ends where the NEXT item starts (any gap in the timeline is
        absorbed into the previous shot instead of being dropped)
      - the last clip ends at final_frame, else next_start_frame (the next
        scene's first start), else its own end time, else its duration
      - if no timestamps exist, durations are accumulated with a fractional
        carry so rounding errors cannot build up

    Guarantee: sum(frame_count) == returned_cursor - starting_frame
    """
    allocations = []
    cursor = int(starting_frame)
    exact_cursor = float(cursor)
    count = len(items)

    for index, item in enumerate(items):
        is_last = index == count - 1
        start_frame = cursor
        end_frame = None
        used_timestamp = False

        if not is_last:
            next_start = item_start_seconds(items[index + 1])
            if next_start is not None:
                end_frame = seconds_to_frame(next_start)
                used_timestamp = True
        else:
            if final_frame is not None:
                end_frame = int(final_frame)
                used_timestamp = True
            elif next_start_frame is not None:
                end_frame = int(next_start_frame)
                used_timestamp = True

        if end_frame is None:
            own_end = item_end_seconds(item)
            if own_end is not None:
                end_frame = seconds_to_frame(own_end)
                used_timestamp = True

        if end_frame is None:
            raw_duration = item.get("duration", 5)
            try:
                duration = float(raw_duration)
            except (TypeError, ValueError):
                duration = 5.0
            duration = max(duration, 1.0 / FPS)
            exact_cursor += duration * FPS
            end_frame = int(round(exact_cursor))

        if end_frame <= start_frame:
            end_frame = start_frame + 1

        if used_timestamp:
            exact_cursor = float(end_frame)

        allocations.append({
            "index": index,
            "start_frame": start_frame,
            "end_frame": end_frame,
            "frame_count": end_frame - start_frame,
            "duration": exact_duration_from_frames(end_frame - start_frame),
            "source_duration": item.get("duration"),
        })
        cursor = end_frame

    total = sum(a["frame_count"] for a in allocations)
    if total != cursor - int(starting_frame):
        raise RuntimeError(
            f"ALLOCATION BUG: allocated {total} frames but span is "
            f"{cursor - int(starting_frame)}."
        )

    return allocations, cursor


def build_video_filter():
    return (
        f"scale={WIDTH}:{HEIGHT}:"
        "force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},"
        "setsar=1,"
        "format=yuv420p,"
        f"fps={FPS}"
    )


def is_motion_graphic_item(item):
    """True when a timeline item is a generated motion graphic
    (its asset record / editor assignment says so)."""
    if not isinstance(item, dict):
        return False
    return bool(
        item.get("motion_graphic")
        or item.get("assignment") == "motion_graphic"
        or item.get("source") == "cinemora_motion_graphics"
    )


def retime_factor_for_slot(source_duration, slot_duration):
    """The setpts factor that fits a WHOLE source into a shorter
    slot, or None when no retime applies (run-9 fix 4).

    Motion graphics are authored as complete sequences — the
    chronology graphic reveals its events across its full rendered
    duration — so trimming one to its timeline slot silently
    amputates the content (run e95ac155: a 6.0s chronology placed
    in a 2.76s sentence window showed one of six events). Footage
    trims; graphics retime. Returns slot/source when the source is
    meaningfully longer than the slot and the speedup stays within
    5x; otherwise None (historical trim behaviour)."""
    try:
        source = float(source_duration)
        slot = float(slot_duration)
    except (TypeError, ValueError):
        return None
    if source <= 0 or slot <= 0 or source <= slot * 1.05:
        return None
    factor = slot / source
    if factor < 0.2:
        return None
    return factor


def build_image_motion_filter(frame_count, motion=None):
    """Stage 3F (MA-16): real motion for stills via zoompan.

    The still is scaled/cropped to a 2x intermediate (3840x2160) so
    zoompan works on a large raster (avoiding its notorious jitter),
    then zoompan emits EXACTLY ``frame_count`` frames at the project
    fps, downscaled to 1920x1080. Motions: "zoom_in" (default slow
    push-in), "zoom_out", "pan_right", "pan_left". Unknown values
    fall back to "zoom_in". The frame count is locked by zoompan's
    d= parameter plus the caller's -frames:v, so the renderer's
    frame-lock audits are unaffected.
    """
    frames = max(int(frame_count), 1)
    motion = str(motion or "zoom_in").lower()
    # Slow zoom: reach ~1.10x over the whole clip.
    inc = 0.10 / frames
    if motion == "zoom_out":
        z = f"if(eq(on,0),1.10,max(zoom-{inc:.6f},1.0))"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    elif motion == "pan_right":
        z = "1.15"
        x = f"(iw-iw/zoom)*on/{max(frames - 1, 1)}"
        y = "ih/2-(ih/zoom/2)"
    elif motion == "pan_left":
        z = "1.15"
        x = f"(iw-iw/zoom)*(1-on/{max(frames - 1, 1)})"
        y = "ih/2-(ih/zoom/2)"
    else:  # zoom_in
        motion = "zoom_in"
        z = f"min(zoom+{inc:.6f},1.10)"
        x = "iw/2-(iw/zoom/2)"
        y = "ih/2-(ih/zoom/2)"
    return (
        f"scale={WIDTH * 2}:{HEIGHT * 2}:"
        "force_original_aspect_ratio=increase,"
        f"crop={WIDTH * 2}:{HEIGHT * 2},"
        "setsar=1,"
        f"zoompan=z='{z}':x='{x}':y='{y}'"
        f":d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS},"
        "format=yuv420p"
    )


def create_image_clip(image_path, duration, output_path, frame_count=None, motion=None):
    if not valid_media(image_path):
        print(f"Skipping invalid image: {image_path}")
        return False

    if frame_count is None:
        frame_count = duration_to_frames(duration)
    frame_count = max(int(frame_count), 1)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    partial_path = _partial_path(output_path)
    command = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", str(image_path),
        "-vf", build_image_motion_filter(frame_count, motion=motion),
        "-frames:v", str(frame_count),
        "-fps_mode", "cfr",
        "-c:v", VIDEO_CODEC,
        "-preset", PRESET,
        "-crf", CRF,
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        str(partial_path),
    ]

    success = run_ffmpeg(command)
    if not success or not valid_media(partial_path):
        _discard_partial(partial_path)
        print("Image clip was not created.")
        return False

    try:
        _publish_partial(partial_path, output_path)
    except OSError:
        _discard_partial(partial_path)
        print("Image clip was not created.")
        return False

    print(
        f"IMAGE CLIP COMPLETE: {output_path} "
        f"frames={frame_count} "
        f"duration={exact_duration_from_frames(frame_count):.3f}s"
    )
    return True


def create_video_clip(video_path, duration, output_path, frame_count=None, start_offset=0.0, retime_factor=None):
    if not valid_media(video_path):
        print(f"Skipping invalid video: {video_path}")
        return False

    if frame_count is None:
        frame_count = duration_to_frames(duration)
    frame_count = max(int(frame_count), 1)

    try:
        start_offset = float(start_offset or 0.0)
    except (TypeError, ValueError):
        start_offset = 0.0
    if start_offset < 0:
        start_offset = 0.0

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Founder defect ruling 2026-11-23 (flickering footage): one
    # placement of an asset is ONE continuous segment. The
    # historical command looped the source (-stream_loop -1), so
    # any source shorter than its timeline slot repeated from
    # frame 0 — a 3s motion graphic in a 9s slot visibly
    # restarted its entrance animation three times (photos fading
    # in, vanishing, fading in again), and short footage looped
    # the same way. The source now plays ONCE; when it is shorter
    # than the slot, tpad clones its LAST frame to the slot end
    # (a settled hold, never a restart). The start offset is
    # clamped inside the source for the same reason: with no loop
    # to wrap around, an out-of-range offset would yield no
    # frames at all.
    if start_offset > 0:
        _probe = probe_media(video_path)
        _source_duration = (_probe or {}).get("duration")
        if _source_duration:
            start_offset = min(
                start_offset, max(0.0, float(_source_duration) - 0.1)
            )

    video_filter = build_video_filter()
    if retime_factor:
        # Run-9 fix 4: compress the source's presentation timestamps
        # so the whole sequence fits the slot instead of being
        # trimmed at the slot boundary.
        video_filter = f"setpts=PTS*{float(retime_factor):.6f}," + video_filter
    video_filter += (
        f",tpad=stop_mode=clone:stop_duration={float(duration):.3f}"
    )

    partial_path = _partial_path(output_path)
    command = [
        "ffmpeg", "-y",
    ]
    # Stage 3F (MA-15): input seeking — "-ss" BEFORE "-i" starts
    # the source at the asset's verified segment offset instead of
    # always at t=0.
    if start_offset > 0:
        command.extend(["-ss", str(start_offset)])
    command.extend([
        "-i", str(video_path),
        "-vf", video_filter,
        "-frames:v", str(frame_count),
        "-fps_mode", "cfr",
        "-c:v", VIDEO_CODEC,
        "-preset", PRESET,
        "-crf", CRF,
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        str(partial_path),
    ])

    success = run_ffmpeg(command)
    if not success or not valid_media(partial_path):
        _discard_partial(partial_path)
        print("Video clip was not created.")
        return False

    try:
        _publish_partial(partial_path, output_path)
    except OSError:
        _discard_partial(partial_path)
        print("Video clip was not created.")
        return False

    print(
        f"VIDEO CLIP COMPLETE: {output_path} "
        f"frames={frame_count} "
        f"duration={exact_duration_from_frames(frame_count):.3f}s"
    )
    return True


def _render_settings_fingerprint():
    """The render settings that determine every encoded artefact
    (Phase 5 cache keys). The code fingerprint in
    ``render_cache`` covers the filter builders themselves; this
    carries their current values."""
    return {
        "fps": FPS,
        "width": WIDTH,
        "height": HEIGHT,
        "codec": VIDEO_CODEC,
        "preset": PRESET,
        "crf": CRF,
        "video_filter": build_video_filter(),
    }


def _create_clip_cached(store, kind, source_path, duration, output_path,
                        frame_count, motion=None, start_offset=0.0,
                        retime_factor=None):
    """``create_image_clip`` / ``create_video_clip`` through the
    Phase 5 clip cache.

    On a cache hit the stored encode is placed at ``output_path``
    and no encode runs — a render resumed after a kill re-uses
    every completed clip (acceptance #2). On a miss the ordinary
    create runs and its result is stored. The key covers the
    source's content hash and every encode parameter, so a changed
    input always misses. All cache behaviour is fail-soft: any
    cache problem degrades to the ordinary create.
    """
    key = None
    if store is not None:
        source_hash = render_cache.hash_file(source_path)
        if source_hash:
            key = render_cache.clip_key({
                "kind": kind,
                "source_sha256": source_hash,
                "frame_count": int(frame_count),
                "duration": float(duration),
                "motion": motion,
                "start_offset": float(start_offset or 0.0),
                "retime_factor": (
                    None if not retime_factor else float(retime_factor)
                ),
                "settings": _render_settings_fingerprint(),
            })
            if store.place("clip", key, output_path):
                store.stats["clip_hits"] += 1
                print(f"CLIP CACHE HIT: {output_path}")
                return True
            store.stats["clip_misses"] += 1
    if kind == "video":
        success = create_video_clip(
            source_path, duration, output_path,
            frame_count=frame_count,
            start_offset=start_offset,
            retime_factor=retime_factor,
        )
    else:
        success = create_image_clip(
            source_path, duration, output_path,
            frame_count=frame_count,
            motion=motion,
        )
    if success and store is not None and key is not None:
        store.put("clip", key, output_path)
    return success


def audit_clip(clip_path, expected_frames, expected_duration, scene_number, item_index):
    info = probe_media(clip_path)
    result = {
        "scene": scene_number,
        "item": item_index,
        "file": str(clip_path),
        "expected_frames": int(expected_frames),
        "actual_frames": None,
        "expected_duration": float(expected_duration),
        "actual_duration": None,
        "frame_delta": None,
        "duration_delta": None,
        "fps": None,
        "width": None,
        "height": None,
        "status": "failed",
    }

    if info:
        actual_frames = info.get("frame_count")
        actual_duration = info.get("duration")

        # AUD-22: if the frame count was only an estimate and it disagrees
        # with the expectation, count the frames exactly before declaring
        # a mismatch — an estimate must never abort a whole render.
        if (
            info.get("frame_count_estimated")
            and actual_frames is not None
            and int(actual_frames) != int(expected_frames)
        ):
            exact = count_frames_exact(clip_path)
            if exact is not None:
                actual_frames = exact
                result["frame_count_source"] = "counted_exact"

        result["actual_frames"] = actual_frames
        result["actual_duration"] = actual_duration
        result["fps"] = info.get("avg_fps")
        result["width"] = info.get("width")
        result["height"] = info.get("height")

        if actual_frames is not None:
            result["frame_delta"] = int(actual_frames) - int(expected_frames)
        if actual_duration is not None:
            result["duration_delta"] = float(actual_duration) - float(expected_duration)

        exact_frames_ok = (
            actual_frames is not None
            and int(actual_frames) == int(expected_frames)
        )
        exact_video_ok = info.get("width") == WIDTH and info.get("height") == HEIGHT
        fps_ok = info.get("avg_fps") is not None and abs(float(info["avg_fps"]) - FPS) < 0.01

        if exact_frames_ok and exact_video_ok and fps_ok:
            result["status"] = "ok"
        else:
            result["status"] = "mismatch"

    return result


def flatten_timeline_items(scenes):
    items = []
    for scene in scenes:
        for item in scene_items(scene):
            items.append(item)
    return items


def render_scene(scene, project_dir, starting_frame=0, final_frame=None, next_start_frame=None, cache_store=None):
    scene_number = scene.get("scene_number", 1)
    items = scene_items(scene)
    # Phase 5: clip encodes go through the deterministic clip
    # cache (see _create_clip_cached). Callers that track cache
    # statistics pass their store; otherwise a store bound to this
    # project dir is used.
    store = (
        cache_store if cache_store is not None
        else render_cache.CacheStore(project_dir)
    )

    if not items:
        print(f"Scene {scene_number}: no timeline items.")
        return [], starting_frame, {
            "scene": scene_number,
            "expected_frames": 0,
            "actual_frames": 0,
            "items": [],
            "status": "empty",
            "placeholder_count": 0,
            "placeholder_items": [],
            "holdover_count": 0,
            "holdover_items": [],
        }

    clips_dir = Path(project_dir) / "clips" / f"scene_{scene_number}"
    clips_dir.mkdir(parents=True, exist_ok=True)

    rendered_clips = []
    audits = []
    # Placeholder clips were removed permanently (founder directive):
    # the renderer never fabricates a labelled stand-in card. When an
    # item's media is missing or fails to render, the slot is covered
    # by HOLDOVER — re-rendering a genuine visual from this scene
    # (the most recent good one, else the scene's first valid media)
    # for the item's exact duration — and every holdover is recorded
    # here and surfaced in the scene audit, the renderer timing audit
    # and the project asset report. If the scene has no genuine media
    # at all, rendering fails loudly instead of shipping a card.
    holdover_items = []
    # Phase 6 (AM-011): per-item composition outcomes for the
    # scene audit — applied layers and honest failures alike.
    composition_records = []
    scene_fallback_media = None
    for _candidate in items:
        _candidate_path = _candidate.get("file_path") or _candidate.get("path") or ""
        if _candidate_path and valid_media(_candidate_path):
            scene_fallback_media = (
                (_candidate.get("type") or "").lower(),
                _candidate_path,
            )
            break
    last_good_media = None
    allocations, scene_end_frame = allocate_item_frames(
        items,
        starting_frame,
        final_frame=final_frame,
        next_start_frame=next_start_frame,
    )

    planned_frames = sum(a["frame_count"] for a in allocations)

    print()
    print("=" * 70)
    print(f"RENDERING SCENE {scene_number}")
    print("=" * 70)
    print(f"Planned frames: {planned_frames} ({starting_frame} -> {scene_end_frame})")

    for index, item in enumerate(items):
        allocation = allocations[index]
        media_type = (item.get("type") or "").lower()
        media_path = item.get("file_path") or item.get("path") or ""
        frame_count = allocation["frame_count"]
        duration = allocation["duration"]
        output_path = clips_dir / f"clip_{index + 1:04d}.mp4"

        print()
        print(f"Visual {index + 1}/{len(items)}")
        print(f"Type: {media_type}")
        print(f"Frames: {frame_count}")
        print(f"Duration: {duration:.3f}s")
        print(f"Frame range: {allocation['start_frame']} -> {allocation['end_frame']}")
        print(f"Source: {media_path or '(none)'}")

        success = False
        media_ok = bool(media_path and valid_media(media_path))
        # Stage 3F: deterministic still motion chosen from the item
        # index (even = zoom-in, odd = pan-right); video items seek
        # to their timeline clip_start.
        item_motion = "zoom_in" if index % 2 == 0 else "pan_right"
        try:
            item_clip_start = float(item.get("clip_start") or 0.0)
        except (TypeError, ValueError):
            item_clip_start = 0.0
        if media_ok:
            if media_type == "video":
                # Run-9 fix 4: a motion graphic longer than its
                # slot is retimed to fit whole, never trimmed —
                # trimming a chronology/stat graphic amputates the
                # content its choreography reveals last.
                _retime = None
                if is_motion_graphic_item(item):
                    _probe = probe_media(media_path)
                    _retime = retime_factor_for_slot(
                        (_probe or {}).get("duration"), duration
                    )
                    if _retime:
                        print(
                            "Motion graphic retimed to fit slot: "
                            f"factor={_retime:.3f}"
                        )
                success = _create_clip_cached(
                    store,
                    "video",
                    media_path,
                    duration,
                    output_path,
                    frame_count=frame_count,
                    start_offset=item_clip_start,
                    retime_factor=_retime,
                )
            else:
                success = _create_clip_cached(
                    store,
                    "image",
                    media_path,
                    duration,
                    output_path,
                    frame_count=frame_count,
                    motion=item_motion,
                )
        else:
            print(
                f"Item {index + 1}: missing or invalid media "
                "— covering with a holdover of genuine scene media."
            )

        used_holdover = False
        holdover_path = None
        if not success:
            holdover = last_good_media or scene_fallback_media
            if holdover is None:
                raise RuntimeError(
                    f"Scene {scene_number} item {index + 1}: media missing "
                    "or failed to render and the scene has no genuine "
                    "visual to hold over — refusing to substitute a "
                    "placeholder."
                )
            holdover_type, holdover_path = holdover
            if holdover_type == "video":
                success = _create_clip_cached(
                    store,
                    "video",
                    holdover_path,
                    duration,
                    output_path,
                    frame_count=frame_count,
                    start_offset=item_clip_start,
                )
            else:
                success = _create_clip_cached(
                    store,
                    "image",
                    holdover_path,
                    duration,
                    output_path,
                    frame_count=frame_count,
                    motion=item_motion,
                )
            used_holdover = bool(success)

        if not success:
            raise RuntimeError(
                f"Unable to create timing-preserving clip scene={scene_number} item={index + 1}"
            )

        if used_holdover:
            holdover_items.append(
                {
                    "item_index": index + 1,
                    "sentence_index": item.get("sentence_index"),
                    "sentence": str(item.get("sentence") or "")[:120],
                    "reason": (
                        "missing_or_invalid_media"
                        if not media_ok
                        else "clip_creation_failed"
                    ),
                    "held_from": holdover_path,
                }
            )
        elif media_ok:
            last_good_media = (media_type, media_path)

        # Phase 6 (AM-011): composition. When the item carries a
        # composition plan, its validated layers are overlaid on
        # the rendered base clip BEFORE the frame audit, so the
        # audit verifies the clip the audience will actually see.
        # Skipped on holdovers — a layer's label names the planned
        # visual, not the held one. A failed layer never fails the
        # item: the base clip stands, and the outcome (applied or
        # failed) is recorded on the item and in the scene audit.
        if success and not used_holdover and item.get("composition"):
            from services import composition as _composition_module

            _composed_path = clips_dir / f"clip_{index + 1:04d}.composed.mp4"
            # Phase 5: composed-clip cache — keyed by the base
            # clip's content hash + the canonical composition plan.
            # The build record is stored in the entry's sidecar, so
            # a hit reproduces the record verbatim (the record is
            # part of the scene audit and the item's render stamp).
            _comp_ok = False
            _comp_record = None
            _comp_key = None
            _base_hash = render_cache.hash_file(output_path)
            if _base_hash:
                _comp_key = render_cache.composed_key({
                    "base_sha256": _base_hash,
                    "plan": render_cache.canonicalise(
                        item["composition"], _depth=1
                    ),
                })
                if store.place("clip", _comp_key, _composed_path):
                    _cached_meta = store.metadata("clip", _comp_key) or {}
                    if isinstance(_cached_meta.get("record"), dict):
                        _comp_ok = True
                        _comp_record = _cached_meta["record"]
                        print(
                            f"COMPOSED CLIP CACHE HIT: {_composed_path}"
                        )
            if _comp_record is None:
                _comp_ok, _comp_record = (
                    _composition_module.apply_composition(
                        output_path,
                        item["composition"],
                        _composed_path,
                        clips_dir / f"composition_{index + 1:04d}",
                    )
                )
                if _comp_ok and _comp_key is not None:
                    store.put(
                        "clip", _comp_key, _composed_path,
                        metadata={"record": _comp_record},
                    )
            if _comp_ok:
                _composed_path.replace(output_path)
            _comp_record = {
                **_comp_record,
                "item_index": index + 1,
                "applied_to_item": _comp_ok,
            }
            if isinstance(item.get("composition"), dict):
                item["composition"]["render"] = _comp_record
            composition_records.append(_comp_record)
            print(
                f"Composition for item {index + 1}: "
                f"{'applied' if _comp_ok else 'FAILED — base clip kept'} "
                f"({_comp_record})"
            )

        audit = audit_clip(
            output_path,
            expected_frames=frame_count,
            expected_duration=duration,
            scene_number=scene_number,
            item_index=index + 1,
        )
        audits.append(audit)

        if audit["status"] != "ok":
            raise RuntimeError(
                "FRAME LOCK FAILURE: "
                f"scene={scene_number} "
                f"item={index + 1} "
                f"expected_frames={frame_count} "
                f"actual_frames={audit.get('actual_frames')} "
                f"expected_duration={duration:.6f} "
                f"actual_duration={audit.get('actual_duration')}"
            )

        rendered_clips.append(str(output_path))

    expected_frames = scene_end_frame - starting_frame
    actual_frames = sum(
        int(item["actual_frames"])
        for item in audits
        if item["actual_frames"] is not None
    )
    status = (
        "ok"
        if actual_frames == expected_frames and len(rendered_clips) == len(items)
        else "mismatch"
    )

    scene_audit = {
        "scene": scene_number,
        "start_frame": starting_frame,
        "end_frame": scene_end_frame,
        "expected_frames": expected_frames,
        "actual_frames": actual_frames,
        "expected_duration": exact_duration_from_frames(expected_frames),
        "actual_duration": exact_duration_from_frames(actual_frames),
        "clip_count": len(rendered_clips),
        "timeline_item_count": len(items),
        "status": status,
        "items": audits,
        # Placeholder clips no longer exist in the renderer; the
        # count is reported as a true 0 (not "unknown") so the asset
        # report's authenticity accounting stays exact.
        "placeholder_count": 0,
        "placeholder_items": [],
        "holdover_count": len(holdover_items),
        "holdover_items": holdover_items,
        # Phase 6 (AM-011): composition outcomes for this scene.
        "composition_records": composition_records,
    }

    print()
    print(f"SCENE {scene_number} FRAME AUDIT:")
    print(f"Expected frames: {expected_frames}")
    print(f"Actual frames:   {actual_frames}")
    print(f"Delta:           {actual_frames - expected_frames}")

    if status != "ok":
        raise RuntimeError(
            f"Scene {scene_number} frame audit failed: "
            f"expected {expected_frames}, got {actual_frames}."
        )

    return rendered_clips, scene_end_frame, scene_audit


def write_concat_file(clips, output_path):
    concat_file = Path(output_path).parent / "concat.txt"
    lines = []
    for clip in clips:
        clip_path = Path(clip).resolve()
        safe_path = str(clip_path).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{safe_path}'\n")
    write_text_atomic(concat_file, "".join(lines))
    return concat_file


def concatenate_clips(clips, output_path, expected_frames=None):
    if not clips:
        print("No clips available for concatenation.")
        return False

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    concat_file = write_concat_file(clips, output_path)

    # Phase 1 durability: the re-encode lands on a unique partial
    # sibling, never on documentary_repaired.mp4 / the master path
    # itself, and is published onto output_path with os.replace()
    # only after the frame-count validation below passes. A kill
    # mid-encode leaves at most an orphaned .partial file; the
    # previous master (if any) stays byte-identical throughout.
    repaired_output = _partial_path(
        output_path.parent / "documentary_repaired.mp4"
    )

    repair_filter = (
        f"fps={FPS},"
        f"scale={WIDTH}:{HEIGHT}:"
        "force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},"
        "setsar=1,"
        "format=yuv420p"
    )

    command = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),
        "-vf", repair_filter,
        "-fps_mode", "cfr",
        "-r", str(FPS),
        "-c:v", VIDEO_CODEC,
        "-preset", PRESET,
        "-crf", CRF,
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
    ]
    if expected_frames is not None:
        command.extend(["-frames:v", str(int(expected_frames))])
    command.append(str(repaired_output))

    print()
    print("=" * 70)
    print("CONCAT RE-ENCODE")
    print("=" * 70)
    success = run_ffmpeg(command, timeout=max(FFMPEG_TIMEOUT, 900))
    if not success or not valid_media(repaired_output):
        _discard_partial(repaired_output)
        return False

    repaired_info = probe_media(repaired_output)
    repaired_frames = repaired_info.get("frame_count") if repaired_info else None
    repaired_duration = repaired_info.get("duration") if repaired_info else None

    print(f"JOINED frames: {repaired_frames}")
    print(f"JOINED duration: {float(repaired_duration or 0):.6f}s")

    if repaired_frames is None and repaired_output.exists() and repaired_output.stat().st_size > 10000:
        print("Probe did not return frames; keeping joined file.")
        repaired_frames = expected_frames

    if expected_frames is not None and repaired_frames is not None:
        if abs(int(repaired_frames) - int(expected_frames)) > 2:
            _discard_partial(repaired_output)
            raise RuntimeError(
                "RENDERER FRAME REPAIR FAILED: "
                f"expected {expected_frames} frames, got {repaired_frames}."
            )

    try:
        # Atomic publish: os.replace overwrites the target in one
        # rename — the target is NEVER unlinked first, so there is
        # no window in which no master exists.
        _publish_partial(repaired_output, output_path)
    except OSError as error:
        _discard_partial(repaired_output)
        raise RuntimeError(f"Could not finalize documentary: {error}")

    print("CONCAT COMPLETE.")
    return True


def _plan_transition_windows(plan, clip_frames):
    """Compute the honoured transitions and their effective windows.

    Extracted verbatim from ``_build_transition_join`` (Phase 5) so
    the chapter renderer and the monolithic join share ONE window
    computation: ``effective`` maps a clip index to the window
    (frames) of the honoured transition INTO that clip, shrunk
    where a short clip carries both an incoming and an outgoing
    transition whose windows would together exceed its frame
    count. ``info`` is the audit record skeleton — downgrades from
    the plan and from the shrink are recorded; segment results are
    added by the caller. ``effective`` is empty when nothing is
    honoured or every honoured window shrank away (``info`` status
    is then ``all_cut``).
    """
    honoured = {
        int(record["index"]): record
        for record in plan
        if record.get("honoured")
    }
    info = {
        "status": "applied",
        "applied": [],
        "downgraded": [
            {
                "index": record["index"],
                "requested_transition": record.get("requested_transition"),
                "transition_reason": record.get("transition_reason"),
                "downgrade_cause": record.get("downgrade_cause"),
            }
            for record in plan
            if record.get("downgraded")
        ],
        "records": plan,
        "fallback_reason": None,
    }
    if not honoured:
        info["status"] = "all_cut"
        return {}, honoured, info

    # Effective windows, shrunk where a short clip carries both an
    # incoming and an outgoing transition whose windows would
    # together exceed its frame count.
    effective = {
        index: int(record["window_frames"])
        for index, record in honoured.items()
    }
    for clip_index, frames in enumerate(clip_frames):
        incoming = effective.get(clip_index, 0)
        outgoing = effective.get(clip_index + 1, 0)
        overflow = incoming + outgoing - int(frames)
        if overflow <= 0:
            continue
        cut_outgoing = min(overflow, outgoing)
        effective[clip_index + 1] = outgoing - cut_outgoing
        overflow -= cut_outgoing
        if overflow > 0:
            effective[clip_index] = incoming - min(overflow, incoming)
    for index in list(effective):
        record = honoured[index]
        if effective[index] != int(record["window_frames"]):
            record = {**record, "window_frames_original": record["window_frames"]}
            honoured[index] = record
        if effective[index] < 1:
            effective.pop(index)
            info["downgraded"].append({
                "index": index,
                "requested_transition": record.get("requested_transition"),
                "transition_reason": record.get("transition_reason"),
                "downgrade_cause": "insufficient_clip_frames",
            })
            record["honoured"] = False
            record["downgraded"] = True
            record["downgrade_cause"] = "insufficient_clip_frames"
            record["transition"] = "cut"
    if not effective:
        info["status"] = "all_cut"
    return effective, honoured, info


def _build_transition_join(all_clips, clip_frames, plan, project_dir):
    """Build the frame-exact join sequence for honoured transitions.

    For each honoured transition into clip i (window W): clip i-1's
    main part keeps its first (frames - W) frames, a 2W-frame
    transition segment replaces clip i-1's last W frames + clip i's
    first W frames, and clip i's main part starts at frame W. The
    sequence therefore sums to exactly the original total.

    Returns (join_clips, info) on success, or (None, info) when the
    caller must fall back to the untouched concat path.
    """
    from services import transitions as _transitions

    effective, honoured, info = _plan_transition_windows(plan, clip_frames)
    if not effective:
        return None, info

    workdir = Path(project_dir) / "transitions"
    workdir.mkdir(parents=True, exist_ok=True)

    segments = {}
    for index, window in sorted(effective.items()):
        record = honoured[index]
        segment = _transitions.build_transition_segment(
            all_clips[index - 1],
            all_clips[index],
            record["transition"],
            window,
            workdir / f"transition_{index:04d}.mp4",
        )
        if segment is None:
            info["status"] = "fallback_concat"
            info["fallback_reason"] = (
                f"transition_build_failed:index={index}:kind={record['transition']}"
            )
            return None, info
        segments[index] = segment
        info["applied"].append({
            "index": index,
            "transition": record["transition"],
            "transition_reason": record.get("transition_reason"),
            "window_frames": segment["window_frames"],
            "requested_window_frames": segment["requested_window_frames"],
            "window_clamped": segment["window_clamped"],
            "segment_frames": segment["frames"],
            "segment": segment["path"],
            "filtergraph": segment["filtergraph"],
        })

    join_clips = []
    total = 0
    for clip_index, clip in enumerate(all_clips):
        incoming = effective.get(clip_index, 0)
        outgoing = effective.get(clip_index + 1, 0)
        if clip_index in segments:
            join_clips.append(segments[clip_index]["path"])
            total += segments[clip_index]["frames"]
        main_frames = int(clip_frames[clip_index]) - incoming - outgoing
        if main_frames < 0:
            info["status"] = "fallback_concat"
            info["fallback_reason"] = f"negative_main_part:clip={clip_index}"
            return None, info
        if main_frames == 0:
            continue
        if incoming == 0 and outgoing == 0:
            join_clips.append(clip)
            total += main_frames
            continue
        main_path = _transitions.trim_clip_frames(
            clip,
            incoming,
            main_frames,
            workdir / f"main_{clip_index:04d}.mp4",
        )
        if main_path is None:
            info["status"] = "fallback_concat"
            info["fallback_reason"] = f"main_trim_failed:clip={clip_index}"
            return None, info
        join_clips.append(main_path)
        total += main_frames

    expected = sum(int(frames) for frames in clip_frames)
    if total != expected:
        info["status"] = "fallback_concat"
        info["fallback_reason"] = (
            f"join_frame_total_mismatch:built={total}:expected={expected}"
        )
        return None, info
    info["join_frame_total"] = total
    return join_clips, info


def render_timeline(scenes, project_dir, expected_duration=None):
    """Final-render entry (pipeline + editor). Phase 8.4: the
    encode runs under a render slot (services/render_slots) so
    concurrent productions overlap their retrieval/narration but
    serialise their CPU-saturating final encodes per
    CINEMORA_RENDER_CONCURRENCY (design 03 §6.2)."""
    from services import render_slots

    with render_slots.render_slot():
        return _render_timeline_impl(
            scenes, project_dir, expected_duration=expected_duration
        )


class _ChapterPathFallback(Exception):
    """The chapter render path found an inconsistency (a segment
    that would not build, a chapter whose frames drifted, an
    assembly that failed its audit). The render restarts on the
    monolithic legacy path, whose own verdicts then decide —
    the chapter path may only ever make a render faster, never
    change its outcome."""


def _render_timeline_impl(scenes, project_dir, expected_duration=None):
    if render_cache.chapter_rendering_enabled():
        try:
            return _render_timeline_chaptered(
                scenes, project_dir, expected_duration=expected_duration
            )
        except _ChapterPathFallback as error:
            print(
                f"CHAPTER RENDERING FALLBACK: {error} — "
                "re-rendering on the monolithic path."
            )
    return _render_timeline_legacy(
        scenes, project_dir, expected_duration=expected_duration
    )


def _scene_render_is_clean(scene_audit):
    """True when a scene's render was a deterministic function of
    its inputs: no holdovers (their source choice depends on
    encode success) and no composition step that failed inside
    ffmpeg (a transient suspect). Only clean scenes are stored
    under their input-derived chapter key; an unclean scene simply
    re-renders next time — a missed cache entry, never a stale
    one. Deterministic composition outcomes (a plan with no valid
    layers, a non-renderable layer kind) stay clean: they are
    functions of the plan, which is in the key."""
    if int(scene_audit.get("holdover_count") or 0) > 0:
        return False
    for record in scene_audit.get("composition_records") or []:
        for failure in record.get("failed") or []:
            if isinstance(failure, dict) and failure.get("reason") in (
                "overlay_render_failed", "composition_exception",
            ):
                return False
    return True


def _probe_frame_count_exactish(path):
    """Frame count for cache validation: the container count, or
    an exact packet count when the container count is only an
    estimate (AUD-22 discipline)."""
    info = probe_media(path)
    if not info:
        return None
    frames = info.get("frame_count")
    if frames is not None and info.get("frame_count_estimated"):
        exact = count_frames_exact(path)
        if exact is not None:
            return exact
    return frames


def _cached_trim(store, clip_path, start_frame, frame_count, work_path):
    """``transitions.trim_clip_frames`` through the segment cache,
    keyed by the clip's content hash + the exact frame window."""
    from services import transitions as _transitions

    clip_hash = render_cache.hash_file(clip_path)
    key = None
    if clip_hash:
        key = render_cache.segment_key({
            "op": "trim",
            "clip_sha256": clip_hash,
            "start_frame": int(start_frame),
            "frame_count": int(frame_count),
        })
        if store.place("seg", key, work_path):
            store.stats["segment_hits"] += 1
            return str(work_path)
        store.stats["segment_misses"] += 1
    result = _transitions.trim_clip_frames(
        clip_path, start_frame, frame_count, work_path
    )
    if result is None:
        return None
    if key is not None:
        store.put("seg", key, work_path)
    return result


def _cached_transition_segment(store, clip_a, clip_b, kind, window,
                               work_path, clip_a_frames, clip_b_frames,
                               expected_hashes=None):
    """``transitions.build_transition_segment`` through the segment
    cache, keyed by both clips' content hashes + kind + window.

    ``expected_hashes`` pins the clips' identities (chapter
    metadata): a cached segment is served without touching the
    clip files at all, and a build happens only when the files on
    disk hash to exactly the expected content — a replaced clip
    can therefore never leak into a segment under a stale key.
    Returns the segment record (same shape as
    ``build_transition_segment``) or None."""
    from services import transitions as _transitions

    if expected_hashes is not None:
        hash_a, hash_b = expected_hashes
    else:
        hash_a = render_cache.hash_file(clip_a)
        hash_b = render_cache.hash_file(clip_b)
    key = None
    if hash_a and hash_b:
        key = render_cache.segment_key({
            "op": "transition",
            "clip_a_sha256": hash_a,
            "clip_b_sha256": hash_b,
            "kind": str(kind),
            "window_frames": int(window),
            "clip_a_frames": int(clip_a_frames),
            "clip_b_frames": int(clip_b_frames),
        })
        if store.place("seg", key, work_path):
            store.stats["segment_hits"] += 1
            record = dict(store.metadata("seg", key) or {})
            record["path"] = str(work_path)
            record["kind"] = str(kind)
            return record
        store.stats["segment_misses"] += 1
    if expected_hashes is not None:
        if render_cache.hash_file(clip_a) != hash_a:
            return None
        if render_cache.hash_file(clip_b) != hash_b:
            return None
    segment = _transitions.build_transition_segment(
        clip_a, clip_b, kind, window, work_path
    )
    if segment is None:
        return None
    if key is not None:
        metadata = {
            name: value for name, value in segment.items()
            if name != "path"
        }
        store.put("seg", key, work_path, metadata=metadata)
    return segment


def _applied_record(record, segment):
    """The transitions-audit "applied" entry for one built segment
    (same shape the monolithic join records)."""
    return {
        "index": int(record["index"]),
        "transition": record["transition"],
        "transition_reason": record.get("transition_reason"),
        "window_frames": segment["window_frames"],
        "requested_window_frames": segment["requested_window_frames"],
        "window_clamped": segment["window_clamped"],
        "segment_frames": segment["frames"],
        "segment": segment["path"],
        "filtergraph": segment["filtergraph"],
    }


def _build_chapter_segments(store, plan, scene_clips, clip_frames,
                            effective, honoured, project_dir):
    """Build one chapter's join segments (internal transition
    segments + trimmed/full mains, in order) through the segment
    cache. The chapter's incoming boundary segment is NOT part of
    its body — it is assembled between chapters. Returns
    (segment_paths, applied_records); raises _ChapterPathFallback
    on any build failure (the monolithic path then decides)."""
    first = plan["first_clip_index"]
    last = plan["last_clip_index"]
    # The workdir is created lazily by the segment builders
    # themselves — a chapter with nothing to build must not create
    # it (the all-cut film has no transitions/ directory at all).
    workdir = Path(project_dir) / "transitions"
    segment_paths = []
    applied_records = []
    for clip_index in range(first, last + 1):
        clip = scene_clips[clip_index - first]
        incoming = effective.get(clip_index, 0)
        outgoing = effective.get(clip_index + 1, 0)
        if clip_index > first and clip_index in effective:
            record = honoured[clip_index]
            segment = _cached_transition_segment(
                store,
                scene_clips[clip_index - first - 1],
                clip,
                record["transition"],
                effective[clip_index],
                workdir / f"transition_{clip_index:04d}.mp4",
                clip_frames[clip_index - 1],
                clip_frames[clip_index],
            )
            if segment is None or int(segment["window_frames"]) != int(
                effective[clip_index]
            ):
                raise _ChapterPathFallback(
                    f"transition_build_failed:index={clip_index}:"
                    f"kind={record['transition']}"
                )
            applied_records.append(_applied_record(record, segment))
            segment_paths.append(segment["path"])
        main_frames = int(clip_frames[clip_index]) - incoming - outgoing
        if main_frames < 0:
            raise _ChapterPathFallback(
                f"negative_main_part:clip={clip_index}"
            )
        if main_frames == 0:
            continue
        if incoming == 0 and outgoing == 0:
            segment_paths.append(clip)
            continue
        main_path = _cached_trim(
            store, clip, incoming, main_frames,
            workdir / f"main_{clip_index:04d}.mp4",
        )
        if main_path is None:
            raise _ChapterPathFallback(
                f"main_trim_failed:clip={clip_index}"
            )
        segment_paths.append(main_path)
    return segment_paths, applied_records


def _join_chapter_body(segment_paths, chapter_path, expected_frames):
    """Join a chapter's segments into its body file.

    Stream-copy first: every segment shares the canonical encode
    parameters and starts on a keyframe (trims and transition
    segments are fresh encodes; item clips are single encodes), so
    a copy join is frame-exact — and it is only believed after an
    exact frame count plus resolution/fps probes pass. Any doubt
    falls back to the repair-filter re-encode the monolithic
    assembly uses (``concatenate_clips``)."""
    chapter_path = Path(chapter_path)
    chapter_path.parent.mkdir(parents=True, exist_ok=True)
    if not segment_paths:
        return False
    if len(segment_paths) == 1:
        partial = _partial_path(chapter_path)
        try:
            shutil.copyfile(segment_paths[0], partial)
        except OSError:
            _discard_partial(partial)
            return False
        if _probe_frame_count_exactish(partial) == expected_frames:
            try:
                _publish_partial(partial, chapter_path)
                return True
            except OSError:
                _discard_partial(partial)
                return False
        _discard_partial(partial)
        return False
    partial = _partial_path(chapter_path)
    write_concat_file(segment_paths, partial)
    command = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(partial.parent / "concat.txt"),
        "-c", "copy",
        "-movflags", "+faststart",
        str(partial),
    ]
    if (
        run_ffmpeg(command, timeout=max(FFMPEG_TIMEOUT, 900))
        and valid_media(partial)
        and _probe_frame_count_exactish(partial) == expected_frames
    ):
        info = probe_media(partial)
        if (
            info
            and info.get("width") == WIDTH
            and info.get("height") == HEIGHT
            and info.get("avg_fps") is not None
            and abs(float(info["avg_fps"]) - FPS) <= 0.01
        ):
            try:
                _publish_partial(partial, chapter_path)
                return True
            except OSError:
                _discard_partial(partial)
                return False
    _discard_partial(partial)
    try:
        return bool(concatenate_clips(
            segment_paths, chapter_path, expected_frames=expected_frames
        ))
    except RuntimeError:
        return False


def _chapter_body_frames(plan, clip_frames, effective):
    """The exact frame count of a chapter's body: its clips' mains
    (edge clips trimmed by the boundary windows) plus its internal
    transition segments. Mirrors ``_build_chapter_segments``'
    arithmetic by construction."""
    first = plan["first_clip_index"]
    last = plan["last_clip_index"]
    total = 0
    for clip_index in range(first, last + 1):
        total += (
            int(clip_frames[clip_index])
            - effective.get(clip_index, 0)
            - effective.get(clip_index + 1, 0)
        )
        if clip_index > first and clip_index in effective:
            total += 2 * effective[clip_index]
    return total


def _render_timeline_chaptered(scenes, project_dir, expected_duration=None):
    """Phase 5 chapter renderer.

    The film is rendered chapter by chapter — a chapter is one
    scene, the production's own structural unit. Each chapter's
    body (its clips joined with its internal transitions, edge
    clips trimmed by the boundary windows) is produced once and
    cached under a deterministic key (see services/render_cache);
    a repeat render, a Phase 1 resume, or a regeneration with an
    unchanged scene reuses the chapter without re-rendering it.
    Boundary transition segments sit between chapter bodies in the
    assembly, which is published through the existing atomic
    concat path and audited by the same final master audit as the
    monolithic path. Any inconsistency raises _ChapterPathFallback
    and the monolithic path decides the render."""
    from services import transitions as _transitions

    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    store = render_cache.CacheStore(project_dir)
    store.prune()

    print()
    print("=" * 70)
    print("DOCUMENTARY FRAME-LOCKED RENDERER (CHAPTERED)")
    print("=" * 70)
    print(f"Scenes: {len(scenes)}")
    print(f"FPS: {FPS}")
    print(f"Resolution: {WIDTH}x{HEIGHT}")

    supplied_frames = None
    if expected_duration is not None:
        supplied_frames = seconds_to_frame(float(expected_duration))

    # Planning pass (pure): frame allocations threaded exactly as
    # the monolithic scene loop threads them, so planned frames
    # are the frames render_scene will enforce per clip.
    scene_plans = []
    running_frame = 0
    for scene_index, scene in enumerate(scenes):
        is_last_scene = scene_index == len(scenes) - 1
        scene_final = None
        if is_last_scene and supplied_frames is not None:
            scene_final = supplied_frames
        next_start = None
        if not is_last_scene:
            next_start = scene_first_start_frame(scenes[scene_index + 1])
        items = scene_items(scene)
        if items:
            allocations, scene_end = allocate_item_frames(
                items,
                running_frame,
                final_frame=scene_final,
                next_start_frame=next_start,
            )
        else:
            allocations, scene_end = [], running_frame
        scene_plans.append({
            "index": scene_index,
            "scene": scene,
            "items": items,
            "allocations": allocations,
            "start_frame": running_frame,
            "end_frame": scene_end,
            "scene_final": scene_final,
            "next_start": next_start,
            "first_clip_index": None,
            "last_clip_index": None,
        })
        running_frame = scene_end

    expected_total_frames = running_frame
    expected_total_duration = exact_duration_from_frames(expected_total_frames)

    if supplied_frames is not None:
        supplied_expected = float(expected_duration)
        supplied_frame_delta = expected_total_frames - supplied_frames

        print()
        print("=" * 70)
        print("TIMELINE FRAME AUDIT")
        print("=" * 70)
        print(f"Timeline frames: {expected_total_frames}")
        print(f"Expected frames: {supplied_frames}")
        print(f"Frame delta: {supplied_frame_delta}")
        print(f"Timeline duration: {expected_total_duration:.6f}s")
        print(f"Expected duration: {supplied_expected:.6f}s")

        if abs(supplied_frame_delta) > 1:
            raise RuntimeError(
                "TIMELINE/NARRATION FRAME MISMATCH: "
                f"timeline={expected_total_frames} frames "
                f"({expected_total_duration:.6f}s), "
                f"expected={supplied_frames} frames "
                f"({supplied_expected:.6f}s), "
                f"delta={supplied_frame_delta} frames. "
                "Renderer stopped before concat because the "
                "timeline itself does not match narration."
            )

        expected_total_frames = supplied_frames
        expected_total_duration = exact_duration_from_frames(expected_total_frames)

    flat_items = flatten_timeline_items(scenes)
    clip_frames = []
    clip_cursor = 0
    for plan in scene_plans:
        if plan["items"]:
            plan["first_clip_index"] = clip_cursor
            for allocation in plan["allocations"]:
                clip_frames.append(allocation["frame_count"])
            clip_cursor += len(plan["items"])
            plan["last_clip_index"] = clip_cursor - 1

    # One transition plan + window computation for the whole film
    # (shared verbatim with the monolithic join).
    plan_records = _transitions.plan_transitions(flat_items)
    if any(record.get("honoured") for record in plan_records):
        effective, honoured, transitions_info = _plan_transition_windows(
            plan_records, clip_frames
        )
    else:
        effective, honoured = {}, {}
        transitions_info = {
            "status": "all_cut",
            "applied": [],
            "downgraded": [
                {
                    "index": record["index"],
                    "requested_transition": record.get("requested_transition"),
                    "transition_reason": record.get("transition_reason"),
                    "downgrade_cause": record.get("downgrade_cause"),
                }
                for record in plan_records
                if record.get("downgraded")
            ],
            "records": plan_records,
            "fallback_reason": None,
        }

    narration_hash = render_cache.hash_file(project_dir / "narration.wav")
    settings = _render_settings_fingerprint()
    chapters_dir = project_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    applied = {}
    scene_audits = []
    chapter_records = []
    resolved = []

    for plan in scene_plans:
        scene = plan["scene"]
        items = plan["items"]
        if not items:
            _clips, _end, scene_audit = render_scene(
                scene,
                str(project_dir),
                plan["start_frame"],
                final_frame=plan["scene_final"],
                next_start_frame=plan["next_start"],
                cache_store=store,
            )
            scene_audits.append(scene_audit)
            resolved.append({
                "plan": plan, "audit": scene_audit,
                "chapter_path": None, "has_clips": False,
                "first_clip_sha256": None, "last_clip_sha256": None,
            })
            chapter_records.append({
                "position": plan["index"],
                "scene": scene.get("scene_number"),
                "status": "empty",
                "frames": 0,
            })
            continue

        first_idx = plan["first_clip_index"]
        last_idx = plan["last_clip_index"]
        body_frames = _chapter_body_frames(plan, clip_frames, effective)
        chapter_path = chapters_dir / f"chapter_{plan['index'] + 1:03d}.mp4"
        slice_payload = {
            "scene_number": scene.get("scene_number"),
            "position": plan["index"],
            "items": [
                render_cache.item_fingerprint(item) for item in items
            ],
            "allocations": [
                [a["start_frame"], a["end_frame"]]
                for a in plan["allocations"]
            ],
            "edge_windows": {
                "incoming": effective.get(first_idx, 0),
                "outgoing": effective.get(last_idx + 1, 0),
            },
            "internal_transitions": [
                {
                    "index": i,
                    "kind": honoured[i]["transition"],
                    "window": effective[i],
                }
                for i in range(first_idx + 1, last_idx + 1)
                if i in effective
            ],
            "settings": settings,
            "narration_sha256": narration_hash,
        }
        key = render_cache.chapter_key(slice_payload)

        meta = None
        if body_frames > 0 and store.lookup("chapter", key) is not None:
            candidate = store.metadata("chapter", key)
            if (
                candidate is not None
                and int(candidate.get("frames") or -1) == body_frames
                and isinstance(candidate.get("scene_audit"), dict)
            ):
                meta = candidate
        if meta is not None and store.place("chapter", key, chapter_path):
            if _probe_frame_count_exactish(chapter_path) == body_frames:
                store.stats["chapter_hits"] += 1
                scene_audit = meta["scene_audit"]
                scene_audits.append(scene_audit)
                for record in meta.get("applied") or []:
                    applied[int(record["index"])] = record
                resolved.append({
                    "plan": plan, "audit": scene_audit,
                    "chapter_path": str(chapter_path), "has_clips": True,
                    "first_clip_sha256": meta.get("first_clip_sha256"),
                    "last_clip_sha256": meta.get("last_clip_sha256"),
                })
                chapter_records.append({
                    "position": plan["index"],
                    "scene": scene.get("scene_number"),
                    "status": "reused",
                    "frames": body_frames,
                    "key": key[:16],
                })
                print(
                    f"CHAPTER CACHE HIT: scene {scene.get('scene_number')} "
                    f"({body_frames} frames reused)"
                )
                continue
        store.stats["chapter_misses"] += 1

        scene_clips, scene_end, scene_audit = render_scene(
            scene,
            str(project_dir),
            plan["start_frame"],
            final_frame=plan["scene_final"],
            next_start_frame=plan["next_start"],
            cache_store=store,
        )
        if scene_end != plan["end_frame"]:
            raise _ChapterPathFallback(
                f"scene_frame_drift:scene={plan['index']}"
            )
        scene_audits.append(scene_audit)
        first_hash = render_cache.hash_file(scene_clips[0])
        last_hash = render_cache.hash_file(scene_clips[-1])

        if body_frames == 0:
            # A lone clip consumed entirely by its two boundary
            # transitions: the chapter contributes no body (the
            # monolithic join likewise emits only the segments).
            resolved.append({
                "plan": plan, "audit": scene_audit,
                "chapter_path": None, "has_clips": True,
                "first_clip_sha256": first_hash,
                "last_clip_sha256": last_hash,
            })
            chapter_records.append({
                "position": plan["index"],
                "scene": scene.get("scene_number"),
                "status": "absorbed",
                "frames": 0,
                "key": key[:16],
            })
            continue

        segment_paths, applied_records = _build_chapter_segments(
            store, plan, scene_clips, clip_frames,
            effective, honoured, project_dir,
        )
        for record in applied_records:
            applied[int(record["index"])] = record
        if not _join_chapter_body(segment_paths, chapter_path, body_frames):
            raise _ChapterPathFallback(
                f"chapter_join_failed:scene={plan['index']}"
            )
        if _scene_render_is_clean(scene_audit):
            store.put("chapter", key, chapter_path, metadata={
                "scene_number": scene.get("scene_number"),
                "position": plan["index"],
                "frames": body_frames,
                "scene_audit": scene_audit,
                "first_clip_sha256": first_hash,
                "last_clip_sha256": last_hash,
                "applied": applied_records,
            })
        resolved.append({
            "plan": plan, "audit": scene_audit,
            "chapter_path": str(chapter_path), "has_clips": True,
            "first_clip_sha256": first_hash,
            "last_clip_sha256": last_hash,
        })
        chapter_records.append({
            "position": plan["index"],
            "scene": scene.get("scene_number"),
            "status": "rendered",
            "frames": body_frames,
            "key": key[:16],
        })

    # Assembly: chapter bodies interleaved with the boundary
    # transition segments, in flat clip order — the same frame
    # sequence the monolithic join produces.
    assembly = []
    previous = None
    for entry in resolved:
        if not entry["has_clips"]:
            continue
        plan = entry["plan"]
        first_idx = plan["first_clip_index"]
        if previous is not None and first_idx in effective:
            record = honoured[first_idx]
            clip_a = previous["audit"]["items"][-1]["file"]
            clip_b = entry["audit"]["items"][0]["file"]
            segment = _cached_transition_segment(
                store,
                clip_a,
                clip_b,
                record["transition"],
                effective[first_idx],
                project_dir / "transitions" / f"transition_{first_idx:04d}.mp4",
                clip_frames[first_idx - 1],
                clip_frames[first_idx],
                expected_hashes=(
                    previous["last_clip_sha256"],
                    entry["first_clip_sha256"],
                ),
            )
            if segment is None or int(segment["window_frames"]) != int(
                effective[first_idx]
            ):
                raise _ChapterPathFallback(
                    f"boundary_segment_failed:index={first_idx}"
                )
            applied[first_idx] = _applied_record(record, segment)
            assembly.append(segment["path"])
        if entry["chapter_path"] is not None:
            assembly.append(entry["chapter_path"])
        previous = entry

    transitions_info["applied"] = [
        applied[index] for index in sorted(applied)
    ]

    audit_file = project_dir / "renderer_timing_audit.json"
    audit_payload = {
        "fps": FPS,
        "width": WIDTH,
        "height": HEIGHT,
        "expected_frames": expected_total_frames,
        "expected_duration": expected_total_duration,
        "scene_count": len(scene_audits),
        "clip_count": len(flat_items),
        "scenes": scene_audits,
        "status": "pre_concat_pass",
        "placeholder_count": sum(
            int(audit.get("placeholder_count") or 0) for audit in scene_audits
        ),
        "holdover_count": sum(
            int(audit.get("holdover_count") or 0) for audit in scene_audits
        ),
        # F9 (Wave 1): every applied and downgraded transition.
        "transitions": transitions_info,
        # Phase 5: per-chapter outcomes + cache behaviour.
        "chapters": chapter_records,
        "render_cache": dict(store.stats),
    }
    write_json_atomic(audit_file, audit_payload, ensure_ascii=True)

    print()
    print(f"Renderer timing audit: {audit_file}")

    # Assembly publishes through the existing atomic concat path:
    # the master is replaced only by a validated encode (Phase 1).
    raw_video = project_dir / "documentary.mp4"
    try:
        success = concatenate_clips(
            assembly,
            raw_video,
            expected_frames=expected_total_frames,
        )
    except RuntimeError as error:
        raise _ChapterPathFallback(f"assembly_concat_raised: {error}")
    if not success:
        raise _ChapterPathFallback("assembly_concat_failed")

    try:
        return _finalise_master_render(
            raw_video,
            expected_total_frames,
            expected_total_duration,
            audit_payload,
            audit_file,
            clip_count=len(flat_items),
        )
    except RuntimeError as error:
        raise _ChapterPathFallback(f"assembly_audit_failed: {error}")


def _render_timeline_legacy(scenes, project_dir, expected_duration=None):
    """The pre-Phase-5 monolithic render: every scene rendered in
    one pass, the flat join sequence assembled, one concat encode.
    Kept verbatim as the chapter renderer's fallback — any
    chapter-path inconsistency lands here, and this path's own
    fallbacks (transition join → plain concat) apply as before.
    Clip encodes still go through the Phase 5 clip cache, so a
    fallback re-render does not redo completed clips."""
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    store = render_cache.CacheStore(project_dir)

    all_clips = []
    scene_audits = []

    print()
    print("=" * 70)
    print("DOCUMENTARY FRAME-LOCKED RENDERER")
    print("=" * 70)
    print(f"Scenes: {len(scenes)}")
    print(f"FPS: {FPS}")
    print(f"Resolution: {WIDTH}x{HEIGHT}")

    supplied_frames = None
    if expected_duration is not None:
        supplied_frames = seconds_to_frame(float(expected_duration))

    running_frame = 0
    for scene_index, scene in enumerate(scenes):
        is_last_scene = scene_index == len(scenes) - 1

        scene_final = None
        if is_last_scene and supplied_frames is not None:
            scene_final = supplied_frames

        # A scene ends where the next scene begins, so gaps between scenes
        # are absorbed instead of silently dropped.
        next_start = None
        if not is_last_scene:
            next_start = scene_first_start_frame(scenes[scene_index + 1])

        scene_clips, running_frame, scene_audit = render_scene(
            scene,
            str(project_dir),
            running_frame,
            final_frame=scene_final,
            next_start_frame=next_start,
            cache_store=store,
        )
        all_clips.extend(scene_clips)
        scene_audits.append(scene_audit)

    expected_total_frames = running_frame
    expected_total_duration = exact_duration_from_frames(expected_total_frames)

    if supplied_frames is not None:
        supplied_expected = float(expected_duration)
        supplied_frame_delta = expected_total_frames - supplied_frames

        print()
        print("=" * 70)
        print("TIMELINE FRAME AUDIT")
        print("=" * 70)
        print(f"Timeline frames: {expected_total_frames}")
        print(f"Expected frames: {supplied_frames}")
        print(f"Frame delta: {supplied_frame_delta}")
        print(f"Timeline duration: {expected_total_duration:.6f}s")
        print(f"Expected duration: {supplied_expected:.6f}s")

        if abs(supplied_frame_delta) > 1:
            raise RuntimeError(
                "TIMELINE/NARRATION FRAME MISMATCH: "
                f"timeline={expected_total_frames} frames "
                f"({expected_total_duration:.6f}s), "
                f"expected={supplied_frames} frames "
                f"({supplied_expected:.6f}s), "
                f"delta={supplied_frame_delta} frames. "
                "Renderer stopped before concat because the "
                "timeline itself does not match narration."
            )

        expected_total_frames = supplied_frames
        expected_total_duration = exact_duration_from_frames(expected_total_frames)

    # F9 (Wave 1): transitions. render_scene returns exactly one
    # clip per timeline item (its audit raises otherwise, and
    # holdovers still produce one clip for their item), so the
    # flattened items pair 1:1 with all_clips. If that alignment
    # cannot be verified here, the untouched concat path is used
    # and the reason recorded.
    join_clips = all_clips
    flat_items = flatten_timeline_items(scenes)
    clip_frames = [
        audit_item.get("actual_frames")
        for audit in scene_audits
        for audit_item in audit.get("items", [])
    ]
    if len(flat_items) != len(all_clips) or len(clip_frames) != len(all_clips) \
            or any(frames is None for frames in clip_frames):
        transitions_info = {
            "status": "fallback_concat",
            "applied": [],
            "downgraded": [],
            "records": [],
            "fallback_reason": (
                "clip_item_alignment_unverified:"
                f"items={len(flat_items)}:clips={len(all_clips)}:"
                f"audited_clip_frames={len(clip_frames)}"
            ),
        }
    else:
        from services import transitions as _transitions

        _plan = _transitions.plan_transitions(flat_items)
        if any(record.get("honoured") for record in _plan):
            _built, transitions_info = _build_transition_join(
                all_clips, clip_frames, _plan, project_dir
            )
            if _built is not None:
                join_clips = _built
        else:
            transitions_info = {
                "status": "all_cut",
                "applied": [],
                "downgraded": [
                    {
                        "index": record["index"],
                        "requested_transition": record.get("requested_transition"),
                        "transition_reason": record.get("transition_reason"),
                        "downgrade_cause": record.get("downgrade_cause"),
                    }
                    for record in _plan
                    if record.get("downgraded")
                ],
                "records": _plan,
                "fallback_reason": None,
            }

    audit_file = project_dir / "renderer_timing_audit.json"
    audit_payload = {
        "fps": FPS,
        "width": WIDTH,
        "height": HEIGHT,
        "expected_frames": expected_total_frames,
        "expected_duration": expected_total_duration,
        "scene_count": len(scene_audits),
        "clip_count": len(all_clips),
        "scenes": scene_audits,
        "status": "pre_concat_pass",
        # Placeholder substitutions no longer exist (always 0);
        # holdovers of genuine media are totalled alongside.
        "placeholder_count": sum(
            int(audit.get("placeholder_count") or 0) for audit in scene_audits
        ),
        "holdover_count": sum(
            int(audit.get("holdover_count") or 0) for audit in scene_audits
        ),
        # F9 (Wave 1): every applied and downgraded transition.
        "transitions": transitions_info,
    }
    # Atomic (Stage 4): the timing audit is read back by the
    # production report — never observable half-written. Original
    # serialisation preserved (indent=2, ensure_ascii default).
    write_json_atomic(audit_file, audit_payload, ensure_ascii=True)

    print()
    print(f"Renderer timing audit: {audit_file}")

    # Phase 1 durability: the existing master is NEVER deleted up
    # front. concatenate_clips publishes its validated replacement
    # over it with os.replace(), so a kill during the (long)
    # re-encode leaves the previous master in place.
    raw_video = project_dir / "documentary.mp4"

    success = concatenate_clips(
        join_clips,
        raw_video,
        expected_frames=expected_total_frames,
    )
    if not success:
        raise RuntimeError("Could not create documentary.mp4")

    return _finalise_master_render(
        raw_video,
        expected_total_frames,
        expected_total_duration,
        audit_payload,
        audit_file,
        clip_count=len(all_clips),
    )


def _finalise_master_render(raw_video, expected_total_frames,
                            expected_total_duration, audit_payload,
                            audit_file, clip_count):
    """The final master audit shared by the monolithic and chapter
    render paths (Phase 5): probe the published master, enforce the
    exact frame count / resolution / fps, complete the timing
    audit, and return the master path. Any mismatch raises — a
    technically broken master is a TECHNICAL failure (Phase 4) on
    every path."""
    final_info = probe_media(raw_video)
    if not final_info:
        raise RuntimeError("Final documentary could not be probed.")

    actual_frames = final_info.get("frame_count")
    actual_duration = final_info.get("duration")
    frame_delta = None if actual_frames is None else actual_frames - expected_total_frames
    duration_delta = None if actual_duration is None else actual_duration - expected_total_duration

    print()
    print("=" * 70)
    print("FINAL RENDER FRAME AUDIT")
    print("=" * 70)
    print(f"Expected frames: {expected_total_frames}")
    print(f"Actual frames:   {actual_frames}")
    print(f"Frame delta:     {frame_delta}")
    print(f"Expected duration: {expected_total_duration:.6f}s")
    print(f"Actual duration:   {float(actual_duration or 0):.6f}s")
    print(f"Duration delta:    {float(duration_delta or 0):.6f}s")
    print(f"FPS: {final_info.get('avg_fps')}")
    print(f"Resolution: {final_info.get('width')}x{final_info.get('height')}")

    if actual_frames != expected_total_frames:
        raise RuntimeError(
            "FINAL FRAME COUNT MISMATCH: "
            f"expected {expected_total_frames}, got {actual_frames}. "
            "The renderer refuses to pass an incorrectly timed "
            "visual master downstream."
        )

    if final_info.get("width") != WIDTH or final_info.get("height") != HEIGHT:
        raise RuntimeError(
            "FINAL RESOLUTION MISMATCH: "
            f"expected {WIDTH}x{HEIGHT}, "
            f"got {final_info.get('width')}x{final_info.get('height')}."
        )

    if final_info.get("avg_fps") is None or abs(float(final_info["avg_fps"]) - FPS) > 0.01:
        raise RuntimeError(
            f"FINAL FPS MISMATCH: expected {FPS} FPS, got {final_info.get('avg_fps')}."
        )

    audit_payload.update({
        "actual_frames": actual_frames,
        "actual_duration": actual_duration,
        "frame_delta": frame_delta,
        "duration_delta": duration_delta,
        "final_fps": final_info.get("avg_fps"),
        "final_width": final_info.get("width"),
        "final_height": final_info.get("height"),
        "status": "pass",
    })
    write_json_atomic(audit_file, audit_payload, ensure_ascii=True)

    print()
    print("=" * 70)
    print("DOCUMENTARY RENDER COMPLETE")
    print("=" * 70)
    print(f"Clips: {clip_count}")
    print(f"Frames: {actual_frames}")
    print(f"Duration: {float(actual_duration):.6f}s")
    print(f"Output: {raw_video}")
    print("FRAME-LOCK STATUS: PASS")
    return str(raw_video)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Frame-locked documentary renderer")
    parser.add_argument("timeline", help="Path to timeline.json")
    parser.add_argument("--output", default=None, help="Optional output path")
    args = parser.parse_args()

    timeline_file = Path(args.timeline)
    with open(timeline_file, "r", encoding="utf-8-sig") as file:
        timeline = json.load(file)

    scenes = timeline.get("scenes", [])
    output = Path(args.output) if args.output else timeline_file.parent / "documentary.mp4"
    expected_duration = (
        timeline.get("narration_duration_seconds")
        or timeline.get("duration_seconds")
    )

    result = render_timeline(
        scenes,
        timeline_file.parent,
        expected_duration=expected_duration,
    )
    print()
    print(json.dumps({"status": "rendered", "output_file": result}, indent=2))