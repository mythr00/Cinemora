"""F9 transitions basics (Wave 1) — frame-exact edit-point transitions.

Design constraint (the reason this module exists in this shape): the
renderer is frame-locked to the narration clock. A conventional
``xfade`` overlaps two clips and *shortens* the film by the overlap,
which would break the renderer's strict total-frame audit. Every
transition here therefore PRESERVES the total frame count exactly:

* a transition into clip B consumes the last W frames of clip A and
  the first W frames of clip B, and outputs exactly 2W frames;
* the caller replaces those 2W source frames with the 2W segment
  frames, so the joined film's frame count is unchanged.

The 2W window is built with freeze handles: A is extended by cloning
its last frame for W frames, B by prepending a clone of its first
frame for W frames, and the two extended layers are combined across
the whole window (cross-blend / fade-through-black / sliding
overlay / burn overlay). At the window's first frame the picture is
pure A, at its last frame pure B.

Transition choice is a *recorded* timeline field, never an effect
applied on a whim (plan §D.5): a non-cut transition is honoured only
when the timeline item names a valid type AND records a valid
reason; anything else is downgraded to ``cut`` with the cause
recorded.

Wave 2 (F9 completion) adds three segment kinds — ``split_screen``
(the incoming clip slides in over the outgoing one behind a hard
divider), ``parallax_depth`` (the outgoing clip drifts slower and
dims behind the incoming one — a depth cue, not a blend) and
``film_strip`` (a sprocketed strip band rides the wipe seam) — all
built on the same freeze-handle 2W contract, so the frame audit is
untouched.

``match_cut`` is NOT a segment kind: it is a recorded edit RULE
(plan F9: "a Director/editor rule — planning logic, zero
rendering"). An item requests it with ``transition: "match_cut"``
and ``transition_reason: "continuity"``, and BOTH adjacent items
must carry a matching continuity record —
``match_cut: {"basis": "subject"|"shape"|"place"|"object",
"key": <recorded continuity key>}`` — with equal basis and key.
When the rule applies, the edit stays a plain cut (zero frames
consumed, nothing rendered) and the plan record carries the
``match_cut`` evidence; when the continuity record is absent or
mismatched, the request is downgraded to a plain cut with the
cause recorded. Nothing here detects continuity — the rule only
honours continuity that was recorded upstream.
"""

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

FPS = 30
WIDTH = 1920
HEIGHT = 1080

VIDEO_CODEC = "libx264"
PRESET = "ultrafast"
CRF = "23"

VALID_TRANSITIONS = {
    "cut", "fade", "dissolve", "whip_pan", "film_burn",
    # Wave 2 (F9 completion).
    "split_screen", "parallax_depth", "film_strip",
    # A recorded rule, not a segment kind (see module docstring).
    "match_cut",
}
VALID_REASONS = {"scene_boundary", "time_passage", "continuity"}

#: Bases a match-cut continuity record may claim.
MATCH_CUT_BASES = {"subject", "shape", "place", "object"}

# Default transition windows, in frames at the project FPS.
DEFAULT_WINDOW_FRAMES = {
    "fade": 12,
    "dissolve": 12,
    "film_burn": 12,
    "whip_pan": 8,
    "split_screen": 12,
    "parallax_depth": 12,
    "film_strip": 16,
}

#: Kinds that consume a 2W segment. match_cut is deliberately
#: absent: it is a cut with a rule record, never a segment.
NON_CUT_TRANSITIONS = VALID_TRANSITIONS - {"cut", "match_cut"}


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

def _default_window(kind):
    return DEFAULT_WINDOW_FRAMES.get(kind, 12)


def _match_cut_evidence(previous_item, item):
    """The continuity evidence for a match cut between two
    adjacent items, or None. Both items must carry a ``match_cut``
    record with a known basis and the SAME non-empty key — the
    rule honours recorded continuity only; it detects nothing."""
    def _record(candidate):
        if not isinstance(candidate, dict):
            return None
        record = candidate.get("match_cut")
        if not isinstance(record, dict):
            return None
        basis = str(record.get("basis") or "").strip().lower()
        key = str(record.get("key") or "").strip()
        if basis not in MATCH_CUT_BASES or not key:
            return None
        return {"basis": basis, "key": key}

    before = _record(previous_item)
    after = _record(item)
    if before is None or after is None:
        return None
    if before["basis"] != after["basis"] or before["key"] != after["key"]:
        return None
    return before


