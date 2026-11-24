"""Thumbnail Generator (founder priority #13; gap-audit doc 03).

The audit's verdict: Cinemora had NO thumbnail feature and no
image-generation capability anywhere in the stack — and the
vision's promise (an AI that understands a reference thumbnail and
paints an original) is not deliverable on today's stack: no vision
input path, no text-to-image provider, no GPU. What IS deliverable
— the audit's recommended v1, built here — is the vision's
purpose: compelling, relevant, non-misleading thumbnails derived
from the documentary itself, composed from the project's OWN
recorded material on the strongest graphics stack in the product
(the PIL Motion Graphics toolkit).

- BASES are real project material only, never stock and never
  generated imagery: frames extracted from the finished film of
  the project's latest completed production run (at timestamps the
  recorded timeline actually contains), the run's verified stills
  (asset-ledger decisions recorded as selected / still /
  swapped_in whose files are on disk and probe as images), and
  the project's own uploads (user_supplied images in the upload
  index — including reference thumbnails the user uploads through
  the existing POST /projects/{id}/uploads machinery).
- COMPOSITION is PIL (services/motion_engine/core.py's fit_cover /
  wrap_text / bundled DejaVu fonts), rendered at YouTube's exact
  spec: 1280x720. Four layout families from the audit §6 —
  bottom band (subject-hero), top band, left panel (split), centre
  scrim. The headline is drawn as pixels by PIL: user text is
  never interpreted as filter/programme syntax at all, which is
  strictly stronger than the drawtext escaping discipline the
  video pipeline needs. PNG is written first; if it exceeds
  YouTube's 2 MB limit the variant is re-encoded as JPEG q90 and
  the recorded format says so.
- HEADLINES are short BY DESIGN. A thumbnail is packaging, not
  film content — the founder's no-full-sentence-card rule governs
  graphics inside the film; a thumbnail headline is a few words
  that name the film, so the studio enforces a hard cap (8 words /
  60 characters) and refuses longer text with the reason stated,
  rather than shrinking a sentence into illegibility.
- CHECKS are mechanical measurements of the finished file, labelled
  as exactly that: pixel dimensions, encoded format, file size
  against YouTube's 2 MB limit, headline word/character counts,
  the WCAG contrast ratio of the headline text colour against the
  measured mean colour of its backing region in the rendered
  image, and the headline's font height when the thumbnail is
  displayed at 128x72 (10% scale). There is NO predicted CTR, no
  performance score, no virality metric — nothing in this module
  claims to know how a thumbnail will perform.
- THE BRIEF is derived deterministically from recorded artefacts:
  the project title/category and the Case Bible (primary subject,
  recorded quantity/date facts). Headline suggestions are those
  recorded strings — the title and the primary subject's recorded
  name — never invented copy. An optional AI drafting action runs
  through llm_client (the Script Studio's pattern: explicit,
  user-triggered, proposal-only, 503 when no provider is
  configured, 502 when it fails) and is constrained to the
  recorded facts it is given.
- REFERENCE ANALYSIS (Workflow A's deterministic half): for an
  uploaded reference image, the measured properties the stack can
  honestly produce — OCR text via frame verification's provider
  chain (engine named), the dominant colour palette with shares
  (PIL quantisation), mean luminance and its spread. These are
  labelled "measured properties", never "AI understanding" of
  composition or emotion — that capability does not exist here
  (audit doc 03 §2) and the UI must not pretend otherwise.
- Every variant records its full composition (base kind + the
  recorded identity of the base, headline, layout, accent) in
  thumbnails/thumbnails.json under the project dir, so any variant
  can be regenerated exactly. Files live beside the record.

Every failure is data or a ThumbnailError with the v1 shape.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from services import editor, frame_verification, llm_client, media_paths, media_probe, run_ledger
from services.atomic_write import write_json_atomic
from services.motion_engine import core as motion_core
from services.motion_engine.styles import FONT_BOLD

# YouTube's published thumbnail spec (audit doc 03 §4).
THUMB_WIDTH = 1280
THUMB_HEIGHT = 720
YOUTUBE_MAX_BYTES = 2_000_000

# Headlines are a few words by design (see module docstring).
MAX_HEADLINE_WORDS = 8
MAX_HEADLINE_CHARS = 60
MAX_HEADLINE_LINES = 3

# The duration badge YouTube overlays sits in the bottom-right
# corner; band/scrim layouts keep headline text out of it.
BADGE_SAFE_TEXT_WIDTH = 980

LAYOUTS = ("bottom_band", "top_band", "left_panel", "center_scrim")
ACCENTS = {
    "amber": (245, 183, 49),
    "red": (229, 57, 53),
    "blue": (66, 133, 244),
    "white": (255, 255, 255),
}
TEXT_RGB = (255, 255, 255)

STORE_DIRNAME = "thumbnails"
STORE_FILENAME = "thumbnails.json"
_ACCEPTED_STILL_DECISIONS = ("selected", "still", "swapped_in")


class ThumbnailError(Exception):
    """A domain error with an HTTP status + stable machine code
    (the API layer maps it onto the v1 error shape)."""

    def __init__(self, status_code: int, code: str, message: str,
                 details=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _project_dir(project_id) -> Path:
    return media_paths.project_dir(project_id)


def _store_dir(project_id) -> Path:
    return _project_dir(project_id) / STORE_DIRNAME


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------
# Store (thumbnails/thumbnails.json — the project's variant record)
# ---------------------------------------------------------------------


def _load_store(project_id) -> dict:
    path = _store_dir(project_id) / STORE_FILENAME
    data = _read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("variants"), list):
        return {"variants": []}
    return data


def _save_store(project_id, store: dict) -> dict:
    store_dir = _store_dir(project_id)
    store_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(store_dir / STORE_FILENAME, store)
    return store


def list_variants(project_id) -> list[dict]:
    """Recorded variants, newest first. A variant whose file has
    disappeared from disk is listed with available=False — the
    record is the truth about what was generated; the file's
    absence is reported, never hidden."""
    store = _load_store(project_id)
    store_dir = _store_dir(project_id)
    out = []
    for variant in store.get("variants") or []:
        entry = dict(variant)
        file_name = entry.get("file_name")
        entry["available"] = bool(
            file_name and (store_dir / str(file_name)).is_file()
        )
        out.append(entry)
    out.sort(key=lambda v: v.get("created_at") or "", reverse=True)
    return out


def _find_variant(project_id, thumb_id) -> dict:
    for variant in _load_store(project_id).get("variants") or []:
        if variant.get("id") == thumb_id:
            return variant
    raise ThumbnailError(404, "thumbnail_not_found", "No such thumbnail variant.")


def variant_path(project_id, thumb_id) -> Path:
    variant = _find_variant(project_id, thumb_id)
    path = _store_dir(project_id) / str(variant.get("file_name") or "")
    if not path.is_file():
        raise ThumbnailError(
            404, "thumbnail_file_missing",
            "This variant is recorded but its image file is no "
            "longer on disk. Regenerate it from its composition.",
        )
    return path


def delete_variant(project_id, thumb_id) -> dict:
    store = _load_store(project_id)
    variants = store.get("variants") or []
    kept = [v for v in variants if v.get("id") != thumb_id]
    if len(kept) == len(variants):
        raise ThumbnailError(404, "thumbnail_not_found", "No such thumbnail variant.")
    removed = next(v for v in variants if v.get("id") == thumb_id)
    store["variants"] = kept
    _save_store(project_id, store)
    file_name = removed.get("file_name")
    if file_name:
        try:
            (_store_dir(project_id) / str(file_name)).unlink(missing_ok=True)
        except OSError:
            pass
    return {"deleted": thumb_id}


# ---------------------------------------------------------------------
# Bases — the project's own recorded material
# ---------------------------------------------------------------------


def _latest_completed_run(project_id):
    for run in run_ledger.list_runs_for(project_id):
        if run.get("status") == "completed":
            return run
    return None


def _film_facts(project_id, run) -> dict | None:
    """The finished film of a completed run, resolved through the
    run-scoped artefact path, with its probed duration. None when
    the run recorded no film file on disk."""
    if run is None:
        return None
    project_dir = _project_dir(project_id)
    film = media_paths.resolve_artefact(
        project_dir, "documentary_final_output.mp4", run.get("run_id")
    )
    if not film.is_file():
        return None
    probe = media_probe.probe_video(film)
    duration = (probe or {}).get("duration_seconds")
    return {"path": film, "duration": duration, "run_id": run.get("run_id")}


def _frame_timestamps(project_id, run, duration) -> list[float]:
    """Candidate frame times from the run's RECORDED timeline:
    the midpoint of each visual item's recorded interval. Falls
    back to fixed fractions of the probed duration when the run
    recorded no timeline — labelled as such by the caller."""
    project_dir = _project_dir(project_id)
    timeline = _read_json(
        media_paths.resolve_artefact(project_dir, "timeline.json", run.get("run_id"))
    )
    times: list[float] = []
    if isinstance(timeline, dict):
        for item in timeline.get("items") or []:
            if not isinstance(item, dict):
                continue
            start, end = item.get("start"), item.get("end")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
                times.append(round((float(start) + float(end)) / 2.0, 2))
    times = sorted(set(times))
    if times:
        return times[:12], "timeline"
    if duration:
        return [round(duration * f, 2) for f in (0.1, 0.3, 0.5, 0.7, 0.9)], "duration_fractions"
    return [], "none"


def _contained_image(project_dir: Path, path) -> Path | None:
    """The resolved Path when `path` names an existing image file
    under the project directory (the media index's containment
    rule: records pointing outside the project are not this
    project's files), else None."""
    if not path:
        return None
    try:
        resolved = Path(str(path)).resolve()
        resolved.relative_to(project_dir.resolve())
    except (ValueError, OSError):
        return None
    if not resolved.is_file():
        return None
    return resolved if media_probe.probe_image(resolved) is not None else None


def _still_entries(project_id, run) -> list[dict]:
    """The latest completed run's verified stills, resolved to
    files. A ledger 'still'/'selected' decision keys on a
    candidate id, not a path, so resolution follows the Asset
    Library's strategy: the decision's own asset_key when it is a
    contained image path, else its recorded content_hash matched
    against the run's timeline files (the stills that actually
    reached the film) hashed the same way. A decision that
    resolves to no file on disk is not offered — never a dead
    base."""
    if run is None:
        return []
    project_dir = _project_dir(project_id)
    decisions, _total = run_ledger.list_decisions(run.get("run_id"), 100_000, 0)

    hash_map: dict[str, Path] = {}
    timeline = _read_json(
        media_paths.resolve_artefact(
            project_dir, "timeline.json", run.get("run_id")
        )
    )
    if isinstance(timeline, dict):
        for item in timeline.get("items") or []:
            if not isinstance(item, dict):
                continue
            resolved = _contained_image(
                project_dir, item.get("file_path") or item.get("path")
            )
            if resolved is not None:
                digest = run_ledger._file_sha256(str(resolved), {})
                if digest:
                    hash_map.setdefault(digest, resolved)

    entries: list[dict] = []
    seen_paths: set[str] = set()
    for row in decisions:
        if row.get("decision_type") not in _ACCEPTED_STILL_DECISIONS:
            continue
        resolved = _contained_image(project_dir, row.get("asset_key"))
        if resolved is None and row.get("content_hash"):
            resolved = hash_map.get(str(row["content_hash"]))
        if resolved is None or str(resolved) in seen_paths:
            continue
        seen_paths.add(str(resolved))
        entries.append(
            {
                "base_id": f"still:{row['id']}",
                "kind": "asset_still",
                "label": resolved.name,
                "decision_id": row["id"],
                "file_name": resolved.name,
                "file_path": str(resolved),
                "provider": row.get("provider"),
                "scene_number": row.get("scene_number"),
                "sentence_number": row.get("sentence_number"),
            }
        )
        if len(entries) >= 24:
            break
    return entries


def list_bases(project_id) -> dict:
    """Every base image the generator can compose from, grouped by
    kind, with honest states: a project with no completed run has
    no film frames and no verified stills, and says so."""
    run = _latest_completed_run(project_id)
    film = _film_facts(project_id, run)
    frames: list[dict] = []
    timestamps_source = "none"
    if film is not None:
        times, timestamps_source = _frame_timestamps(
            project_id, run, film["duration"]
        )
        for t in times:
            frames.append(
                {
                    "base_id": f"frame:{run.get('run_id')}:{t}",
                    "kind": "film_frame",
                    "label": f"Film frame at {t:.1f}s",
                    "run_id": run.get("run_id"),
                    "timestamp_seconds": t,
                }
            )

    stills = [
        {key: value for key, value in entry.items() if key != "file_path"}
        for entry in _still_entries(project_id, run)
    ]

    uploads: list[dict] = []
    for upload in editor._uploads_index(_project_dir(project_id)):
        path = upload.get("file_path")
        if not path or not Path(str(path)).is_file():
            continue
        if (upload.get("type") or "") != "image":
            if media_probe.probe_image(Path(str(path))) is None:
                continue
        uploads.append(
            {
                "base_id": f"upload:{upload.get('upload_id')}",
                "kind": "upload",
                "label": Path(str(path)).name,
                "upload_id": upload.get("upload_id"),
                "file_name": Path(str(path)).name,
            }
        )

    return {
        "film": {
            "state": "recorded" if film is not None else "not_recorded",
            "run_id": run.get("run_id") if run else None,
            "duration_seconds": film["duration"] if film else None,
            "timestamps_source": timestamps_source if film else "none",
            "frames": frames,
        },
        "stills": stills,
        "uploads": uploads,
    }


def _extract_frame(project_id, film_path: Path, t: float) -> Path:
    """Cut one frame from the film with ffmpeg, cached by film
    identity + frame number (the Editor Studio's preview caching
    pattern): repeat compositions from the same frame are free."""
    stat = film_path.stat()
    frame_number = int(round(t * 30))
    cache_key = hashlib.sha256(
        f"{film_path}|{stat.st_size}|{stat.st_mtime_ns}|{frame_number}".encode()
    ).hexdigest()[:20]
    cache_dir = _store_dir(project_id) / "frames"
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"frame_{cache_key}.jpg"
    if target.is_file():
        return target
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{t:.3f}", "-i", str(film_path),
            "-frames:v", "1", "-q:v", "3", str(target),
        ],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0 or not target.is_file():
        raise ThumbnailError(
            500, "frame_extract_failed",
            "ffmpeg could not extract that frame from the film.",
        )
    return target


