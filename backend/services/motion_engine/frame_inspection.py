"""Frame inspection: pre-delivery VISUAL validation for rendered
graphics (Motion Graphics Programme Phase 5, gap X3).

Technical validation (exists / decodes / 1920x1080 / duration)
cannot see layout. The founder-judged films shipped graphics
whose defects were purely visual — a one-word card filling a
9-second slot, labels truncated at the frame edge, headlines
overflowing their boxes into the next step — and every one
passed. This module inspects a rendered frame with PIL and
flags the gross defect classes those films exhibited:

  near_empty_frame
      A full-frame graphic whose content occupies a tiny
      fraction of the frame (the closing_card failure shape:
      one word on an empty field for seconds of screen time).
  content_clipped_at_frame_edge
      Content ink reaches the frame's outer edge in short,
      text-height runs — the signature of a label truncated at
      the edge (the milestone_waveform failure shape). Long
      structural runs (full-height grid lines, vignette edges,
      full-bleed photography) are not clipping and are excluded.
  text_line_overrun
      A text-height band whose ink forms one near-continuous
      line across most of the frame width, built from separated
      word clusters — adjacent boxes' headlines running into
      each other (the process failure shape). Solid graphic
      rules/axes are excluded: their ink is one continuous run.

Thresholds were calibrated against the programme's evidence
frames (cinemora-audit/motion-graphics-programme/evidence/):
every closing_card frame scores an ink fraction under 0.009
with small-print glyphs only, while a legitimate sparse title
carries display-size type (see NEAR_EMPTY_* below); the
waveform frame is the only designed frame with edge ink; the
process frame's merged headline spans 82% of the frame width
at text height while no acceptable frame's text does.

The inspection is deliberately conservative: it flags gross
defects only. Fine layout judgement stays with label-fit
(services.motion_engine.label_fit) at render time and with
human frame inspection in the test suite.
"""

import io
import subprocess
from collections import Counter, deque
from pathlib import Path

from PIL import Image

# Analysis resolution: defect geometry is gross, so a quarter-
# scale frame is plenty and keeps the check fast enough to run
# on every registered graphic.
ANALYSIS_SIZE = (480, 270)

#: A pixel counts as content ("ink") when its per-channel
#: absolute difference from the frame's modal (background)
#: colour sums above this. Texture noise in the engine's
#: textured backgrounds sums to ~42 and stays below it.
INK_DIFF_THRESHOLD = 60

#: Below this content fraction a full-frame graphic may be a
#: near-empty card — but fraction alone cannot tell a one-word
#: filler card from a legitimate sparse graphic (a rendered
#: archival_title "Coffee" scores 0.008, inside the closing_card
#: range 0.003–0.008; a two-item checklist scores 0.009). Two
#: further conditions separate them: the filler card's largest
#: glyph is small print (12px at analysis scale, vs display
#: type at 17px+), and its content huddles in one small cluster
#: (bounding box 5–7% of the frame, vs ~20% for the checklist's
#: spread of title + counter + rows). A frame is near-empty
#: only when ALL THREE hold.
NEAR_EMPTY_MAX_FRACTION = 0.010
NEAR_EMPTY_MAX_COMPONENT_HEIGHT = 14
NEAR_EMPTY_MAX_BBOX_FRACTION = 0.10

#: Edge-clipping is only judged on designed (non-photographic)
#: frames: above this content fraction the frame is full-bleed
#: imagery, where edge ink is the picture, not a clipped label.
EDGE_CHECK_MAX_FRACTION = 0.35

#: Ink pixels in one edge band (counted over runs of text-like
#: length only) at/above this count as clipped content.
EDGE_MIN_PIXELS = 4

#: A row/column whose ink covers at least this fraction of the
#: frame is a full-bleed design element, not clipped content.
EDGE_FULLBLEED_COVERAGE = 0.35

#: A run along an edge longer than this fraction of the frame
#: dimension is structural (grid line, vignette, rule), never a
#: clipped glyph.
EDGE_RUN_MAX_FRACTION = 0.25

#: Text-line overrun geometry: a horizontally-merged ink blob
#: at least this wide, within this height band (fractions of
#: the frame), made of separated clusters with no single solid
#: run dominating — merged adjacent labels, not a drawn rule.
OVERRUN_MIN_WIDTH_FRACTION = 0.70
OVERRUN_MIN_HEIGHT_FRACTION = 0.010
OVERRUN_MAX_HEIGHT_FRACTION = 0.065
OVERRUN_MAX_RUN_FRACTION = 0.35
OVERRUN_MIN_CLUSTERS = 4
_OVERRUN_DILATION = 6