def _requested_window(item, kind):
    """An item may record an explicit window; otherwise the default."""
    for key in ("transition_window_frames", "transition_frames"):
        value = item.get(key)
        if value is not None:
            try:
                window = int(value)
            except (TypeError, ValueError):
                continue
            if window > 0:
                return window
    return _default_window(kind)


def plan_transitions(items):
    """Plan the transition INTO each item (item 0 never has one).

    Returns one record per edit point (items[1:]). A non-cut
    transition is honoured only when the item names a valid type
    AND a valid reason; otherwise it is downgraded to ``cut`` and
    the downgrade + cause are recorded on the record.
    """
    records = []
    sequence = list(items or [])
    for index, item in enumerate(sequence):
        if index == 0:
            continue
        if not isinstance(item, dict):
            item = {}
        requested = item.get("transition")
        requested = str(requested).strip().lower() if requested else "cut"
        reason = item.get("transition_reason")
        reason = str(reason).strip().lower() if reason else None

        record = {
            "index": index,
            "item_index": index,
            "requested_transition": requested,
            "transition_reason": reason,
            "transition": "cut",
            "honoured": False,
            "downgraded": False,
            "downgrade_cause": None,
            "window_frames": 0,
            # Wave 2: the match-cut rule's evidence, when it
            # applied at this edit point (else None).
            "match_cut": None,
        }

        if requested == "cut":
            records.append(record)
            continue

        if requested == "match_cut":
            # The match-cut RULE (zero rendering): the edit stays
            # a cut; the rule only records whether recorded
            # continuity justifies calling it one.
            if reason not in VALID_REASONS:
                record["downgraded"] = True
                record["downgrade_cause"] = (
                    "missing_or_invalid_transition_reason")
                records.append(record)
                continue
            previous = sequence[index - 1]
            if not isinstance(previous, dict):
                previous = {}
            evidence = _match_cut_evidence(previous, item)
            if reason != "continuity" or evidence is None:
                record["downgraded"] = True
                record["downgrade_cause"] = (
                    "match_cut_continuity_unrecorded")
                records.append(record)
                continue
            record["match_cut"] = evidence
            records.append(record)
            continue

        if requested not in VALID_TRANSITIONS:
            record["downgraded"] = True
            record["downgrade_cause"] = "unknown_transition"
            records.append(record)
            continue

        if reason not in VALID_REASONS:
            record["downgraded"] = True
            record["downgrade_cause"] = "missing_or_invalid_transition_reason"
            records.append(record)
            continue

        record["transition"] = requested
        record["honoured"] = True
        record["window_frames"] = _requested_window(item, requested)
        records.append(record)

    return records


# ---------------------------------------------------------------------------
# Probing / exact trims
# ---------------------------------------------------------------------------

def probe_frame_count(path):
    """Exact-ish frame count for a clip: container nb_frames, with a
    counted-frames fallback. Returns an int or None."""
    path = Path(path)
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=nb_frames",
                "-of", "csv=p=0",
                str(path),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=30,
        )
        if result.returncode == 0:
            value = (result.stdout or "").strip()
            if value and value != "N/A":
                return int(float(value))
    except Exception:
        pass
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-count_frames",
                "-show_entries", "stream=nb_read_frames",
                "-of", "csv=p=0",
                str(path),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=120,
        )
        if result.returncode == 0:
            value = (result.stdout or "").strip()
            if value and value != "N/A":
                return int(float(value))
    except Exception:
        pass
    return None


def _run_ffmpeg(command, timeout=300):
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout,
        )
        if result.returncode != 0:
            print("TRANSITION FFMPEG FAILED:")
            print((result.stderr or "")[-4000:])
            return False
        return True
    except Exception as error:  # noqa: BLE001 - reported, caller falls back
        print(f"TRANSITION FFMPEG ERROR: {error}")
        return False


