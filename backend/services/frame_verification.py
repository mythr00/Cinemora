"""Frame-level visual verification (Transformation Plan Phase 1.1).

Until this module, verification was metadata-only: the verifier's
``visual`` dimension was recorded ``not_evaluated`` under a standing
order of the previous era (AM-028 — VA-01/VA-02 excluded). The
founder's Complete Autonomous Takeover Mandate (2026-11-21)
supersedes that carve-out — it orders inspection of the actual
footage — so the visual dimension is now EVALUATED: every candidate
that survives metadata verification and is downloaded gets its
sampled frames judged here, post-download, exactly where the
transcript check (Stage 3B) already demotes assets whose spoken
words betray them. Metadata-time verdicts keep ``not_evaluated``
for the visual dimension — no frame exists at that point, and the
value is never inferred — and this stage replaces it with ``pass``
or ``fail`` on the downloaded asset's verdict copy.

Every detector is deterministic and explainable (no model-based
judging): each rejection is a typed reason in the closed vocabulary
(services.rejection_diagnostics) with the measured evidence on the
record, so the asset report's ledger can explain the exclusion.

Detectors, in evaluation order (first rejection wins; every
detector's evidence is recorded regardless):

1. ``social_story_card`` — a social-media story card: many centred
   text lines, regularly pitched, on a dark block (the run-9
   Turkey search-party card). Social/meme formats are BANNED by
   standing founder ruling; this is the frame-level half of that
   ban (the metadata half is story_understanding's
   ``social_platform_source`` / ``meme_format_source``).
2. ``social_caption_overlay`` — burned-in social captions (the
   run-9 TikTok "POV:" clip): a few large centred text lines on a
   uniform backing, persistent across the sampled frames. The
   bottom band is excluded — news lower-thirds and this pipeline's
   own caption position live there and are legitimate.
3. ``meme_format_frame`` — the classic meme layout: near-uniform
   bars at the top and bottom edges with text lines inside them.
4. ``channel_card_frame`` — a channel's branding/title card burned
   into the clip (the run-9 "Marron True Crime" card over the Moab
   bodycam beat): display-size text in the central band that is
   present in some sampled frames but not all — a transient overlay
   on real footage, i.e. a source intro/outro that was never
   trimmed. A card present in EVERY frame is recorded as evidence
   but not rejected (a static title still is a different class).
5. ``insufficient_frame_fill`` — a portrait/vertical source whose
   content fills too little of the 16:9 delivery frame (the run-9
   pillarboxed portraits). Where the download normalisation's
   blurred-fill treatment (media_pipeline.blurred_fill_filter_
   complex, run-9 fix 5) genuinely rescues the frame — the flanks
   are the blurred copy, not baked-in black bars — the asset passes
   with the treatment recorded. Baked-in black bars are not rescued
   by blurring the whole frame, and stills have no blurred-fill
   route in the render path (the renderer centre-crops them), so
   below-threshold sources of those kinds are rejected.
6. Geography contradiction — frame-visible signage text, read by
   the OCR provider chain, fed through the SAME gazetteer rule as
   metadata (story_understanding. detect_contradiction's
   geography class): the production establishes exactly one
   country and the frames positively name exactly one different
   country. The chain's primary engine is RapidOCR
   (rapidocr-onnxruntime, installed in the project venv — the
   founder delegated the OCR choice, 2026-11-22); pytesseract
   and a tesseract binary remain as fallbacks. The engine that
   produced the text is recorded on every frame-check record
   (``ocr_engine``) and in the geography sub-record. Only when
   NO engine in the chain is usable does the sub-check record
   ``not_evaluated`` with ``ocr_engine_unavailable`` — never
   inferred, never guessed. OCR is bounded to the frames this
   stage already samples, downscaled to ``OCR_MAX_WIDTH`` (see
   the provider seam below for the measured costs).

Watermark detection is RECORD ONLY (founder ruling 2026-11-22,
commit 6d408d6): watermarked stock is ALLOWED. A frame-level
watermark detection stamps ``watermarked_preview`` on the verdict
— the same field the metadata path stamps — as a provenance
signal. It never rejects, and the asset is never labelled clean
on this evidence. The frame detector claims a watermark only on
strong evidence (a large soft text line on a locally smooth
backing — the stock-preview norm); over a busy photographic
backing it honestly stays silent and the metadata path's
agency-based stamp covers the asset.

The stage is fail-soft, like the transcript check: an error (no
frames sampleable, an unreadable file) records ``not_evaluated``
with the cause and the asset proceeds — a verification outage must
never wedge a production, and the record says the dimension was
not evaluated rather than pretending it passed.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

try:  # package import (backend runs as a package in the suite)
    from services import media_probe
    from services import story_understanding as _story
except ImportError:  # flat execution (scripts run inside services/)
    import media_probe
    import story_understanding as _story


# ---------------------------------------------------------------------------
# Vocabulary (registered in services.rejection_diagnostics)
# ---------------------------------------------------------------------------

FRAME_STAGE = "frame_verification"

SOCIAL_CAPTION_REASON = "social_caption_overlay"
SOCIAL_STORY_CARD_REASON = "social_story_card"
MEME_FORMAT_FRAME_REASON = "meme_format_frame"
CHANNEL_CARD_REASON = "channel_card_frame"
INSUFFICIENT_FILL_REASON = "insufficient_frame_fill"
FROZEN_SEGMENT_REASON = "frozen_segment"
# Geography reuses the metadata contradiction class verbatim:
# story_understanding.CONTRADICTORY_GEOGRAPHY.

#: The authority under which the visual dimension is evaluated at
#: all — recorded on every frame-check record so the flip out of
#: AM-028's exclusion is auditable from the artefacts.
AUTHORITY = (
    "takeover_mandate_2026-11-21 / transformation_plan_phase_1_1; "
    "supersedes AM-028 (visual dimension excluded)"
)

# ---------------------------------------------------------------------------
# Detector thresholds. These are DETECTOR thresholds (Phase 1.1's
# own knobs) — the lexical (token-overlap) similarity thresholds (0.55/0.34/0.22) and reuse
# caps are a different, untouched system. Every value here is
# recorded on the frame-check record's ``thresholds`` block so a
# tuning change is visible in the artefacts it affected.
# ---------------------------------------------------------------------------

ANALYSIS_WIDTH = 640

# OCR cost bound: frames wider than this are downscaled before
# OCR (the geography sub-check reads signage, not fine print).
# Measured on this machine: a 2400px still costs 2.2-3.5s of
# RapidOCR time native vs ~1.2s at 1280px, with identical text;
# the sampled video frames are already 640px and pass through
# untouched. OCR never sees more than the frames this stage
# already sampled (3 per video, 1 per image).
OCR_MAX_WIDTH = 1280

# Stroke detection: |pixel - local blur| above this marks a hard
# stroke (text edge/fill against its immediate background).
STROKE_MIN_CONTRAST = 26.0
# Glyph CORES: the high-contrast heart of a glyph stroke. Cores
# stay separate per glyph even when a caption's drop shadow merges
# the soft strokes into one blob — line detection counts runs on
# the core mask, not the soft mask (measured on the run-9 corpus:
# the POV caption's soft mask is one 137px run per row; its core
# mask is 15-22 separate runs per text row).
CORE_CONTRAST = 90.0
# Display text (channel cards) is large: its strokes are wide, so
# the blur spreads its edge contrast thinner (measured ~70 peak
# for white display type on a mid-tone ground, vs ~120 for small
# caption type on black). The card scan seeds on a lower core
# threshold; its glyph range (>= 0.085 of frame height), width
# and transience requirements carry the precision instead.
CARD_CORE_CONTRAST = 45.0
BLUR_RADIUS = 3

# A text line's glyph height, as a fraction of frame height.
CAPTION_GLYPH_MIN = 0.018
CAPTION_GLYPH_MAX = 0.085
# Display-size text (channel cards / logos).
CARD_GLYPH_MIN = 0.085
# A display-text LINE taller than 30% of the frame is not a glyph
# line — it is a scene stratum whose seed rows chained to the cap
# (measured on the run-9 driveway clip: full-width bands at
# exactly the old 0.40 cap, stroke density ~0.03).
CARD_GLYPH_MAX = 0.30
# Text lines keep margins; a "line" spanning more than 92% of the
# frame width edge-to-edge is a scene band, not type.
CARD_MAX_WIDTH_FRAC = 0.92
CARD_MIN_WIDTH_FRAC = 0.45

# Lines whose centre sits in the bottom band are excluded from the
# caption/story-card rules (news lower-thirds; this pipeline's own
# caption position in rendered frames).
BOTTOM_BAND_FRACTION = 0.85

# Backing uniformity behind a text line (non-stroke pixels in the
# expanded line box): standard deviation and mean bounds. The std
# bound is set from the run-9 corpus: the POV clip's text sits on a
# dark photographic backing whose measured std is ~23-27 (a pure
# black backing would be ~0; a busy scene region measures 45+).
BACKING_STD_MAX = 30.0
BACKING_DARK_MEAN_MAX = 85.0
BACKING_LIGHT_MEAN_MIN = 170.0

# Story card: at least this many centred, regularly pitched lines
# on a block whose background is dark.
STORY_CARD_MIN_LINES = 7
STORY_CARD_BLOCK_MEAN_MAX = 120.0
STORY_CARD_MIN_LEFT_FRAC = 0.10
STORY_CARD_MAX_LINE_WIDTH_FRAC = 0.92
STORY_CARD_MAX_PITCH_RATIO = 2.6

# Caption overlay: centred lines on a uniform backing.
CAPTION_MIN_LINES_VIDEO = 2
CAPTION_MIN_LINES_IMAGE = 3
CAPTION_CENTER_MIN = 0.30
CAPTION_CENTER_MAX = 0.70

# Meme bars: near-uniform bands at both frame edges.
MEME_BAR_HEIGHT_FRAC = 0.055
MEME_BAR_TOLERANCE = 14.0
MEME_BAR_PIXEL_FRACTION = 0.70
MEME_LINE_MIN_DENSITY = 0.03

# Frame fill: the fraction of the 1920x1080 delivery frame the
# source's content occupies under a contain fit. Below FILL_MIN a
# source is rejected unless the blurred-fill treatment rescues it.
# 0.34 sits just above the 9:16 class (0.316 — the run-9 pillarbox
# defect) and just below a 2:3 still (0.375 — a legitimate, if
# crop-heavy, documentary still).
FILL_MIN = 0.34
DELIVERY_WIDTH = 1920
DELIVERY_HEIGHT = 1080
# Flank classification on sampled video frames.
BLACK_COLUMN_MEAN_MAX = 16.0
BLURRED_HF_RATIO = 0.30

# Motion (Gate 4 fix, 2026-11-26, inspection defect B5): a VIDEO
# segment whose sampled frames (0.12 / 0.50 / 0.88 of the segment,
# services.media_probe.SAMPLE_FRAME_FRACTIONS) show essentially no
# change between ANY consecutive pair is a still passed off as
# footage — in the Hyatt run the sentence-4 "video" window was
# frozen for its entire 4.05 s (measured frame diffs 0.00–0.37 on a
# 0–255 scale, against 2.8–3.8 for genuinely moving footage in the
# same film). The ceiling sits between those two measured classes.
# Mean absolute grayscale difference is computed on the analysis
# raster (ANALYSIS_WIDTH), the same raster every other detector
# reads. Images are exempt by construction (kind != "video"), and a
# segment with fewer than two sampled frames is not judged
# (absence of evidence stays not_evaluated via frames_sampled).
MOTION_FROZEN_MAX_DIFF = 1.0

# Watermark (record only): large, soft (low-contrast) text lines.
# The width cap keeps document text rows (which span the page)
# from masquerading as a watermark word.
WATERMARK_GLYPH_MIN = 0.07
WATERMARK_GLYPH_MAX = 0.38
WATERMARK_MIN_WIDTH_FRAC = 0.20
WATERMARK_MAX_WIDTH_FRAC = 0.85
WATERMARK_CONTRAST_MIN = 8.0
WATERMARK_CONTRAST_MAX = 110.0

_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


def thresholds_snapshot():
    """The detector thresholds in force, for the record."""
    return {
        "stroke_min_contrast": STROKE_MIN_CONTRAST,
        "caption_glyph_range": [CAPTION_GLYPH_MIN, CAPTION_GLYPH_MAX],
        "card_core_contrast": CARD_CORE_CONTRAST,
        "card_max_width_frac": CARD_MAX_WIDTH_FRAC,
        "card_glyph_min": CARD_GLYPH_MIN,
        "card_min_width_frac": CARD_MIN_WIDTH_FRAC,
        "bottom_band_fraction": BOTTOM_BAND_FRACTION,
        "backing_std_max": BACKING_STD_MAX,
        "story_card_min_lines": STORY_CARD_MIN_LINES,
        "story_card_block_mean_max": STORY_CARD_BLOCK_MEAN_MAX,
        "caption_min_lines_video": CAPTION_MIN_LINES_VIDEO,
        "caption_min_lines_image": CAPTION_MIN_LINES_IMAGE,
        "gap_density_max": GAP_DENSITY_MAX,
        "meme_bar_height_frac": MEME_BAR_HEIGHT_FRAC,
        "meme_bar_pixel_fraction": MEME_BAR_PIXEL_FRACTION,
        "meme_bar_tolerance": MEME_BAR_TOLERANCE,
        "meme_line_min_density": MEME_LINE_MIN_DENSITY,
        "fill_min": FILL_MIN,
        "motion_frozen_max_diff": MOTION_FROZEN_MAX_DIFF,
        "black_column_mean_max": BLACK_COLUMN_MEAN_MAX,
        "blurred_hf_ratio": BLURRED_HF_RATIO,
        "watermark_contrast_range": [
            WATERMARK_CONTRAST_MIN, WATERMARK_CONTRAST_MAX,
        ],
        "watermark_glyph_range": [WATERMARK_GLYPH_MIN, WATERMARK_GLYPH_MAX],
        "watermark_backing_std_max": WATERMARK_BACKING_STD_MAX,
        "ocr_max_width": OCR_MAX_WIDTH,
    }


# ---------------------------------------------------------------------------
# Frame analysis (pure: PIL image in, measurements out)
# ---------------------------------------------------------------------------

def _gray_and_blur(image):
    """The analysis raster: grayscale at ANALYSIS_WIDTH plus its
    local-background estimate (a small gaussian blur)."""
    img = image.convert("L")
    if img.width != ANALYSIS_WIDTH:
        height = max(1, round(img.height * ANALYSIS_WIDTH / img.width))
        img = img.resize((ANALYSIS_WIDTH, height), Image.BILINEAR)
    gray = np.asarray(img, dtype=np.float32)
    blur = np.asarray(
        img.filter(ImageFilter.GaussianBlur(BLUR_RADIUS)), dtype=np.float32
    )
    return gray, blur


def _row_profile(mask, *, min_runs, min_run_len, max_run):
    """Per-row text-bearing flags and spans from a stroke mask: a
    row is text-bearing when it carries at least ``min_runs`` short
    runs (glyph strokes are short; photographic texture runs long
    or merges)."""
    height = mask.shape[0]
    padded = np.concatenate(
        [np.zeros((height, 1), bool), mask, np.zeros((height, 1), bool)],
        axis=1,
    )
    diff = np.diff(padded.astype(np.int8), axis=1)
    row_ok = np.zeros(height, bool)
    row_span = [None] * height
    for y in range(height):
        starts = np.flatnonzero(diff[y] == 1)
        if len(starts) == 0:
            continue
        ends = np.flatnonzero(diff[y] == -1)
        lengths = ends - starts
        keep = (lengths >= min_run_len) & (lengths <= max_run)
        if int(keep.sum()) >= min_runs:
            row_ok[y] = True
            row_span[y] = (int(starts[keep][0]), int(ends[keep][-1]))
    return row_ok, row_span


def _detect_lines(gray, mask, *, glyph_min, glyph_max, min_runs=4,
                  min_run_len=2, ok_fraction=0.5, gap_tolerance=2,
                  dilate=False, exclude_mask=None, grow_mask=None,
                  grow_min_runs=3, min_seed_rows=1):
    """Group stroke-mask rows into text-line bands.

    Bands form over SEED rows (``mask`` rows with enough short
    runs), then expand over adjacent GROW rows (``grow_mask``, a
    softer stroke mask): small glyphs at analysis scale seed on
    only a few core rows, but their soft strokes span the full
    glyph height, so the grow profile restores the true band
    height. A band whose height is in the glyph range and which
    carries enough seed rows is a line. Each line records its
    geometry and the uniformity of the backing immediately around
    it — the backing excludes ``exclude_mask`` pixels (default:
    the seed mask) so a glyph's own soft body never counts as its
    background.
    """
    height, width = mask.shape
    max_run = max(2, int(0.10 * width))
    if dilate:
        # Bridge 1px strokes (small soft text at analysis scale).
        # Both shifts read the ORIGINAL mask — an in-place cascade
        # would grow every stroke across the row.
        dilated = mask.copy()
        dilated[:, 1:] |= mask[:, :-1]
        dilated[:, :-1] |= mask[:, 1:]
        mask = dilated
    if exclude_mask is None:
        exclude_mask = mask
    row_ok, row_span = _row_profile(
        mask, min_runs=min_runs, min_run_len=min_run_len, max_run=max_run
    )
    row_grow = None
    if grow_mask is not None:
        row_grow, grow_span = _row_profile(
            grow_mask, min_runs=grow_min_runs,
            min_run_len=2, max_run=max_run,
        )
        for y in range(height):
            if row_span[y] is None:
                row_span[y] = grow_span[y]

    # Raw bands form over SEED rows only; then each band expands
    # over adjacent grow rows (the glyph's soft extent) up to the
    # glyph-height cap. Grow rows never start a band — a soft
    # profile alone is far too permissive (measured: 144 of 360
    # rows on an ordinary frame).
    bands = []
    y = 0
    while y < height:
        if not row_ok[y]:
            y += 1
            continue
        y0 = y
        y1 = y
        gap = 0
        cursor = y
        while cursor + 1 < height and gap <= gap_tolerance:
            if row_ok[cursor + 1]:
                y1 = cursor + 1
                gap = 0
            else:
                gap += 1
            cursor += 1
        if row_grow is not None:
            max_h = int(glyph_max * height)
            while (
                y0 > 0 and row_grow[y0 - 1] and (y1 - y0 + 1) < max_h
            ):
                y0 -= 1
            while (
                y1 + 1 < height and row_grow[y1 + 1]
                and (y1 - y0 + 1) < max_h
            ):
                y1 += 1
        bands.append([y0, y1])
        y = y1 + 1

    def _span(band):
        spans = [row_span[r] for r in range(band[0], band[1] + 1) if row_span[r]]
        if not spans:
            return (0, 0)
        return (min(s[0] for s in spans), max(s[1] for s in spans))

    lines = []
    for y0, y1 in bands:
        band_h = y1 - y0 + 1
        glyph_frac = band_h / height
        ok_rows = int(row_ok[y0:y1 + 1].sum())
        if not (
            glyph_min <= glyph_frac <= glyph_max
            and ok_rows >= ok_fraction * band_h
            and ok_rows >= min_seed_rows
        ):
            continue
        x0, x1 = _span([y0, y1])
        # Backing: non-stroke pixels in the box expanded by
        # ~0.6 glyph heights above and below the line.
        pad = int(0.6 * band_h) + 1
        by0 = max(0, y0 - pad)
        by1 = min(height, y1 + pad + 1)
        region = gray[by0:by1, x0:x1]
        region_mask = exclude_mask[by0:by1, x0:x1]
        background = region[~region_mask]
        if background.size:
            bg_mean = float(background.mean())
            bg_std = float(background.std())
        else:
            bg_mean, bg_std = 0.0, 999.0
        lines.append({
            "y0": y0, "y1": y1,
            "stroke_density": round(
                float(mask[y0:y1 + 1, x0:x1].mean()), 4
            ),
            "height_frac": round(glyph_frac, 4),
            "x0_frac": round(x0 / width, 4),
            "x1_frac": round(x1 / width, 4),
            "width_frac": round((x1 - x0) / width, 4),
            "center_x_frac": round(((x0 + x1) / 2) / width, 4),
            "center_y_frac": round(((y0 + y1) / 2) / height, 4),
            "backing_mean": round(bg_mean, 1),
            "backing_std": round(bg_std, 1),
            "uniform_dark": bool(
                bg_std <= BACKING_STD_MAX and bg_mean <= BACKING_DARK_MEAN_MAX
            ),
            "uniform_light": bool(
                bg_std <= BACKING_STD_MAX
                and bg_mean >= BACKING_LIGHT_MEAN_MIN
            ),
        })
    return lines


def _uniform_bar(gray, top):
    """Whether the frame's edge band is a near-uniform bar (meme
    letterbox). The bar's own caption text sits INSIDE the band,
    so row uniformity fails exactly where the format is present;
    the test is instead the fraction of band pixels at the bar's
    modal value — a true bar is one flat colour with sparse text
    on it, while a photographic edge band (sky, wall) spreads its
    pixels across a gradient."""
    height = gray.shape[0]
    band_h = max(2, int(MEME_BAR_HEIGHT_FRAC * height))
    band = gray[:band_h] if top else gray[height - band_h:]
    median = float(np.median(band))
    fraction = float((np.abs(band - median) <= MEME_BAR_TOLERANCE).mean())
    return fraction >= MEME_BAR_PIXEL_FRACTION


def _edge_dead_run(dead, from_left):
    """Length of the contiguous dead-column run at one frame edge
    (gaps of up to 2 live columns tolerated — compression noise)."""
    count = 0
    gap = 0
    indexes = range(len(dead)) if from_left else range(len(dead) - 1, -1, -1)
    for i in indexes:
        if dead[i]:
            count += 1
            gap = 0
        else:
            gap += 1
            if gap > 2:
                break
            count += 1
    # Trailing tolerated-gap columns are not dead; trim them.
    return max(0, count - gap) if gap else count


def _frame_fill(gray):
    """Content fill of one sampled video frame.

    Returns {fill, flank} where flank classifies the dead columns
    at the edges: "black" (baked-in bars), "blurred" (the blurred-
    fill treatment's copies), "mixed" or "none". A frame that is
    dark or flat overall cannot be judged and reports fill 1.0 —
    darkness is a lighting fact, not a pillarbox.
    """
    height, width = gray.shape
    col_mean = gray.mean(axis=0)
    # Per-column high-frequency energy, aligned to columns (the
    # gradient's first value stands in for the first column).
    gradient = np.abs(np.diff(gray, axis=1)).mean(axis=0)
    col_hf = np.concatenate([[gradient[0]], gradient]) if gradient.size else gradient
    # A frame whose CENTRE is itself near-black is a genuinely dark
    # scene (night footage) — darkness there is lighting, not a
    # pillarbox, and the flanks cannot be judged. (The frame-wide
    # median cannot be the guard: a pillarboxed frame is mostly
    # black BY CONSTRUCTION, so its median is black too.)
    if float(col_mean[width // 3: 2 * width // 3].mean()) < BLACK_COLUMN_MEAN_MAX:
        return {"fill": 1.0, "flank": "none"}
    centre = col_hf[width // 3: 2 * width // 3]
    centre_hf = float(np.median(centre)) if centre.size else 0.0
    if centre_hf <= 1e-6:
        return {"fill": 1.0, "flank": "none"}
    black = col_mean < BLACK_COLUMN_MEAN_MAX
    blurred = (col_hf < BLURRED_HF_RATIO * centre_hf) & ~black
    dead = black | blurred
    left = _edge_dead_run(dead, True)
    right = _edge_dead_run(dead, False)
    if left + right >= width:
        return {"fill": 1.0, "flank": "none"}
    fill = 1.0 - (left + right) / width
    dead_indexes = list(range(left)) + list(range(width - right, width))
    if not dead_indexes:
        flank = "none"
    else:
        black_share = float(black[dead_indexes].mean())
        blurred_share = float(blurred[dead_indexes].mean())
        if black_share >= 0.7:
            flank = "black"
        elif blurred_share >= 0.7:
            flank = "blurred"
        else:
            flank = "mixed"
    return {"fill": round(fill, 4), "flank": flank}


def analyse_frame(image):
    """All per-frame measurements the rules aggregate over."""
    gray, blur = _gray_and_blur(image)
    contrast = np.abs(gray - blur)
    hard_mask = contrast >= STROKE_MIN_CONTRAST
    core_mask = contrast >= CORE_CONTRAST
    # Text lines are counted on glyph CORES (see CORE_CONTRAST):
    # small glyphs cross many core runs on only some of their rows,
    # so the band rules are looser than for soft strokes.
    lines = _detect_lines(
        gray, core_mask,
        glyph_min=CAPTION_GLYPH_MIN, glyph_max=CAPTION_GLYPH_MAX,
        min_runs=5, min_run_len=1, ok_fraction=0.15, gap_tolerance=3,
        exclude_mask=hard_mask, grow_mask=hard_mask, min_seed_rows=2,
    )
    card_lines = _detect_lines(
        gray, contrast >= CARD_CORE_CONTRAST,
        glyph_min=CARD_GLYPH_MIN, glyph_max=CARD_GLYPH_MAX,
        min_runs=5, min_run_len=1, ok_fraction=0.15, gap_tolerance=3,
        exclude_mask=hard_mask, grow_mask=hard_mask, min_seed_rows=2,
    )
    soft_mask = (
        (contrast >= WATERMARK_CONTRAST_MIN)
        & (contrast <= WATERMARK_CONTRAST_MAX)
    )
    watermark_lines = _detect_lines(
        gray, soft_mask,
        glyph_min=WATERMARK_GLYPH_MIN, glyph_max=WATERMARK_GLYPH_MAX,
    )
    for line in watermark_lines:
        x0 = int(line["x0_frac"] * gray.shape[1])
        x1 = int(line["x1_frac"] * gray.shape[1]) + 1
        region = contrast[line["y0"]:line["y1"] + 1, x0:x1]
        soft = region[soft_mask[line["y0"]:line["y1"] + 1, x0:x1]]
        line["mean_contrast"] = round(float(soft.mean()), 1) if soft.size else 0.0
        # The watermark's defining softness: NOWHERE among the
        # line's stroke pixels does the contrast reach hard-text
        # levels. A real caption (white on black) has soft
        # anti-aliased edges too, but its stroke cores are hard —
        # the p95 over stroke pixels separates them.
        stroke_vals = region[region >= WATERMARK_CONTRAST_MIN]
        line["p95_contrast"] = (
            round(float(np.percentile(stroke_vals, 95)), 1)
            if stroke_vals.size else 0.0
        )
    return {
        "lines": lines,
        "card_lines": card_lines,
        "watermark_lines": watermark_lines,
        "mask": hard_mask,
        "meme_bars": {
            "top": _uniform_bar(gray, True),
            "bottom": _uniform_bar(gray, False),
        },
        "fill": _frame_fill(gray),
    }


# ---------------------------------------------------------------------------
# Rules over the sampled frames
# ---------------------------------------------------------------------------

def _above_bottom(lines):
    return [l for l in lines if l["center_y_frac"] <= BOTTOM_BAND_FRACTION]


# Inter-line gap stroke density, recorded as evidence on the
# story-card block. It was designed as a discriminator (text floats
# on a stroke-free backing; texture is stroke-dense in the gaps
# too), but the run-9 corpus refuted it: the story card's panel is
# translucent over a forest photograph, so its gaps measure as
# dense as any texture (0.34). It gates nothing; the text rules
# stand on core-line formation, centredness, backing uniformity
# and persistence instead.
GAP_DENSITY_MAX = 0.05


def _gap_density(analysis, lines):
    """Stroke density in the gaps between a block of lines, within
    the block's x-span."""
    if len(lines) < 2:
        return 0.0
    mask = analysis["mask"]
    height, width = mask.shape
    x0 = int(min(l["x0_frac"] for l in lines) * width)
    x1 = int(max(l["x1_frac"] for l in lines) * width)
    in_line = np.zeros(height, bool)
    for line in lines:
        in_line[line["y0"]:line["y1"] + 1] = True
    y0 = min(l["y0"] for l in lines)
    y1 = max(l["y1"] for l in lines)
    gap_rows = [y for y in range(y0, y1 + 1) if not in_line[y]]
    if not gap_rows:
        return 0.0
    return float(mask[gap_rows][:, x0:x1 + 1].mean())


def _story_card_evidence(analysis):
    """The story-card block in one frame, or None: >= STORY_CARD_
    MIN_LINES centred lines above the bottom band, regularly
    pitched, on a dark block background."""
    lines = _above_bottom(analysis["lines"])
    if len(lines) < STORY_CARD_MIN_LINES:
        return None
    lefts = sorted(l["x0_frac"] for l in lines)
    widths = sorted(l["width_frac"] for l in lines)
    median_left = lefts[len(lefts) // 2]
    median_width = widths[len(widths) // 2]
    if median_left < STORY_CARD_MIN_LEFT_FRAC:
        return None
    if median_width > STORY_CARD_MAX_LINE_WIDTH_FRAC:
        return None
    heights = sorted(l["height_frac"] for l in lines)
    median_h = heights[len(heights) // 2]
    centres = [l["center_y_frac"] for l in lines]
    gaps = [b - a for a, b in zip(centres, centres[1:])]
    regular = (
        sum(1 for g in gaps if g <= STORY_CARD_MAX_PITCH_RATIO * median_h)
        >= 0.7 * len(gaps)
    ) if gaps else False
    if not regular:
        return None
    block_mean = float(np.mean([l["backing_mean"] for l in lines]))
    if block_mean > STORY_CARD_BLOCK_MEAN_MAX:
        return None
    return {
        "line_count": len(lines),
        "median_left_frac": median_left,
        "median_width_frac": median_width,
        "block_backing_mean": round(block_mean, 1),
        # Recorded, not gated: a story card's panel is often
        # translucent over photography, so its inter-line gaps are
        # as stroke-dense as the texture it floats on (measured
        # 0.34 on the run-9 card) — the gap statistic does not
        # separate the class and is evidence only.
        "gap_density": round(_gap_density(analysis, lines), 4),
    }


def _caption_lines(analysis):
    """Centred, uniformly backed caption lines above the bottom
    band in one frame."""
    return [
        l for l in _above_bottom(analysis["lines"])
        if (l["uniform_dark"] or l["uniform_light"])
        and CAPTION_CENTER_MIN <= l["center_x_frac"] <= CAPTION_CENTER_MAX
    ]


def _justified_block(lines):
    """Whether a line set is DOCUMENT text rather than captions:
    wide lines (median width >= 0.60 of frame) sharing one
    straight margin (left or right edge std <= 0.012). Captions
    are centred, so both edges wander as line lengths vary;
    document columns align at least one edge. Measured on the
    run-9 corpus: the autopsy report's right margin std is 0.0014
    at width 0.95; the POV caption's edges wander at 0.023-0.027
    at width ~0.19."""
    if len(lines) < 3:
        return False
    widths = sorted(l["width_frac"] for l in lines)
    if widths[len(widths) // 2] < 0.60:
        return False

    def _pstdev(values):
        mean = sum(values) / len(values)
        return (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5

    return (
        _pstdev([l["x0_frac"] for l in lines]) <= 0.012
        or _pstdev([l["x1_frac"] for l in lines]) <= 0.012
    )


def _card_lines(analysis):
    """Display-size lines in the central band (channel cards)."""
    return [
        l for l in analysis["card_lines"]
        if CARD_MIN_WIDTH_FRAC <= l["width_frac"] <= CARD_MAX_WIDTH_FRAC
        and 0.25 <= l["center_y_frac"] <= 0.75
    ]


def _meme_evidence(analysis):
    bars = analysis["meme_bars"]
    if not (bars["top"] and bars["bottom"]):
        return None
    in_bars = [
        l for l in analysis["lines"]
        if (l["center_y_frac"] <= 0.16 or l["center_y_frac"] >= 0.84)
        # A caption line is made of glyph strokes (the run-9 POV
        # lines measure 0.27 stroke density in their box). Sparse
        # strata in a bordered photograph can ghost into the line
        # scan at ~0.01 — they are not text and must not convert a
        # white-margined photo into a "meme".
        and l["stroke_density"] >= MEME_LINE_MIN_DENSITY
    ]
    if not in_bars:
        return None
    return {"bar_lines": len(in_bars)}


# A frame watermark is only claimed where the evidence is strong:
# the soft line must sit on a locally SMOOTH backing (a watermark
# over sky, wall or a studio backdrop — the stock-preview norm).
# Over a busy photographic backing, deterministic soft-text
# detection cannot separate a watermark from scene texture, and
# this detector honestly stays silent; the metadata path's
# agency-based stamp (story_understanding.is_watermarked_stock)
# covers those assets with the same field.
WATERMARK_BACKING_STD_MAX = 20.0


def _watermark_evidence(analyses):
    """Record-only: a large soft text line spanning the frame's
    centre, on a smooth backing, in any sampled frame."""
    for index, analysis in enumerate(analyses):
        for line in analysis["watermark_lines"]:
            if (
                WATERMARK_MIN_WIDTH_FRAC
                <= line["width_frac"]
                <= WATERMARK_MAX_WIDTH_FRAC
                and 0.15 <= line["center_y_frac"] <= 0.85
                and line.get("p95_contrast", 999.0) <= WATERMARK_CONTRAST_MAX
                and line.get("backing_std", 999.0) <= WATERMARK_BACKING_STD_MAX
            ):
                return {
                    "detected": True,
                    "frame": index,
                    "width_frac": line["width_frac"],
                    "height_frac": line["height_frac"],
                    "mean_contrast": line.get("mean_contrast"),
                    "p95_contrast": line.get("p95_contrast"),
                    "backing_std": line.get("backing_std"),
                }
    return {"detected": False}


def _fill_decision(analyses, kind, native_size):
    """The fill verdict: {fill, flank, treatment, fired}.

    Images judge by native geometry (the downloaded still keeps
    its real dimensions); videos judge by the sampled frames of
    the normalised clip, where baked-in bars and the blurred-fill
    treatment are both visible.
    """
    if kind == "image" and native_size:
        w, h = native_size
        if w and h:
            scale = min(DELIVERY_WIDTH / w, DELIVERY_HEIGHT / h)
            fill = (w * scale) * (h * scale) / (DELIVERY_WIDTH * DELIVERY_HEIGHT)
            fill = round(fill, 4)
            if fill < FILL_MIN:
                # Stills have no blurred-fill route: the renderer
                # centre-crops them (zoompan), which silently
                # amputates a portrait subject. Reject.
                return {
                    "fill": fill, "flank": "none",
                    "treatment": "none_available", "fired": True,
                }
            return {
                "fill": fill, "flank": "none",
                "treatment": "native" if fill >= 0.9 else "renderer_crop",
                "fired": False,
            }
    fills = sorted(a["fill"]["fill"] for a in analyses)
    median_fill = fills[len(fills) // 2]
    flanks = [a["fill"]["flank"] for a in analyses]
    flank = max(set(flanks), key=flanks.count)
    if median_fill < FILL_MIN:
        if flank == "blurred":
            # The download normalisation's blurred-fill treatment
            # already rescued this source: the flanks are its own
            # blurred copy, not black bars. Pass, recorded.
            return {
                "fill": median_fill, "flank": flank,
                "treatment": "blurred_fill", "fired": False,
            }
        return {
            "fill": median_fill, "flank": flank,
            "treatment": "none_available", "fired": True,
        }
    return {
        "fill": median_fill, "flank": flank,
        "treatment": "blurred_fill" if flank == "blurred" else "native",
        "fired": False,
    }


def _geography_record(ocr_text, scenario, bible, ocr_engine=None):
    """The geography sub-check. OCR text, when a provider produced
    any, goes through the metadata gazetteer rule verbatim. The
    producing engine is recorded with the verdict; ``engine`` is
    None exactly when no engine produced text (the
    ``ocr_engine_unavailable`` case)."""
    if ocr_text is None:
        return {
            "outcome": "not_evaluated",
            "detail": "ocr_engine_unavailable",
            "engine": None,
        }
    if not scenario or not bible:
        return {
            "outcome": "not_evaluated",
            "detail": "no_scenario_or_bible",
            "engine": ocr_engine,
        }
    frame_countries = _story._countries_in_text(_story._norm(ocr_text))
    home = _story._scenario_countries(bible, scenario)
    record = {
        "outcome": "pass",
        "engine": ocr_engine,
        "frame_countries": sorted(frame_countries),
        "home_countries": sorted(home),
    }
    if (
        len(home) == 1
        and len(frame_countries) == 1
        and frame_countries.isdisjoint(home)
    ):
        record["outcome"] = "fail"
        record["reason"] = _story.CONTRADICTORY_GEOGRAPHY
        record["detail"] = (
            f"{next(iter(frame_countries))} vs {next(iter(home))}"
        )
    return record


def _motion_evidence(images, kind):
    """Consecutive-frame motion evidence for a video segment
    (Gate 4 fix, defect B5 — see MOTION_FROZEN_MAX_DIFF).

    Returns {"diffs": [...], "max_diff": float|None, "fired": bool}.
    Fires only for kind == "video" with at least two sampled frames
    whose grayscale rasters share a shape, when EVERY consecutive
    pair differs by less than the frozen ceiling — i.e. no sampled
    moment of the segment moves. A single moving pair is enough to
    pass: the check condemns only wholly static segments (a still
    photograph or a frozen broadcast frame served as footage),
    never slow or subtle motion."""
    evidence = {"diffs": [], "max_diff": None, "fired": False}
    if kind != "video" or len(images) < 2:
        return evidence
    rasters = []
    for image in images:
        gray, _blur = _gray_and_blur(image)
        rasters.append(gray)
    shape = rasters[0].shape
    if any(r.shape != shape for r in rasters):
        return evidence
    diffs = [
        float(np.mean(np.abs(rasters[i + 1] - rasters[i])))
        for i in range(len(rasters) - 1)
    ]
    evidence["diffs"] = [round(d, 3) for d in diffs]
    evidence["max_diff"] = round(max(diffs), 3) if diffs else None
    evidence["fired"] = bool(diffs) and max(diffs) < MOTION_FROZEN_MAX_DIFF
    return evidence


def verify_frames(images, *, kind="video", scenario=None, bible=None,
                  ocr_text=None, ocr_engine=None, native_size=None):
    """Judge sampled frames (PIL images) — the pure core.

    Returns the frame-check record: outcome pass / reject /
    not_evaluated, the typed reason when rejected, every detector's
    evidence, and the record-only watermark fact. Never raises for
    content reasons; programming errors propagate to the caller's
    fail-soft wrapper.
    """
    record = {
        "stage": FRAME_STAGE,
        "authority": AUTHORITY,
        "outcome": "not_evaluated",
        "reason": "",
        "frames_sampled": len(images),
        "kind": kind,
        "ocr_engine": ocr_engine,
        "thresholds": thresholds_snapshot(),
        "detectors": {},
        "watermarked_preview": False,
    }
    if not images:
        record["detail"] = "no_frames_sampled"
        return record

    analyses = [analyse_frame(image) for image in images]

    # 1. Story card (any frame — the card is static).
    story = None
    for index, analysis in enumerate(analyses):
        story = _story_card_evidence(analysis)
        if story:
            story = {"frame": index, **story}
            break
    record["detectors"]["social_story_card"] = story or {"line_count": 0}

    # 2. Caption overlay (persistent across frames for video).
    # A frame whose line set is a justified document block does
    # not count — documents are legitimate documentary material.
    caption_counts = []
    justified_frames = 0
    for analysis in analyses:
        frame_lines = _caption_lines(analysis)
        if _justified_block(frame_lines):
            justified_frames += 1
            caption_counts.append(0)
        else:
            caption_counts.append(len(frame_lines))
    if kind == "image":
        caption_fired = caption_counts[0] >= CAPTION_MIN_LINES_IMAGE
        caption_needed = 1
    else:
        caption_needed = max(1, (len(analyses) + 1) // 2)
        caption_fired = sum(
            1 for c in caption_counts if c >= CAPTION_MIN_LINES_VIDEO
        ) >= caption_needed
    record["detectors"]["social_caption_overlay"] = {
        "line_counts": caption_counts,
        "justified_block_frames": justified_frames,
        "frames_needed": caption_needed,
        "fired": caption_fired,
    }

    # 3. Meme bars (any frame).
    meme = None
    for index, analysis in enumerate(analyses):
        meme = _meme_evidence(analysis)
        if meme:
            meme = {"frame": index, **meme}
            break
    record["detectors"]["meme_format"] = meme or {"bar_lines": 0}

    # 4. Channel card: display text in SOME but not all frames.
    card_counts = [len(_card_lines(a)) for a in analyses]
    card_fired = (
        kind == "video"
        and len(analyses) >= 2
        and any(c > 0 for c in card_counts)
        and not all(c > 0 for c in card_counts)
    )
    record["detectors"]["channel_card"] = {
        "line_counts": card_counts,
        "static_card": bool(card_counts and all(c > 0 for c in card_counts)),
        "fired": card_fired,
    }

    # 5. Fill.
    fill = _fill_decision(analyses, kind, native_size)
    record["detectors"]["frame_fill"] = fill

    # Watermark (record only — never a rejection).
    watermark = _watermark_evidence(analyses)
    record["detectors"]["watermark"] = watermark
    record["watermarked_preview"] = bool(watermark.get("detected"))

    # 6. Geography (OCR-dependent).
    geography = _geography_record(ocr_text, scenario, bible, ocr_engine)
    record["detectors"]["geography"] = geography

    # 7. Motion (Gate 4 fix, defect B5): a wholly static video
    # segment is a still passed off as footage. Two static shapes
    # are explained by design and stay recorded-not-rejected, per
    # the phase-1 contracts: a static channel card (the asset's own
    # title still, channel_card.static_card) and blurred-fill
    # treatment (the download normalisation's own frame). The B5
    # defect — a full-frame news segment with neither explanation,
    # frozen for its whole window — still fires.
    motion = _motion_evidence(images, kind)
    if motion["fired"] and (
        record["detectors"]["channel_card"].get("static_card")
        or fill.get("treatment") == "blurred_fill"
    ):
        motion["fired"] = False
        motion["explained_static"] = True
    record["detectors"]["motion"] = motion

    # First rejection wins, in the documented order.
    if story:
        record["outcome"] = "reject"
        record["reason"] = SOCIAL_STORY_CARD_REASON
    elif caption_fired:
        record["outcome"] = "reject"
        record["reason"] = SOCIAL_CAPTION_REASON
    elif meme:
        record["outcome"] = "reject"
        record["reason"] = MEME_FORMAT_FRAME_REASON
    elif card_fired:
        record["outcome"] = "reject"
        record["reason"] = CHANNEL_CARD_REASON
    elif fill["fired"]:
        record["outcome"] = "reject"
        record["reason"] = INSUFFICIENT_FILL_REASON
    elif geography["outcome"] == "fail":
        record["outcome"] = "reject"
        record["reason"] = geography["reason"]
    elif motion["fired"]:
        record["outcome"] = "reject"
        record["reason"] = FROZEN_SEGMENT_REASON
    else:
        record["outcome"] = "pass"
    return record


# ---------------------------------------------------------------------------
# OCR provider seam (geography). A chain, tried in order:
#
# 1. RapidOCR (rapidocr-onnxruntime) — the engine installed in the
#    project venv (founder delegated the OCR choice, 2026-11-22;
#    smoke-tested on the run-9 film frames and the founder's
#    reference screenshots before wiring). Lazy-loaded as a
#    process-wide singleton: building it loads three ONNX models
#    (~1.3s measured on this machine), so importing this module —
#    and every metadata-only or stubbed path — pays nothing.
# 2. pytesseract, when the package is importable.
# 3. A ``tesseract`` binary on PATH.
#
# A provider that runs and finds no text returns "" (OCR ran; the
# frames carry no words). A provider that is unavailable or fails
# returns None and the chain falls through; when every provider
# returns None the geography sub-check records not_evaluated /
# ocr_engine_unavailable. Every step is fail-soft, like the rest
# of this module: an OCR failure must never wedge a production.
#
# Cost (measured on this VM, 1.5 shared CPUs): engine build ~1.3s
# once per process; per frame ~1.1s for sparse frames and ~2-3s
# for text-dense frames at the sampled 640px width; stills are
# downscaled to OCR_MAX_WIDTH first (a 2400px still: 2.2-3.5s
# native vs ~1.2s at 1280px, identical text). A video candidate
# therefore pays roughly 3.5-9s across its 3 sampled frames,
# inside the production's existing D-B2 wall-clock guard — the
# budgets themselves are unchanged.
# ---------------------------------------------------------------------------

OCR_ENGINE_RAPIDOCR = "rapidocr"
OCR_ENGINE_PYTESSERACT = "pytesseract"
OCR_ENGINE_TESSERACT = "tesseract"

_RAPIDOCR_ENGINE = None
_RAPIDOCR_UNAVAILABLE = False
# D-03/C-11: WHY the engine is unavailable (exception type + message),
# recorded when the build first fails so the degradation is explicit
# in-process, not just inferred downstream from ocr_engine_unavailable.
_RAPIDOCR_UNAVAILABLE_REASON = None
_RAPIDOCR_LOCK = threading.Lock()


def _rapidocr_engine():
    """The shared RapidOCR engine, built on first use. A failed
    build is cached: a broken install is not retried per
    candidate."""
    global _RAPIDOCR_ENGINE, _RAPIDOCR_UNAVAILABLE
    global _RAPIDOCR_UNAVAILABLE_REASON
    if _RAPIDOCR_ENGINE is not None or _RAPIDOCR_UNAVAILABLE:
        return _RAPIDOCR_ENGINE
    with _RAPIDOCR_LOCK:
        if _RAPIDOCR_ENGINE is None and not _RAPIDOCR_UNAVAILABLE:
            try:
                from rapidocr_onnxruntime import RapidOCR

                _RAPIDOCR_ENGINE = RapidOCR()
            except Exception as error:
                _RAPIDOCR_UNAVAILABLE = True
                _RAPIDOCR_UNAVAILABLE_REASON = (
                    f"{type(error).__name__}: {error}"
                )
                # Explicit degradation (D-03/C-11): the geography
                # record already stamps ocr_engine_unavailable, but
                # the failure itself was previously swallowed with no
                # trace of the cause. Log it once, when it is cached.
                print(
                    "[frame_verification] RapidOCR unavailable "
                    f"({_RAPIDOCR_UNAVAILABLE_REASON}); OCR degrades "
                    "to ocr_engine_unavailable / fallback providers."
                )
    return _RAPIDOCR_ENGINE


def _rapidocr_frame_lines(engine, frame_path):
    """The text lines RapidOCR reads in one frame, in reading
    order. Its raw result rows are [box, text, confidence]; only
    the text is kept (the engine's own score threshold has
    already dropped weak rows). Frames wider than OCR_MAX_WIDTH
    are downscaled first — see the cost note above."""
    with Image.open(frame_path) as img:
        if img.width > OCR_MAX_WIDTH:
            height = max(
                1, round(img.height * OCR_MAX_WIDTH / img.width)
            )
            array = np.asarray(
                img.convert("RGB").resize(
                    (OCR_MAX_WIDTH, height), Image.BILINEAR
                )
            )
            result, _elapse = engine(array)
        else:
            result, _elapse = engine(str(frame_path))
    if not result:
        return []
    return [
        str(item[1]) for item in result
        if isinstance(item, (list, tuple)) and len(item) >= 2 and item[1]
    ]


def _rapidocr_text(frame_paths):
    """Joined RapidOCR text for the frames (one detected line per
    line), or None when the engine is unavailable or fails — the
    chain then falls through to the legacy providers."""
    if not frame_paths:
        return None
    engine = _rapidocr_engine()
    if engine is None:
        return None
    try:
        lines = []
        for frame_path in frame_paths:
            lines.extend(_rapidocr_frame_lines(engine, frame_path))
        return "\n".join(lines)
    except Exception:
        return None


def _fallback_ocr_text(frame_paths):
    """(text, engine) from the legacy providers — pytesseract,
    then a tesseract binary — with the seam's original semantics:
    a pytesseract failure ends the chain (None), a missing
    pytesseract falls through to the binary."""
    try:
        import pytesseract

        texts = []
        for frame_path in frame_paths:
            with Image.open(frame_path) as img:
                texts.append(pytesseract.image_to_string(img))
        return "\n".join(texts), OCR_ENGINE_PYTESSERACT
    except ImportError:
        pass
    except Exception:
        return None, None
    binary = shutil.which("tesseract")
    if not binary:
        return None, None
    texts = []
    for frame_path in frame_paths:
        try:
            result = subprocess.run(
                [binary, str(frame_path), "stdout"],
                capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return None, None
        if result.returncode != 0:
            return None, None
        texts.append(result.stdout or "")
    return "\n".join(texts), OCR_ENGINE_TESSERACT


def _ocr_chain(frame_paths):
    """(text, engine_name) from the first provider that produces
    text; (None, None) when no provider does."""
    if not frame_paths:
        return None, None
    text = _rapidocr_text(frame_paths)
    if text is not None:
        return text, OCR_ENGINE_RAPIDOCR
    return _fallback_ocr_text(frame_paths)


def _default_ocr_provider(frame_paths):
    """The seam's original text-only contract (kept for callers
    that do not record the engine): the chain's text, or None."""
    return _ocr_chain(frame_paths)[0]


def _ocr_for_frames(provider, frame_paths):
    """(text, engine) for one asset's frames. A caller-supplied
    provider keeps the text-only contract and its engine is
    unrecorded (None); the default path runs the chain and
    reports which engine produced the text."""
    if provider is not None:
        return provider(frame_paths), None
    return _ocr_chain(frame_paths)


# ---------------------------------------------------------------------------
# Asset-level entry point (IO + fail-soft)
# ---------------------------------------------------------------------------

def _asset_kind(asset, path):
    kind = str(asset.get("type") or asset.get("visual_type") or "").lower()
    if kind in ("video", "image"):
        return kind
    if Path(path).suffix.lower() in _VIDEO_SUFFIXES:
        return "video"
    return "image"


def verify_asset_frames(asset, scenario=None, bible=None, *,
                        ocr_text_provider=None):
    """Run the frame stage over one downloaded asset's file.

    ``asset`` is the downloader's asset dict (file_path/path, type,
    probed width/height/duration). Returns the frame-check record.
    NEVER raises: any failure records ``not_evaluated`` with the
    cause (the transcript check's fail-soft discipline).
    """
    base = {
        "stage": FRAME_STAGE,
        "authority": AUTHORITY,
        "outcome": "not_evaluated",
        "reason": "",
        "frames_sampled": 0,
        "ocr_engine": None,
        "thresholds": thresholds_snapshot(),
        "detectors": {},
        "watermarked_preview": False,
    }
    try:
        path = asset.get("file_path") or asset.get("path")
        if not path or not Path(path).exists():
            base["detail"] = "asset_file_missing"
            return base
        kind = _asset_kind(asset, path)
        if kind == "image":
            with Image.open(path) as img:
                img.load()
                images = [img.copy()]
            ocr_text, ocr_engine = _ocr_for_frames(
                ocr_text_provider, [Path(path)]
            )
            native_size = None
            if asset.get("width") and asset.get("height"):
                native_size = (int(asset["width"]), int(asset["height"]))
            return verify_frames(
                images, kind="image", scenario=scenario, bible=bible,
                ocr_text=ocr_text, ocr_engine=ocr_engine,
                native_size=native_size,
            )
        work_dir = Path(tempfile.mkdtemp(prefix="cinemora-frames-"))
        try:
            frame_paths = media_probe.extract_sample_frames(
                path, work_dir,
                duration_seconds=asset.get("duration_seconds")
                or asset.get("clip_duration"),
            )
            images = []
            for frame_path in frame_paths:
                with Image.open(frame_path) as img:
                    img.load()
                    images.append(img.copy())
            ocr_text, ocr_engine = _ocr_for_frames(
                ocr_text_provider, frame_paths
            )
            record = verify_frames(
                images, kind="video", scenario=scenario, bible=bible,
                ocr_text=ocr_text, ocr_engine=ocr_engine,
            )
            # GitHub Skills Programme (Lane 2, 2026-11-25):
            # record-only content evidence — ffmpeg freeze/black
            # intervals, PySceneDetect shot boundaries and
            # perceptual hashes of the sampled frames. None of it
            # changes the verdict above; each piece fails soft
            # independently, exactly like the frame stage itself.
            from services import content_analysis, perceptual_hash
            from services import shot_detection

            record["content_analysis"] = (
                content_analysis.analyse_video_content(path)
            )
            record["shots"] = shot_detection.detect_shots(
                path,
                duration_seconds=asset.get("duration_seconds")
                or asset.get("clip_duration"),
            )
            try:
                record["frame_hashes"] = perceptual_hash.frame_hashes(images)
            except Exception as error:  # never wedge verification
                record["frame_hashes"] = {
                    "count": 0,
                    "reason": f"error: {type(error).__name__}: {error}",
                }
            return record
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
    except Exception as error:  # fail-soft: never wedge a production
        base["detail"] = f"error: {type(error).__name__}: {error}"
        return base