def resolve_base(project_id, base_id: str):
    """(image_path, base_record) for a base id from list_bases.
    The base_record is what a variant stores as its composition's
    base — the recorded identity, not a bare path."""
    if not isinstance(base_id, str) or ":" not in base_id:
        raise ThumbnailError(404, "base_not_found", "No such base image.")
    kind, _, rest = base_id.partition(":")

    if kind == "frame":
        run_id, _, t_text = rest.rpartition(":")
        try:
            t = float(t_text)
        except ValueError:
            raise ThumbnailError(404, "base_not_found", "No such base image.") from None
        run = _latest_completed_run(project_id)
        if run is None or run.get("run_id") != run_id:
            raise ThumbnailError(
                404, "base_not_found",
                "That frame belongs to a run that is not this "
                "project's latest completed production.",
            )
        film = _film_facts(project_id, run)
        if film is None:
            raise ThumbnailError(
                404, "base_not_found",
                "The latest completed run recorded no film file.",
            )
        path = _extract_frame(project_id, film["path"], t)
        return path, {
            "kind": "film_frame",
            "run_id": run_id,
            "timestamp_seconds": t,
            "file_name": film["path"].name,
        }

    if kind == "still":
        run = _latest_completed_run(project_id)
        for entry in _still_entries(project_id, run):
            if entry["decision_id"] == rest:
                path = Path(entry["file_path"])
                return path, {
                    "kind": "asset_still",
                    "decision_id": entry["decision_id"],
                    "run_id": run.get("run_id"),
                    "file_name": path.name,
                    "provider": entry.get("provider"),
                    "scene_number": entry.get("scene_number"),
                    "sentence_number": entry.get("sentence_number"),
                }
        raise ThumbnailError(
            404, "base_not_found",
            "No recorded verified still with that identity is on disk.",
        )

    if kind == "upload":
        upload = editor.get_upload(project_id, rest)
        if upload is not None:
            path = Path(str(upload.get("file_path") or ""))
            if path.is_file() and media_probe.probe_image(path) is not None:
                return path, {
                    "kind": "upload",
                    "upload_id": rest,
                    "file_name": path.name,
                }
        raise ThumbnailError(
            404, "base_not_found",
            "No uploaded image with that identity is on disk.",
        )

    raise ThumbnailError(404, "base_not_found", "No such base image.")