def _encode_command(filter_complex, out_path, frame_count, input_args):
    command = ["ffmpeg", "-y"]
    command.extend(input_args)
    command.extend([
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-frames:v", str(int(frame_count)),
        "-fps_mode", "cfr",
        "-r", str(FPS),
        "-c:v", VIDEO_CODEC,
        "-preset", PRESET,
        "-crf", CRF,
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        str(out_path),
    ])
    return command


def trim_clip_frames(clip_path, start_frame, frame_count, out_path):
    """Exact frame-range trim: frames [start_frame, start_frame+count).

    Returns the output path (str) on success, else None."""
    frame_count = int(frame_count)
    start_frame = int(start_frame)
    if frame_count <= 0:
        return None
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    video_filter = (
        f"trim=start_frame={start_frame}:end_frame={start_frame + frame_count},"
        "setpts=PTS-STARTPTS,"
        f"fps={FPS},format=yuv420p"
    )
    command = [
        "ffmpeg", "-y",
        "-i", str(clip_path),
        "-vf", video_filter,
        "-frames:v", str(frame_count),
        "-fps_mode", "cfr",
        "-c:v", VIDEO_CODEC,
        "-preset", PRESET,
        "-crf", CRF,
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        str(out_path),
    ]
    if not _run_ffmpeg(command):
        return None
    if probe_frame_count(out_path) != frame_count:
        return None
    return str(out_path)


def split_for_transition(clip_path, window_frames, workdir, stem="clip"):
    """Split a clip around a transition window of ``window_frames``.

    Returns a dict with the clip's total frame count, the clamped
    window, and exact-trim paths for the head (all but the last W
    frames — None when that is empty) and the tail (the last W
    frames)."""
    total = probe_frame_count(clip_path)
    if total is None:
        return None
    window = max(0, min(int(window_frames), int(total)))
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    head_path = None
    head_frames = int(total) - window
    if head_frames > 0:
        head_path = trim_clip_frames(
            clip_path, 0, head_frames, workdir / f"{stem}_head.mp4"
        )
        if head_path is None:
            return None
    tail_path = None
    if window > 0:
        tail_path = trim_clip_frames(
            clip_path, head_frames, window, workdir / f"{stem}_tail.mp4"
        )
        if tail_path is None:
            return None
    return {
        "clip": str(clip_path),
        "total_frames": int(total),
        "window_frames": window,
        "head_path": head_path,
        "head_frames": head_frames,
        "tail_path": tail_path,
        "tail_frames": window,
    }


# ---------------------------------------------------------------------------
# Film-burn overlay (PIL-generated frames, piped to ffmpeg)
# ---------------------------------------------------------------------------

