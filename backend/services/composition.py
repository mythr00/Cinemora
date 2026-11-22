"""Composition engine (Master Directive Phase 6, AM-011).

A timeline item's visual no longer has to be a single full-frame
replacement: a composition plan layers designed elements OVER the
item's footage/still — a lower-third label card, or a footage
window inset — each layer carrying its own timing, geometry,
opacity, z-order, registry-named animation and validation status.

Model (plain dicts, JSON-safe):

    composition = {
        "pattern": "lower_third_card",
        "decision_reason": "scene_entry_label",
        "layers": [layer, ...],
    }
    layer = {
        "kind": "lower_third_card" | "footage_window",
        "start": float, "end": float,          # item-local seconds
        "position": {"x": int, "y": int, "w": int, "h": int},
        "scale": float, "opacity": float, "z_order": int,
        "animation": {"entrance": <registry name>,
                      "exit": <registry name>},
        "source": {...},                        # per kind, below
        "validation": {"passed": bool, "failures": [str]},
    }

lower_third_card source: {"type": "label", "label": str,
"sublabel": str|None, "theme": str|None} — rendered to a
transparent PNG by this module (PIL) and overlaid by ffmpeg.
footage_window source: {"type": "media", "path": str,
"clip_start": float, "clip_end": float} — the source window is
the one the CALLER recorded (Phase 5 timeline stamps); this module
never re-derives a window.

Rules:
* Validation is explicit and recorded per layer: timing must fit
  inside the item, geometry must sit inside the frame (text layers
  additionally inside the safe area), opacity in (0, 1], animation
  names must exist in the motion registry. An invalid layer is
  NEVER rendered and never silently dropped — its failures ride on
  the layer record.
* Rendering is honest: apply_composition returns a record of what
  was applied and what failed; on any ffmpeg failure the caller
  keeps the base clip. A failed layer never fails the item.
* Composition is opt-in per item — stamped only where the Visual
  Director decided it (plan v2). Nothing here scans a timeline
  looking for places to overlay.
"""

from pathlib import Path

from services.motion_engine import motion_registry, themes

FRAME_WIDTH = 1920
FRAME_HEIGHT = 1080
SAFE_MARGIN_X = 64
SAFE_MARGIN_BOTTOM = 54

PATTERNS = ("lower_third_card", "footage_window", "split_screen")


# ---------------------------------------------------------------------------
# Layer constructors (the reusable primitives)
# ---------------------------------------------------------------------------

def lower_third_layer(label, sublabel=None, *, start=0.0, end=None,
                      duration=None, theme=None):
    """A lower-third label card: bottom-left, inside the safe
    area, sized to the label. `end` defaults to the item duration
    the caller supplies."""
    if end is None:
        end = float(duration or 0.0)
    return {
        "kind": "lower_third_card",
        "start": round(float(start), 3),
        "end": round(float(end), 3),
        "position": {"x": 96, "y": FRAME_HEIGHT - 264, "w": 1184, "h": 132},
        "scale": 1.0,
        "opacity": 0.96,
        "z_order": 10,
        "animation": {"entrance": "rise_in", "exit": "fade_out"},
        "source": {
            "type": "label",
            "label": str(label or ""),
            "sublabel": str(sublabel) if sublabel else None,
            "theme": theme,
        },
    }


def footage_window_layer(media_path, *, clip_start, clip_end,
                         start=0.0, end=None, duration=None,
                         rect=None, z_order=20):
    """A footage window: a second media source inset over the
    base. The source window (clip_start/clip_end) is the caller's
    RECORDED window — composition never re-derives one."""
    if end is None:
        end = float(duration or 0.0)
    rect = rect or {"x": 1232, "y": 72, "w": 592, "h": 333}
    return {
        "kind": "footage_window",
        "start": round(float(start), 3),
        "end": round(float(end), 3),
        "position": dict(rect),
        "scale": 1.0,
        "opacity": 1.0,
        "z_order": int(z_order),
        "animation": {"entrance": "fade_in", "exit": "fade_out"},
        "source": {
            "type": "media",
            "path": str(media_path),
            "clip_start": round(float(clip_start), 3),
            "clip_end": round(float(clip_end), 3),
        },
    }