def _ink_mask(image):
    """(mask, width, height, ink_fraction) for an RGB image:
    mask is a bytearray of 1 where a pixel differs from the
    frame's modal colour by more than INK_DIFF_THRESHOLD."""
    small = image.convert("RGB").resize(ANALYSIS_SIZE)
    width, height = small.size
    pixels = small.load()
    flat = [pixels[x, y] for y in range(height) for x in range(width)]
    bg = Counter(flat).most_common(1)[0][0]
    mask = bytearray(width * height)
    count = 0
    for i, (r, g, b) in enumerate(flat):
        if (abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2])
                > INK_DIFF_THRESHOLD):
            mask[i] = 1
            count += 1
    return mask, width, height, count / float(width * height)


def _largest_component_height(mask, width, height):
    """Height (px, analysis scale) of the tallest connected ink
    component — glyphs, rules and shapes alike."""
    seen = bytearray(width * height)
    best = 0
    for start in range(width * height):
        if not mask[start] or seen[start]:
            continue
        queue = deque([start])
        seen[start] = 1
        min_y = max_y = start // width
        while queue:
            cell = queue.popleft()
            cx, cy = cell % width, cell // width
            min_y = min(min_y, cy)
            max_y = max(max_y, cy)
            for nxt in (cell - 1, cell + 1, cell - width, cell + width):
                if (0 <= nxt < width * height and mask[nxt]
                        and not seen[nxt]
                        and abs(nxt % width - cx) <= 1):
                    seen[nxt] = 1
                    queue.append(nxt)
        best = max(best, max_y - min_y + 1)
    return best


def _edge_band_defects(mask, width, height):
    """Clipped-content defects from the four edge bands.

    For the left/right bands a "run" is a vertical run of ink in
    one band column; for top/bottom, a horizontal run in one
    band row. Runs shorter than 3px are noise; runs longer than
    EDGE_RUN_MAX_FRACTION of the dimension are structural and
    ignored. What remains is text-height ink touching the edge:
    a label the frame cut off.

    Full-bleed design rows/columns are excluded first: a row
    whose ink covers >= EDGE_FULLBLEED_COVERAGE of the width is
    a deliberate edge-to-edge element (the crime-scene caution
    band, a vignette edge), and ink in such a row is the design
    reaching the edge on purpose, not a label cut off by it."""
    defects = []
    row_full = [
        sum(mask[y * width:(y + 1) * width]) >= EDGE_FULLBLEED_COVERAGE * width
        for y in range(height)
    ]
    col_full = [
        sum(mask[y * width + x] for y in range(height))
        >= EDGE_FULLBLEED_COVERAGE * height
        for x in range(width)
    ]

    def _band_pixels(vertical, fixed):
        # Ink pixels along one edge line (a column for the
        # left/right bands, a row for top/bottom), counting only
        # runs of text-like length: >= 3px (below is noise) and
        # <= EDGE_RUN_MAX_FRACTION of the line's length (above
        # is a structural line, not a clipped glyph). Pixels in
        # full-bleed rows/columns break runs and never count.
        limit = height if vertical else width
        max_run = limit * EDGE_RUN_MAX_FRACTION
        total = 0
        run = 0
        for along in range(limit):
            idx = (along * width + fixed) if vertical else (
                fixed * width + along)
            # A pixel belongs to a full-bleed element when its
            # row spans the frame (a horizontal band) or its
            # column does (a vertical band) — either way it is
            # design reaching the edge, not clipped content.
            full = row_full[along if vertical else fixed] or (
                col_full[fixed if vertical else along])
            if mask[idx] and not full:
                run += 1
            else:
                if 3 <= run <= max_run:
                    total += run
                run = 0
        if 3 <= run <= max_run:
            total += run
        return total

    bands = (
        ("left", True, (0, 1)),
        ("right", True, (width - 2, width - 1)),
        ("top", False, (0, 1)),
        ("bottom", False, (height - 2, height - 1)),
    )
    for name, vertical, positions in bands:
        total = sum(
            _band_pixels(vertical, pos) for pos in positions
        )
        if total >= EDGE_MIN_PIXELS:
            defects.append({
                "type": "content_clipped_at_frame_edge",
                "edge": name,
                "detail": (
                    f"{total} content pixels in text-height runs "
                    f"touch the {name} frame edge — content is "
                    "clipped by the frame"
                ),
            })
    return defects