def render_burn_overlay(out_path, frame_count):
    """Generate the film-burn flare clip: black frames with an
    orange-white flare that grows and fades across the window
    (peak at the boundary, i.e. the middle of the 2W segment).
    Returns the path on success, else None."""
    frame_count = int(frame_count)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS),
        "-i", "-",
        "-an", "-c:v", VIDEO_CODEC, "-preset", PRESET, "-crf", "20",
        "-pix_fmt", "yuv420p", "-frames:v", str(frame_count),
        str(out_path),
    ]
    proc = subprocess.Popen(
        command, stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    try:
        for n in range(frame_count):
            progress = n / max(frame_count - 1, 1)
            # Envelope: 0 at both ends, 1 at the boundary (middle).
            import math

            envelope = math.sin(math.pi * progress)
            frame = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
            draw = ImageDraw.Draw(frame)
            cx, cy = WIDTH * 0.5, HEIGHT * 0.55
            outer = 140 + 820 * progress
            layers = [
                (outer, (255, 110, 0)),
                (outer * 0.62, (255, 170, 40)),
                (outer * 0.34, (255, 235, 190)),
            ]
            for radius, colour in layers:
                scaled = tuple(int(c * envelope) for c in colour)
                if max(scaled) <= 0:
                    continue
                draw.ellipse(
                    [cx - radius, cy - radius * 0.78,
                     cx + radius, cy + radius * 0.78],
                    fill=scaled,
                )
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    stderr = proc.stderr.read() if proc.stderr else b""
    if proc.wait() != 0:
        print("BURN OVERLAY FAILED:", stderr.decode("utf-8", "replace")[:500])
        return None
    if probe_frame_count(out_path) != frame_count:
        return None
    return str(out_path)


# ---------------------------------------------------------------------------
# Transition segment
# ---------------------------------------------------------------------------

def build_transition_segment(clip_a, clip_b, kind, window_frames, out_path):
    """Build the 2W-frame segment replacing A's last W frames and
    B's first W frames for transition ``kind`` into B.

    Returns a record dict on success (with the effective — possibly
    clamped — window and the exact output frame count), or None
    when no transition can be built (caller falls back to cut).
    """
    kind = str(kind or "").strip().lower()
    if kind not in NON_CUT_TRANSITIONS:
        return None
    a_frames = probe_frame_count(clip_a)
    b_frames = probe_frame_count(clip_b)
    if a_frames is None or b_frames is None:
        return None
    if int(a_frames) < 2 or int(b_frames) < 2:
        return None

    requested_window = int(window_frames)
    window = min(requested_window, int(a_frames), int(b_frames))
    if window < 1:
        return None
    clamped = window != requested_window
    total_out = 2 * window
    denom = max(total_out - 1, 1)
    pad_seconds = f"{window / FPS:.6f}"

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Freeze-handle extended layers (each exactly 2W frames).
    a_layer = (
        f"[0:v]trim=start_frame={int(a_frames) - window}:end_frame={int(a_frames)},"
        "setpts=PTS-STARTPTS,"
        f"tpad=stop_mode=clone:stop_duration={pad_seconds},"
        f"trim=end_frame={total_out},setpts=PTS-STARTPTS,"
        f"fps={FPS},format=yuv420p[a]"
    )
    b_layer = (
        f"[1:v]trim=start_frame=0:end_frame={window},"
        "setpts=PTS-STARTPTS,"
        f"tpad=start_mode=clone:start_duration={pad_seconds},"
        f"trim=end_frame={total_out},setpts=PTS-STARTPTS,"
        f"fps={FPS},format=yuv420p[b]"
    )
    alpha = f"(N/{denom})"

    input_args = ["-i", str(clip_a), "-i", str(clip_b)]
    extra = []
    duration = f"{total_out / FPS:.6f}"

    if kind == "dissolve":
        graph = (
            f"{a_layer};{b_layer};"
            f"[a][b]blend=all_mode=normal:"
            f"all_expr='A*(1-{alpha})+B*{alpha}',format=yuv420p[out]"
        )
    elif kind == "fade":
        # Dissolve blend darkened to black exactly at the boundary:
        # brightness factor |2*alpha - 1| is 1 at both ends, 0 mid.
        # The factor is applied with geq in RGB — multiplying the
        # blend result inside the (YUV) blend expression would scale
        # the offset chroma planes toward 0 and shift the hue green
        # instead of darkening (verified on real clips).
        factor = f"abs(2*N/{denom}-1)"
        graph = (
            f"{a_layer};{b_layer};"
            f"[a][b]blend=all_mode=normal:"
            f"all_expr='A*(1-{alpha})+B*{alpha}',format=rgb24[mix];"
            f"[mix]geq=r='r(X,Y)*{factor}':g='g(X,Y)*{factor}':"
            f"b='b(X,Y)*{factor}',format=yuv420p[out]"
        )
    elif kind == "whip_pan":
        graph = (
            f"{a_layer};{b_layer};"
            f"color=c=black:s={WIDTH}x{HEIGHT}:r={FPS}:d={duration}[base];"
            f"[base][a]overlay=x='-{WIDTH}*n/{denom}':y=0:eval=frame[tmp];"
            f"[tmp][b]overlay=x='{WIDTH}-{WIDTH}*n/{denom}':y=0:eval=frame,"
            "format=yuv420p[out]"
        )
    elif kind == "film_burn":
        burn_path = out_path.parent / f"{out_path.stem}.burn.mp4"
        if render_burn_overlay(burn_path, total_out) is None:
            return None
        input_args.extend(["-i", str(burn_path)])
        graph = (
            f"{a_layer};{b_layer};"
            f"[a][b]blend=all_mode=normal:"
            f"all_expr='A*(1-{alpha})+B*{alpha}',format=rgb24[mix];"
            f"[2:v]trim=end_frame={total_out},setpts=PTS-STARTPTS,"
            "format=rgb24[burn];"
            "[mix][burn]blend=all_mode=screen,format=yuv420p[out]"
        )
        extra.append(str(burn_path))
    elif kind == "split_screen":
        # The incoming clip slides in from the right over the
        # outgoing one; mid-window the frame is a true split
        # screen. A white divider rides the incoming clip's left
        # edge (drawn onto B before the overlay, so it tracks).
        graph = (
            f"{a_layer};{b_layer};"
            f"color=c=black:s={WIDTH}x{HEIGHT}:r={FPS}:d={duration}[base];"
            f"[base][a]overlay=x=0:y=0[tmp];"
            f"[b]drawbox=x=0:y=0:w=6:h={HEIGHT}:"
            "color=white:t=fill[bb];"
            f"[tmp][bb]overlay=x='{WIDTH}-{WIDTH}*n/{denom}':y=0:"
            "eval=frame,format=yuv420p[out]"
        )
    elif kind == "parallax_depth":
        # Depth cue, not a blend: A drifts left at half rate and
        # dims while B slides in at full rate over it — the two
        # planes move at different speeds, which is what reads as
        # parallax at an edit point.
        graph = (
            f"{a_layer};{b_layer};"
            f"color=c=black:s={WIDTH}x{HEIGHT}:r={FPS}:d={duration}[base];"
            f"[a]eq=brightness='-0.16*n/{denom}':eval=frame[adim];"
            f"[base][adim]overlay=x='-{WIDTH // 2}*n/{denom}':y=0:"
            "eval=frame[tmp];"
            f"[tmp][b]overlay=x='{WIDTH}-{WIDTH}*n/{denom}':y=0:"
            "eval=frame,format=yuv420p[out]"
        )
    elif kind == "film_strip":
        # A sprocketed film-strip band rides the wipe seam: left
        # of the seam the incoming clip, right of it the outgoing.
        # The blend runs in RGB so the seam's X is full-resolution
        # in every channel; the strip itself (dark band, bright
        # sprocket holes, cell dividers) is drawn by geq over the
        # seam position sx = WIDTH * N / denom.
        sx = f"({WIDTH}*N/{denom})"
        band = f"abs(X-{sx})"

        def _strip_expr(channel, cell_colour):
            return (
                f"if(gt({band},84),{channel}(X,Y),"
                f"if(gt({band},52),if(lt(mod(Y,96),44),235,16),"
                f"if(lt(mod(Y,288),10),{cell_colour},"
                f"{channel}(X,Y)*0.5)))"
            )

        graph = (
            f"{a_layer};{b_layer};"
            "[a]format=rgb24[ar];[b]format=rgb24[br];"
            f"[ar][br]blend=all_mode=normal:"
            f"all_expr='if(lt(X,{sx}),B,A)'[mix];"
            f"[mix]geq=r='{_strip_expr('r', 200)}':"
            f"g='{_strip_expr('g', 170)}':b='{_strip_expr('b', 60)}',"
            "format=yuv420p[out]"
        )
    else:  # pragma: no cover - guarded above
        return None

    command = _encode_command(graph, out_path, total_out, input_args)
    if not _run_ffmpeg(command):
        return None
    actual = probe_frame_count(out_path)
    if actual != total_out:
        print(
            f"TRANSITION SEGMENT FRAME MISMATCH: kind={kind} "
            f"expected {total_out}, got {actual} — falling back to cut."
        )
        return None

    return {
        "path": str(out_path),
        "kind": kind,
        "window_frames": window,
        "requested_window_frames": requested_window,
        "window_clamped": clamped,
        "frames": total_out,
        "clip_a_frames": int(a_frames),
        "clip_b_frames": int(b_frames),
        "filtergraph": graph,
        "burn_overlay": extra[0] if extra else None,
    }