def split_screen_layer(media_path, *, clip_start, clip_end,
                       start=0.0, end=None, duration=None, side="right"):
    """A split-screen primitive: the layer's media occupies one
    half of the frame, the base clip the other half.

    Phase 8 (AM-011 completion): the render path IS wired —
    apply_composition builds the split view (base half + media
    half, side by side) and overlays it for the layer's window.
    Phase 6 shipped constructor + validation only and marked such
    layers `renderable: False`; that flag is superseded — a valid
    split_screen layer now renders like any other layer, and an
    invalid one is excluded by validation, never by a blanket
    flag."""
    if end is None:
        end = float(duration or 0.0)
    rect = (
        {"x": FRAME_WIDTH // 2, "y": 0, "w": FRAME_WIDTH // 2, "h": FRAME_HEIGHT}
        if side == "right"
        else {"x": 0, "y": 0, "w": FRAME_WIDTH // 2, "h": FRAME_HEIGHT}
    )
    return {
        "kind": "split_screen",
        "start": round(float(start), 3),
        "end": round(float(end), 3),
        "position": rect,
        "scale": 1.0,
        "opacity": 1.0,
        "z_order": 20,
        "animation": {"entrance": "cut_in", "exit": "cut_out"},
        "source": {
            "type": "media",
            "path": str(media_path),
            "clip_start": round(float(clip_start), 3),
            "clip_end": round(float(clip_end), 3),
            "side": side,
        },
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_layer(layer, item_duration):
    """Validate one layer against its item. Returns the list of
    failure strings (empty = valid). Every rule is a recorded,
    named failure — never an exception, never a silent fix-up."""
    failures = []
    if not isinstance(layer, dict):
        return ["layer_not_a_dict"]
    kind = layer.get("kind")
    if kind not in ("lower_third_card", "footage_window", "split_screen"):
        failures.append(f"unknown_layer_kind:{kind}")
    try:
        start = float(layer.get("start"))
        end = float(layer.get("end"))
    except (TypeError, ValueError):
        return failures + ["layer_timing_not_numeric"]
    if start < 0:
        failures.append("layer_starts_before_item")
    if end <= start:
        failures.append("layer_end_not_after_start")
    if item_duration is not None and end > float(item_duration) + 0.001:
        failures.append("layer_ends_after_item")
    position = layer.get("position") or {}
    try:
        x, y = int(position["x"]), int(position["y"])
        w, h = int(position["w"]), int(position["h"])
        if w <= 0 or h <= 0:
            failures.append("layer_has_no_area")
        if x < 0 or y < 0 or x + w > FRAME_WIDTH or y + h > FRAME_HEIGHT:
            failures.append("layer_outside_frame")
        elif kind == "lower_third_card" and (
            x < SAFE_MARGIN_X
            or y + h > FRAME_HEIGHT - SAFE_MARGIN_BOTTOM
        ):
            failures.append("layer_outside_safe_area")
    except (KeyError, TypeError, ValueError):
        failures.append("layer_position_incomplete")
    opacity = layer.get("opacity")
    if not isinstance(opacity, (int, float)) or not (0 < opacity <= 1):
        failures.append("layer_opacity_out_of_range")
    animation = layer.get("animation") or {}
    if not motion_registry.is_entrance(animation.get("entrance")):
        failures.append(
            f"unknown_entrance_animation:{animation.get('entrance')}"
        )
    if not motion_registry.is_exit(animation.get("exit")):
        failures.append(f"unknown_exit_animation:{animation.get('exit')}")
    source = layer.get("source") or {}
    if kind == "lower_third_card":
        if not str(source.get("label") or "").strip():
            failures.append("lower_third_has_no_label")
    if kind in ("footage_window", "split_screen"):
        if not source.get("path"):
            failures.append("media_layer_has_no_source")
        if source.get("clip_end") is not None and source.get(
            "clip_start"
        ) is not None and float(source["clip_end"]) <= float(
            source["clip_start"]
        ):
            failures.append("media_layer_window_empty")
    return failures


def validate_composition(composition, item_duration):
    """Validate every layer of a composition plan in place: each
    layer gains its `validation` record. Returns the plan."""
    if not isinstance(composition, dict):
        return composition
    for layer in composition.get("layers") or []:
        failures = validate_layer(layer, item_duration)
        layer["validation"] = {
            "passed": not failures,
            "failures": failures,
        }
    return composition


def valid_layers(composition):
    """The layers that passed validation AND are renderable."""
    return [
        layer for layer in (composition or {}).get("layers") or []
        if (layer.get("validation") or {}).get("passed")
        and layer.get("renderable", True)
    ]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_lower_third_png(layer, out_path):
    """Render a lower-third layer to a full-frame transparent PNG:
    a translucent themed bar with an accent rule, the label in
    bold and the sublabel beneath it. Text is drawn ONLY from the
    layer's recorded label/sublabel."""
    from PIL import Image, ImageDraw

    from services.motion_engine.core import load_font

    source = layer.get("source") or {}
    theme = themes.resolve_theme(source.get("theme")) or themes.THEMES[
        themes.DEFAULT_THEME
    ]
    position = layer["position"]
    x, y, w, h = (
        int(position["x"]), int(position["y"]),
        int(position["w"]), int(position["h"]),
    )
    image = Image.new("RGBA", (FRAME_WIDTH, FRAME_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, "RGBA")
    opacity = float(layer.get("opacity") or 0.96)
    bar_alpha = int(235 * opacity)
    draw.rectangle(
        [x, y, x + w, y + h],
        fill=tuple(theme["surface"]) + (bar_alpha,),
    )
    draw.rectangle(
        [x, y, x + 10, y + h], fill=tuple(theme["accent"]) + (255,)
    )
    label_font = load_font(theme["font_bold"], 44)
    sub_font = load_font(theme["font"], 30)
    draw.text(
        (x + 36, y + 22), str(source.get("label") or "")[:64],
        font=label_font, fill=tuple(theme["text"]) + (255,),
    )
    if source.get("sublabel"):
        draw.text(
            (x + 36, y + 76), str(source["sublabel"])[:72],
            font=sub_font, fill=tuple(theme["muted"]) + (255,),
        )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)
    return out_path


def _fade_terms(layer):
    """(fade_in_start, fade_in_dur, fade_out_start, fade_out_dur)
    from the layer's registry-named animations."""
    animation = layer.get("animation") or {}
    entrance = motion_registry.entrance(animation.get("entrance")) or {}
    exit_b = motion_registry.exit_behaviour(animation.get("exit")) or {}
    in_dur = motion_registry.duration_seconds(entrance.get("duration")) or 0.0
    out_dur = motion_registry.duration_seconds(exit_b.get("duration")) or 0.0
    start, end = float(layer["start"]), float(layer["end"])
    in_dur = min(in_dur, max(0.0, (end - start) / 2))
    out_dur = min(out_dur, max(0.0, (end - start) / 2))
    return start, in_dur, end - out_dur, out_dur


def apply_composition(base_clip, composition, out_path, work_dir):
    """Apply a composition plan's valid layers over a rendered
    base clip with ffmpeg overlay. Returns (ok, record): on
    success out_path holds the composed clip; on any failure the
    base clip is untouched and the record names the failure —
    the caller keeps the base (a failed layer never fails the
    item, and never silently disappears: it is in the record).

    Layers apply in z_order. lower_third_card layers render via
    PNG; footage_window layers trim their source to the RECORDED
    clip window and scale it into the layer rect; split_screen
    layers build a half-and-half composite of base + source for
    the layer's window (Phase 8)."""
    from services import renderer as _renderer

    record = {"applied": [], "failed": []}
    layers = valid_layers(composition)
    if not layers:
        return False, {**record, "reason": "no_valid_layers"}
    layers = sorted(layers, key=lambda l: l.get("z_order") or 0)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    current = Path(base_clip)
    try:
        # The composed output must keep the base clip's EXACT
        # duration — the editor's frame lock is a hard contract, and
        # an overlay step otherwise stretches the output toward its
        # longest input stream (-shortest does not reliably clamp a
        # filtergraph; Petito run 3f54aaea composed a 6.0s item to
        # 7.0s). Probe the base once and clamp every step to it.
        from services.media_probe import probe_video

        _probe = probe_video(current)
        base_duration = (
            float(_probe["duration_seconds"])
            if _probe and _probe.get("duration_seconds")
            else None
        )
        _clamp = (
            ["-t", str(round(base_duration, 3))]
            if base_duration else []
        )
        for index, layer in enumerate(layers):
            step_out = work_dir / f"composed_step_{index}.mp4"
            kind = layer["kind"]
            start, in_dur, out_start, out_dur = _fade_terms(layer)
            end = float(layer["end"])
            if kind == "lower_third_card":
                png = work_dir / f"layer_{index}.png"
                render_lower_third_png(layer, png)
                overlay_input = [
                    # Bounded loop: an unbounded -loop 1 image input
                    # never EOFs, and -shortest does not reliably end
                    # the overlay filtergraph — the step hung to the
                    # ffmpeg timeout on the Petito production
                    # (2026-11-22). Bound it to the base duration
                    # (fallback: the layer end + 1s of fade margin).
                    "-loop", "1", "-framerate", "30",
                    "-t", str(round(
                        base_duration if base_duration
                        else end + 1.0, 3)),
                    "-i", str(png),
                ]
                prep = "format=rgba"
            elif kind == "footage_window":
                source = layer["source"]
                overlay_input = [
                    "-ss", str(source["clip_start"]),
                    "-to", str(source["clip_end"]),
                    "-i", str(source["path"]),
                ]
                pos = layer["position"]
                prep = (
                    f"scale={int(pos['w'])}:{int(pos['h'])}"
                    ":force_original_aspect_ratio=increase,"
                    f"crop={int(pos['w'])}:{int(pos['h'])},format=rgba"
                )
            elif kind == "split_screen":
                # Phase 8: build the split view — the base clip
                # scaled/cropped into one half, the layer's media
                # (its RECORDED window) into the other — then
                # overlay that full-frame composite for the
                # layer's window only. Outside the window the
                # base plays untouched.
                source = layer["source"]
                half_w = FRAME_WIDTH // 2
                half = (
                    f"scale={half_w}:{FRAME_HEIGHT}"
                    ":force_original_aspect_ratio=increase,"
                    f"crop={half_w}:{FRAME_HEIGHT},setsar=1"
                )
                if source.get("side") == "left":
                    stack = "[med][bas]hstack=inputs=2[split]"
                else:
                    stack = "[bas][med]hstack=inputs=2[split]"
                filter_complex = (
                    f"[0:v]{half}[bas];"
                    f"[1:v]{half}[med];"
                    f"{stack};"
                    f"[0:v][split]overlay=0:0"
                    f":enable='between(t,{start},{end})'[out]"
                )
                command = [
                    "ffmpeg", "-y", "-i", str(current),
                    "-ss", str(source["clip_start"]),
                    "-to", str(source["clip_end"]),
                    "-i", str(source["path"]),
                    "-filter_complex", filter_complex,
                    "-map", "[out]", "-map", "0:a?",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "copy", "-shortest", *_clamp, str(step_out),
                ]
                result = _renderer.run_ffmpeg(command)
                if not result or not step_out.exists():
                    # run_ffmpeg returns False on failure/timeout and
                    # never None; a timed-out step still leaves a
                    # partial file behind, so existence alone is not
                    # success (Petito run 6091b411: the partial file
                    # replaced the good base clip and the frame lock
                    # failed on the corrupt result).
                    step_out.unlink(missing_ok=True)
                    record["failed"].append({
                        "kind": kind, "reason": "overlay_render_failed",
                    })
                    return False, record
                record["applied"].append({
                    "kind": kind,
                    "z_order": layer.get("z_order"),
                    "start": layer["start"], "end": layer["end"],
                })
                current = step_out
                continue
            else:
                record["failed"].append({
                    "kind": kind, "reason": "layer_kind_not_renderable",
                })
                continue
            if in_dur > 0:
                prep += f",fade=t=in:st={start}:d={in_dur}:alpha=1"
            if out_dur > 0:
                prep += f",fade=t=out:st={out_start}:d={out_dur}:alpha=1"
            pos = layer["position"]
            filter_complex = (
                f"[1:v]{prep}[ov];"
                f"[0:v][ov]overlay={int(pos['x'])}:{int(pos['y'])}"
                f":enable='between(t,{start},{end})'[out]"
            )
            command = [
                "ffmpeg", "-y", "-i", str(current),
                *overlay_input,
                "-filter_complex", filter_complex,
                "-map", "[out]", "-map", "0:a?",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "copy", "-shortest", *_clamp, str(step_out),
            ]
            result = _renderer.run_ffmpeg(command)
            if not result or not step_out.exists():
                # See the split_screen branch: False from run_ffmpeg
                # (failure or timeout) must fail the step even when
                # a partial output file exists.
                step_out.unlink(missing_ok=True)
                record["failed"].append({
                    "kind": kind, "reason": "overlay_render_failed",
                })
                return False, record
            record["applied"].append({
                "kind": kind,
                "z_order": layer.get("z_order"),
                "start": layer["start"], "end": layer["end"],
            })
            current = step_out
    except Exception as error:  # honest failure: base clip survives
        record["failed"].append({"reason": "composition_exception",
                                 "detail": str(error)})
        return False, record
    if not record["applied"]:
        return False, record
    Path(current).replace(out_path)
    return True, record


# ---------------------------------------------------------------------------
# Timeline stamping (the Director-decision consumer)
# ---------------------------------------------------------------------------

def apply_decisions_to_timeline(timeline_data, plan_v2):
    """Stamp composition plans onto timeline items from plan v2's
    per-beat composition decisions. Matching is by (scene_number,
    sentence_index) — the keys timeline items and v2 rows share.
    Layer timing is item-local (0 .. item duration), read from the
    item's own recorded duration. Returns the number of items
    stamped. Items without a decision are untouched."""
    decisions = {}
    for row in (plan_v2 or {}).get("sentences") or []:
        if not isinstance(row, dict):
            continue
        decision = row.get("decision") or {}
        composition = decision.get("composition")
        if not composition:
            continue
        key = (row.get("scene_number"), row.get("sentence_index"))
        if key[0] is not None and key[1] is not None:
            decisions[key] = composition
    if not decisions:
        return 0
    stamped = 0
    for scene in (timeline_data or {}).get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        scene_number = scene.get("scene_number")
        for item in scene.get("items") or []:
            if not isinstance(item, dict):
                continue
            decision = decisions.get(
                (scene_number, item.get("sentence_index"))
            )
            if decision is None:
                continue
            duration = float(item.get("duration") or 0.0)
            if duration <= 0:
                continue
            pattern = decision.get("pattern") or "lower_third_card"
            if pattern == "footage_window":
                # Phase 8: the Director's window decision carries
                # the second visual's RECORDED clip window; the
                # layer is built from exactly that record — a
                # decision without a usable window stamps nothing
                # (the layer would fail validation anyway, but an
                # unusable decision must not masquerade as a plan).
                asset = decision.get("window_asset") or {}
                if not asset.get("path") or asset.get(
                    "clip_end", 0
                ) <= asset.get("clip_start", 0):
                    continue
                layer = footage_window_layer(
                    asset["path"],
                    clip_start=asset["clip_start"],
                    clip_end=asset["clip_end"],
                    start=0.0,
                    duration=duration,
                )
            elif pattern == "split_screen":
                asset = decision.get("split_asset") or {}
                if not asset.get("path") or asset.get(
                    "clip_end", 0
                ) <= asset.get("clip_start", 0):
                    continue
                layer = split_screen_layer(
                    asset["path"],
                    clip_start=asset["clip_start"],
                    clip_end=asset["clip_end"],
                    start=0.0,
                    duration=duration,
                    side=asset.get("side") or "right",
                )
            else:
                layer = lower_third_layer(
                    decision.get("label"),
                    decision.get("sublabel"),
                    start=0.0,
                    duration=duration,
                    theme=(plan_v2 or {}).get("theme"),
                )
                pattern = "lower_third_card"
            plan = {
                "pattern": pattern,
                "decision_reason": decision.get("reason"),
                "layers": [layer],
            }
            validate_composition(plan, duration)
            item["composition"] = plan
            stamped += 1
    return stamped