# ---------------------------------------------------------------------
# The brief — derived deterministically from recorded artefacts
# ---------------------------------------------------------------------


def build_brief(project_id, project_title=None, project_category=None) -> dict:
    """What the documentary itself records as thumbnail material:
    title, category, the Case Bible's primary subject and its
    recorded quantity/date facts. Suggestions are recorded strings
    (the title, the subject's recorded name) — never invented copy.
    Sections the project never recorded report not_recorded."""
    project_dir = _project_dir(project_id)
    bible = _read_json(project_dir / "case_bible.json")
    bible_state = "recorded" if isinstance(bible, dict) else "not_recorded"

    subject = None
    facts: list[dict] = []
    story_type = None
    if isinstance(bible, dict):
        story_type = bible.get("story_type")
        subject_id = bible.get("primary_subject_id")
        for entity in bible.get("entities") or []:
            if isinstance(entity, dict) and entity.get("id") == subject_id:
                subject = {
                    "name": entity.get("canonical"),
                    "type": entity.get("type"),
                    "role": entity.get("role"),
                }
                break
        fact_table = bible.get("script_facts") or {}
        for fact in fact_table.get("facts") or []:
            if not isinstance(fact, dict):
                continue
            if fact.get("kind") in ("quantity", "date") and fact.get("display"):
                facts.append(
                    {
                        "kind": fact.get("kind"),
                        "display": fact.get("display"),
                        "sentence_number": fact.get("sentence_number"),
                    }
                )
            if len(facts) >= 12:
                break

    suggestions: list[str] = []
    for candidate in (project_title, (subject or {}).get("name")):
        if candidate and str(candidate).strip() and str(candidate) not in suggestions:
            suggestions.append(str(candidate))

    return {
        "title": project_title,
        "category": project_category,
        "story_type": story_type,
        "case_bible_state": bible_state,
        "primary_subject": subject,
        "recorded_facts": facts,
        "headline_suggestions": suggestions,
    }