def _overrun_defects(mask, width, height):
    """Merged-headline defects: horizontally dilate the ink so
    the words of one text line join into a single blob, then
    look for a text-height blob spanning most of the frame whose
    own ink is separated clusters (words), not one solid run
    (a drawn rule or axis)."""
    dilated = bytearray(width * height)
    for y in range(height):
        base = y * width
        run = 0
        for x in range(width):
            if mask[base + x]:
                run = _OVERRUN_DILATION + 1
            if run > 0:
                dilated[base + x] = 1
                run -= 1
    seen = bytearray(width * height)
    defects = []
    for start in range(width * height):
        if not dilated[start] or seen[start]:
            continue
        queue = deque([start])
        seen[start] = 1
        min_x = max_x = start % width
        min_y = max_y = start // width
        while queue:
            cell = queue.popleft()
            cx, cy = cell % width, cell // width
            min_x = min(min_x, cx)
            max_x = max(max_x, cx)
            min_y = min(min_y, cy)
            max_y = max(max_y, cy)
            for nxt in (cell - 1, cell + 1, cell - width, cell + width):
                if (0 <= nxt < width * height and dilated[nxt]
                        and not seen[nxt]
                        and abs(nxt % width - cx) <= 1):
                    seen[nxt] = 1
                    queue.append(nxt)
        blob_w = max_x - min_x + 1
        blob_h = max_y - min_y + 1
        if blob_w < OVERRUN_MIN_WIDTH_FRACTION * width:
            continue
        if not (OVERRUN_MIN_HEIGHT_FRACTION * height <= blob_h
                <= OVERRUN_MAX_HEIGHT_FRACTION * height):
            continue
        # The blob's own ink: count separated clusters and the
        # longest solid run inside its bounding box. A drawn
        # rule is one run; merged text is many clusters.
        clusters = 0
        longest = 0
        for y in range(min_y, max_y + 1):
            base = y * width
            run = 0
            for x in range(min_x, max_x + 1):
                if mask[base + x]:
                    run += 1
                else:
                    if run > 0:
                        clusters += 1
                        longest = max(longest, run)
                    run = 0
            if run > 0:
                clusters += 1
                longest = max(longest, run)
        if (clusters >= OVERRUN_MIN_CLUSTERS
                and longest < OVERRUN_MAX_RUN_FRACTION * blob_w):
            defects.append({
                "type": "text_line_overrun",
                "detail": (
                    f"text-height ink band spans {blob_w}/{width} "
                    "of the frame width in separated clusters — "
                    "adjacent labels have run into each other"
                ),
            })
    return defects


def inspect_frame(image):
    """Inspect one rendered frame (a PIL Image or an image file
    path) for gross visual defects.

    Returns {"passed": bool, "defects": [...], "metrics":
    {"ink_fraction": float, "background": (r, g, b)}}."""
    if isinstance(image, (str, Path)):
        image = Image.open(image)
    mask, width, height, fraction = _ink_mask(image)
    defects = []
    metrics = {"ink_fraction": round(fraction, 4)}
    if fraction < NEAR_EMPTY_MAX_FRACTION:
        tallest = _largest_component_height(mask, width, height)
        xs = [i % width for i in range(width * height) if mask[i]]
        ys = [i // width for i in range(width * height) if mask[i]]
        bbox_fraction = (
            ((max(xs) - min(xs) + 1) / width)
            * ((max(ys) - min(ys) + 1) / height)
            if xs else 0.0
        )
        metrics["largest_component_height"] = tallest
        metrics["content_bbox_fraction"] = round(bbox_fraction, 4)
        if (tallest <= NEAR_EMPTY_MAX_COMPONENT_HEIGHT
                and bbox_fraction < NEAR_EMPTY_MAX_BBOX_FRACTION):
            defects.append({
                "type": "near_empty_frame",
                "detail": (
                    f"content occupies {fraction:.3%} of the frame "
                    f"(< {NEAR_EMPTY_MAX_FRACTION:.1%}), its largest "
                    f"element is {tallest}px tall and it all sits "
                    f"in {bbox_fraction:.1%} of the frame — a "
                    "full-frame graphic with almost nothing in it"
                ),
            })
    if fraction <= EDGE_CHECK_MAX_FRACTION:
        defects.extend(_edge_band_defects(mask, width, height))
    defects.extend(_overrun_defects(mask, width, height))
    return {
        "passed": not defects,
        "defects": defects,
        "metrics": metrics,
    }


def extract_frame(path, at_fraction=0.7):
    """One settled frame of a rendered video as a PIL Image, or
    None when ffmpeg cannot produce one. `at_fraction` samples
    late in the clip, when entrance animations have settled —
    an early frame legitimately shows a graphic mid-reveal."""
    try:
        from services.motion_engine.core import probe_duration

        duration = probe_duration(path)
    except Exception:
        duration = 0.0
    at = max(0.0, duration * at_fraction) if duration else 0.0
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{at:.3f}",
             "-i", str(path), "-frames:v", "1",
             "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        return Image.open(io.BytesIO(result.stdout)).convert("RGB")
    except Exception:
        return None


def inspect_video(path, at_fraction=0.7):
    """Visual verdict for one rendered graphic file: extract a
    settled frame and inspect it. When no frame can be
    extracted the verdict is {"passed": None, ...} — unknown,
    never a silent pass and never a fabricated failure; the
    caller decides how to treat an unavailable inspection (the
    graphics pipeline records it and does not block on tooling
    failure, since technical validation already passed)."""
    frame = extract_frame(path, at_fraction=at_fraction)
    if frame is None:
        return {
            "passed": None,
            "defects": [],
            "metrics": {},
            "detail": "frame extraction failed; visual inspection "
                      "unavailable",
        }
    verdict = inspect_frame(frame)
    verdict["detail"] = ""
    return verdict
