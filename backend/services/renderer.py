import json
import subprocess
from pathlib import Path

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
        str(output_path),
    ]

    success = run_ffmpeg(command)
    if not success or not valid_media(output_path):
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

    video_filter = build_video_filter()
    if retime_factor:
        # Run-9 fix 4: compress the source's presentation timestamps
        # so the whole sequence fits the slot instead of being
        # trimmed at the slot boundary.
        video_filter = f"setpts=PTS*{float(retime_factor):.6f}," + video_filter

    command = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",
    ]
    # Stage 3F (MA-15): input seeking — "-ss" BEFORE "-i" starts the
    # (looped) source at the asset's verified segment offset instead
    # of always at t=0. With start_offset=0 the command is identical
    # to the historical one.
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
        str(output_path),
    ])

    success = run_ffmpeg(command)
    if not success or not valid_media(output_path):
        print("Video clip was not created.")
        return False

    print(
        f"VIDEO CLIP COMPLETE: {output_path} "
        f"frames={frame_count} "
        f"duration={exact_duration_from_frames(frame_count):.3f}s"
    )
    return True


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


def render_scene(scene, project_dir, starting_frame=0, final_frame=None, next_start_frame=None):
    scene_number = scene.get("scene_number", 1)
    items = scene_items(scene)

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
                success = create_video_clip(
                    media_path,
                    duration,
                    output_path,
                    frame_count=frame_count,
                    start_offset=item_clip_start,
                    retime_factor=_retime,
                )
            else:
                success = create_image_clip(
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
                success = create_video_clip(
                    holdover_path,
                    duration,
                    output_path,
                    frame_count=frame_count,
                    start_offset=item_clip_start,
                )
            else:
                success = create_image_clip(
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
            _comp_ok, _comp_record = (
                _composition_module.apply_composition(
                    output_path,
                    item["composition"],
                    _composed_path,
                    clips_dir / f"composition_{index + 1:04d}",
                )
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

    repaired_output = output_path.parent / "documentary_repaired.mp4"
    if repaired_output.exists():
        try:
            repaired_output.unlink()
        except OSError:
            pass

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
            raise RuntimeError(
                "RENDERER FRAME REPAIR FAILED: "
                f"expected {expected_frames} frames, got {repaired_frames}."
            )

    try:
        if output_path.exists():
            output_path.unlink()
        repaired_output.replace(output_path)
    except OSError as error:
        raise RuntimeError(f"Could not finalize documentary: {error}")

    print("CONCAT COMPLETE.")
    return True


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
        return None, info

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
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)

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

    raw_video = project_dir / "documentary.mp4"
    if raw_video.exists():
        try:
            raw_video.unlink()
        except OSError:
            pass

    success = concatenate_clips(
        join_clips,
        raw_video,
        expected_frames=expected_total_frames,
    )
    if not success:
        raise RuntimeError("Could not create documentary.mp4")

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
    print(f"Clips: {len(all_clips)}")
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