# ---------------------------------------------------------------------
# Composition (PIL — the Motion Graphics toolkit)
# ---------------------------------------------------------------------


def _validate_headline(headline) -> str:
    text = " ".join(str(headline or "").split())
    if not text:
        raise ThumbnailError(
            422, "headline_required", "A headline is required."
        )
    words = len(text.split())
    if words > MAX_HEADLINE_WORDS or len(text) > MAX_HEADLINE_CHARS:
        raise ThumbnailError(
            422, "headline_too_long",
            f"Thumbnail headlines are kept to at most "
            f"{MAX_HEADLINE_WORDS} words / {MAX_HEADLINE_CHARS} "
            f"characters so the text stays legible at small sizes; "
            f"this one has {words} words / {len(text)} characters. "
            f"Shorten it — a thumbnail headline names the film in a "
            f"few words; it is not a sentence.",
        )
    return text


def _fit_headline(draw, text: str, max_width: int):
    """(font, lines) at the largest size whose wrapped lines fit
    max_width in at most MAX_HEADLINE_LINES lines. Refuses when
    even the floor size cannot fit — never overflows silently."""
    for size in range(96, 35, -4):
        font = motion_core.load_font(FONT_BOLD, size)
        lines = motion_core.wrap_text(draw, text.upper(), font, max_width)
        if len(lines) <= MAX_HEADLINE_LINES and all(
            draw.textlength(line, font=font) <= max_width for line in lines
        ):
            return font, lines, size
    raise ThumbnailError(
        422, "headline_does_not_fit",
        "The headline does not fit the layout even at the smallest "
        "legible size. Shorten it.",
    )


def _gradient_scrim(size, region_top: int, region_bottom: int,
                    top_alpha: int, bottom_alpha: int) -> Image.Image:
    """A vertical black gradient over [region_top, region_bottom)."""
    width, height = size
    strip = Image.new("L", (1, region_bottom - region_top))
    for y in range(region_bottom - region_top):
        fraction = y / max(1, (region_bottom - region_top) - 1)
        strip.putpixel((0, y), int(top_alpha + (bottom_alpha - top_alpha) * fraction))
    alpha = strip.resize((width, region_bottom - region_top))
    scrim = Image.new("RGBA", size, (0, 0, 0, 0))
    scrim.paste(Image.new("RGBA", (width, region_bottom - region_top), (0, 0, 0, 255)),
                (0, region_top), alpha)
    return scrim


def _relative_luminance(rgb) -> float:
    def channel(value):
        c = value / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(float(v)) for v in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _compose(base_image: Image.Image, headline: str, layout: str,
             accent_rgb) -> tuple[Image.Image, dict]:
    """Render one composition. Returns (image, facts) where facts
    carries the measured text geometry the checks are computed
    from: the union bbox of the drawn headline lines and the font
    size used."""
    canvas = motion_core.fit_cover(base_image.convert("RGB"), (THUMB_WIDTH, THUMB_HEIGHT))
    draw_probe = ImageDraw.Draw(canvas)
    facts: dict = {}

    if layout == "left_panel":
        panel_width = 470
        font, lines, size = _fit_headline(draw_probe, headline, panel_width - 72)
        line_height = int(size * 1.18)
        block_height = line_height * len(lines)
        panel = Image.new("RGBA", (THUMB_WIDTH, THUMB_HEIGHT), (0, 0, 0, 0))
        panel_draw = ImageDraw.Draw(panel)
        panel_draw.rectangle([0, 0, panel_width, THUMB_HEIGHT], fill=(14, 14, 17, 242))
        panel_draw.rectangle([0, 0, 10, THUMB_HEIGHT], fill=accent_rgb + (255,))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), panel).convert("RGB")
        draw = ImageDraw.Draw(canvas)
        y = (THUMB_HEIGHT - block_height) // 2
        x = 44
        boxes = []
        for line in lines:
            draw.text((x, y), line, font=font, fill=TEXT_RGB)
            boxes.append((x, y, x + draw.textlength(line, font=font), y + size))
            y += line_height
        facts["text_box"] = (
            min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes),
        )
        facts["font_size"] = size
        return canvas, facts

    # Band / scrim layouts share the horizontal headline block.
    font, lines, size = _fit_headline(draw_probe, headline, BADGE_SAFE_TEXT_WIDTH - 88)
    line_height = int(size * 1.18)
    block_height = line_height * len(lines)
    margin = 44

    if layout == "bottom_band":
        region_top = THUMB_HEIGHT - block_height - margin * 2 - 18
        scrim = _gradient_scrim(
            (THUMB_WIDTH, THUMB_HEIGHT), max(0, region_top), THUMB_HEIGHT, 0, 205
        )
        canvas = Image.alpha_composite(canvas.convert("RGBA"), scrim).convert("RGB")
        text_y = THUMB_HEIGHT - block_height - margin
        accent_y = text_y - 22
    elif layout == "top_band":
        region_bottom = block_height + margin * 2 + 18
        scrim = _gradient_scrim(
            (THUMB_WIDTH, THUMB_HEIGHT), 0, min(THUMB_HEIGHT, region_bottom), 205, 0
        )
        canvas = Image.alpha_composite(canvas.convert("RGBA"), scrim).convert("RGB")
        text_y = margin
        accent_y = text_y + block_height + 12
    else:  # center_scrim
        scrim = Image.new("RGBA", (THUMB_WIDTH, THUMB_HEIGHT), (0, 0, 0, 110))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), scrim).convert("RGB")
        text_y = (THUMB_HEIGHT - block_height) // 2
        accent_y = text_y - 22

    draw = ImageDraw.Draw(canvas)
    draw.rectangle([margin, accent_y, margin + 120, accent_y + 8], fill=accent_rgb)
    boxes = []
    y = text_y
    for line in lines:
        draw.text(
            (margin, y), line, font=font, fill=TEXT_RGB,
            stroke_width=max(2, size // 18), stroke_fill=(0, 0, 0),
        )
        boxes.append((margin, y, margin + draw.textlength(line, font=font), y + size))
        y += line_height
    facts["text_box"] = (
        min(b[0] for b in boxes), min(b[1] for b in boxes),
        max(b[2] for b in boxes), max(b[3] for b in boxes),
    )
    facts["font_size"] = size
    return canvas, facts


def _mechanical_checks(image: Image.Image, facts: dict, headline: str,
                       file_size: int, image_format: str) -> dict:
    """Measurements of the finished file — labelled mechanical
    checks, never performance claims (see module docstring)."""
    x0, y0, x1, y1 = (int(v) for v in facts["text_box"])
    region = image.crop((max(0, x0 - 6), max(0, y0 - 6),
                         min(THUMB_WIDTH, x1 + 6), min(THUMB_HEIGHT, y1 + 6)))
    mean_rgb = np.asarray(region, dtype=float).reshape(-1, 3).mean(axis=0)
    backing_luminance = _relative_luminance(mean_rgb)
    text_luminance = _relative_luminance(TEXT_RGB)
    lighter, darker = max(text_luminance, backing_luminance), min(text_luminance, backing_luminance)
    contrast = (lighter + 0.05) / (darker + 0.05)
    return {
        "dimensions": [image.size[0], image.size[1]],
        "format": image_format,
        "file_size_bytes": file_size,
        "under_youtube_size_limit": file_size < YOUTUBE_MAX_BYTES,
        "headline_words": len(headline.split()),
        "headline_characters": len(headline),
        "headline_contrast_ratio": round(float(contrast), 2),
        "headline_font_height_px_at_10_percent_scale": round(facts["font_size"] * 0.1, 1),
    }


def compose_thumbnail(project_id, base_id, headline, layout="bottom_band",
                      accent="amber", project_title=None) -> dict:
    """Compose, measure, persist and record one variant."""
    if layout not in LAYOUTS:
        raise ThumbnailError(
            422, "unknown_layout",
            f"Layout must be one of: {', '.join(LAYOUTS)}.",
        )
    if accent not in ACCENTS:
        raise ThumbnailError(
            422, "unknown_accent",
            f"Accent must be one of: {', '.join(ACCENTS)}.",
        )
    text = _validate_headline(
        headline if headline is not None else project_title
    )
    base_path, base_record = resolve_base(project_id, base_id)
    try:
        with Image.open(base_path) as img:
            base_image = img.copy()
    except (OSError, ValueError) as error:
        raise ThumbnailError(
            422, "base_unreadable",
            "The base image could not be read as an image.",
        ) from error

    image, facts = _compose(base_image, text, layout, ACCENTS[accent])

    thumb_id = _new_id("thumb")
    store_dir = _store_dir(project_id)
    store_dir.mkdir(parents=True, exist_ok=True)
    png_path = store_dir / f"{thumb_id}.png"
    image.save(png_path, format="PNG")
    file_size = png_path.stat().st_size
    image_format = "PNG"
    final_path = png_path
    if file_size >= YOUTUBE_MAX_BYTES:
        jpg_path = store_dir / f"{thumb_id}.jpg"
        image.save(jpg_path, format="JPEG", quality=90)
        png_path.unlink(missing_ok=True)
        final_path = jpg_path
        file_size = jpg_path.stat().st_size
        image_format = "JPEG"

    checks = _mechanical_checks(image, facts, text, file_size, image_format)
    variant = {
        "id": thumb_id,
        "created_at": _now_iso(),
        "file_name": final_path.name,
        "base": base_record,
        "base_id": base_id,
        "headline": text,
        "layout": layout,
        "accent": accent,
        "checks": checks,
    }
    store = _load_store(project_id)
    store.setdefault("variants", []).append(variant)
    _save_store(project_id, store)
    return {**variant, "available": True}


def generate_variants(project_id, headline=None, project_title=None) -> dict:
    """The one-action variant set: up to two bases x the four
    layout families. A combination that honestly cannot be made
    (e.g. the headline fits no layout) is reported in `skipped`
    with its reason — never silently dropped, never faked."""
    bases = list_bases(project_id)
    ordered: list[dict] = []
    ordered.extend(bases["stills"][:1])
    ordered.extend(bases["film"]["frames"][:1])
    ordered.extend(bases["uploads"][:1])
    ordered.extend(bases["stills"][1:2])
    ordered.extend(bases["film"]["frames"][1:2])
    ordered = ordered[:2]
    if not ordered:
        raise ThumbnailError(
            404, "no_bases",
            "This project has no recorded material to compose from "
            "yet: no completed production (film frames / verified "
            "stills) and no uploaded images.",
        )
    text = _validate_headline(
        headline if headline is not None else project_title
    )
    created: list[dict] = []
    skipped: list[dict] = []
    for base in ordered:
        for layout in LAYOUTS:
            try:
                created.append(
                    compose_thumbnail(
                        project_id, base["base_id"], text,
                        layout=layout, project_title=project_title,
                    )
                )
            except ThumbnailError as error:
                skipped.append(
                    {
                        "base_id": base["base_id"],
                        "layout": layout,
                        "code": error.code,
                        "message": error.message,
                    }
                )
    return {"created": created, "skipped": skipped}


# ---------------------------------------------------------------------
# Reference analysis — measured properties only (audit doc 03 §2)
# ---------------------------------------------------------------------


def analyse_image(project_id, base_id) -> dict:
    """Deterministic signals for one base image (an uploaded
    reference thumbnail is an upload base): OCR text + the engine
    that read it, the dominant palette with shares, mean luminance
    and its spread. Labelled measured properties — this is NOT an
    AI reading of composition, hierarchy or emotion; no such
    capability exists in this stack."""
    path, base_record = resolve_base(project_id, base_id)
    with Image.open(path) as img:
        rgb = img.convert("RGB")
        width, height = rgb.size
        small = rgb.copy()
        small.thumbnail((320, 320))
        quantised = small.quantize(colors=6, method=Image.MEDIANCUT)
        palette_counts = sorted(
            quantised.getcolors() or [], key=lambda c: c[0], reverse=True
        )
        total = sum(count for count, _idx in palette_counts) or 1
        palette_raw = quantised.getpalette() or []
        palette = []
        for count, index in palette_counts:
            r, g, b = palette_raw[index * 3: index * 3 + 3]
            palette.append(
                {
                    "hex": f"#{r:02x}{g:02x}{b:02x}",
                    "share": round(count / total, 3),
                }
            )
        grey = np.asarray(rgb.convert("L"), dtype=float)
        luminance_mean = round(float(grey.mean()) / 255.0, 3)
        luminance_spread = round(float(grey.std()) / 255.0, 3)

    ocr_text, ocr_engine = frame_verification._ocr_for_frames(None, [str(path)])
    ocr_lines = [line for line in (ocr_text or "").splitlines() if line.strip()]
    return {
        "base": base_record,
        "base_id": base_id,
        "label": (
            "Measured properties of this image (OCR text, colour "
            "palette, luminance). This is not an AI analysis of "
            "composition or emotion — no vision model exists in "
            "this stack."
        ),
        "dimensions": [width, height],
        "ocr": {
            "engine": ocr_engine,
            "state": "recorded" if ocr_text is not None else "unavailable",
            "lines": ocr_lines,
            "word_count": len((ocr_text or "").split()),
        },
        "palette": palette,
        "luminance": {"mean": luminance_mean, "spread": luminance_spread},
    }


# ---------------------------------------------------------------------
# AI headline drafting — llm_client only, proposals only
# ---------------------------------------------------------------------


def ai_available() -> bool:
    return bool(getattr(llm_client, "BREWKEG_API_KEY", None))


def suggest_headlines_ai(project_id, project_title=None,
                         project_category=None) -> dict:
    """Draft headline proposals from the recorded brief through
    llm_client. Proposals only: nothing is composed or stored by
    this action. 503 when no provider is configured, 502 when it
    fails (the Script Studio's contract) — never canned text."""
    if not ai_available():
        raise ThumbnailError(
            503, "thumbnail_ai_unavailable",
            "Headline drafting is unavailable: no LLM provider is "
            "configured on this deployment.",
        )
    brief = build_brief(project_id, project_title, project_category)
    subject = (brief.get("primary_subject") or {}).get("name")
    facts = [f["display"] for f in brief.get("recorded_facts") or []]
    system = (
        "You draft YouTube thumbnail headlines for documentary "
        "films. Rules: at most 6 words each; original wording; use "
        "ONLY the facts you are given — invent nothing, promise "
        "nothing the film does not contain, no clickbait falsehoods."
    )
    user = (
        f"Film title: {project_title or 'untitled'}\n"
        f"Category: {project_category or brief.get('story_type') or 'documentary'}\n"
        f"Primary subject: {subject or 'not recorded'}\n"
        f"Recorded facts: {', '.join(facts) if facts else 'none recorded'}\n"
        'Return JSON: {"headlines": ["...", "...", "..."]}'
    )
    try:
        data = llm_client.llm_json(system, user)
    except RuntimeError as error:
        if "BREWKEG_API_KEY is not configured" in str(error):
            raise ThumbnailError(
                503, "thumbnail_ai_unavailable",
                "Headline drafting is unavailable: no LLM provider "
                "is configured on this deployment.",
            ) from error
        raise ThumbnailError(
            502, "thumbnail_ai_failed", f"The AI provider failed: {error}"
        ) from error
    headlines = []
    for item in (data or {}).get("headlines") or []:
        text = " ".join(str(item).split())
        if text and len(text.split()) <= MAX_HEADLINE_WORDS and len(text) <= MAX_HEADLINE_CHARS:
            headlines.append(text)
    return {"proposals": headlines[:3], "basis": "llm_draft_from_recorded_brief"}


def studio_view(project_id, project_title=None, project_category=None) -> dict:
    """The whole workspace in one read: brief, bases, variants."""
    return {
        "schema": "cinemora.thumbnails/1",
        "project_id": str(project_id),
        "spec": {
            "width": THUMB_WIDTH,
            "height": THUMB_HEIGHT,
            "max_bytes": YOUTUBE_MAX_BYTES,
            "layouts": list(LAYOUTS),
            "accents": list(ACCENTS),
            "max_headline_words": MAX_HEADLINE_WORDS,
            "max_headline_characters": MAX_HEADLINE_CHARS,
        },
        "brief": build_brief(project_id, project_title, project_category),
        "bases": list_bases(project_id),
        "variants": list_variants(project_id),
    }
