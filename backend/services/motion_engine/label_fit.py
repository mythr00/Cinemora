"""Label fit: the motion engine's guarantee that text is never
rendered clipped, truncated or colliding (Motion Graphics
Programme Phase 5, gap X3).

Before this module, layout defects shipped in release-grade
films: timeline labels overlapped each other, process headlines
overflowed their boxes into the neighbouring step, and waveform
labels were truncated mid-word at the frame edge — every one of
them passed the purely technical output validation. The rule
now is:

  * A label is measured BEFORE it is drawn (PIL text metrics
    against the box it must live in).
  * A label that does not fit REFUSES the graphic with the typed
    reason ``label_does_not_fit`` — it is never drawn clipped,
    never truncated mid-word, never shrunk below the offered
    sizes, and never allowed to overlap another label's box.
  * Wrapping happens at word boundaries only; the full text is
    preserved across lines or the label does not fit at all.

Templates opt in by calling :func:`fit_lines` /
:func:`require_labels_fit` during their data-validation phase
(before any frame is drawn) and returning
``(None, {"reason": LABEL_FIT_REASON, ...})`` on refusal — the
engine's existing refusal contract. The graphics pipeline maps
that reason onto the MG-08 skip vocabulary unchanged.
"""

from services.motion_engine.core import wrap_text

#: The typed refusal reason, shared by templates (metadata),
#: the pipeline skip vocabulary (GRAPHIC_SKIP_REASONS) and the
#: editor's regeneration path.
LABEL_FIT_REASON = "label_does_not_fit"


class LabelFitError(ValueError):
    """Raised by require_* helpers when a label cannot fit.

    Carries the typed ``reason`` so a caller that prefers the
    exception form can convert it straight into the engine's
    (None, {"reason": ...}) refusal tuple."""

    reason = LABEL_FIT_REASON

    def __init__(self, detail, *, label=None):
        super().__init__(detail)
        self.detail = detail
        self.label = label


def measure(draw, text, font):
    """(width, height) in pixels of `text` rendered in `font`,
    measured with the draw context's own metrics."""
    text = str(text or "")
    if not text:
        return 0.0, 0.0
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return float(right - left), float(bottom - top)


def fit_lines(draw, text, font, max_width, max_lines=1):
    """Wrap `text` at word boundaries so every line fits
    `max_width`, using at most `max_lines` lines.

    Returns the list of lines, or None when the text does NOT
    fit: a single word wider than `max_width`, or a wrap needing
    more than `max_lines` lines. The full text is always
    preserved across the returned lines — this function never
    truncates and never splits a word."""
    text = " ".join(str(text or "").split())
    if not text:
        return []
    if max_width <= 0 or max_lines <= 0:
        return None
    for word in text.split():
        if draw.textlength(word, font=font) > max_width:
            return None
    lines = wrap_text(draw, text, font, max_width)
    if len(lines) > max_lines:
        return None
    return lines


def fit_font(draw, text, tokens, max_width, max_lines=1,
             sizes=(27, 22, 19), bold=True):
    """The strict counterpart of templates._fit_label: try each
    offered size (largest first) and return (lines, font) for the
    first size at which the FULL text fits, or None when no
    offered size fits — the caller refuses the graphic instead
    of overflowing the box at the smallest size."""
    from services.motion_engine.core import load_font

    text = " ".join(str(text or "").split())
    if not text:
        return [], load_font(
            tokens["font_bold"] if bold else tokens["font"], sizes[0])
    font_path = tokens["font_bold"] if bold else tokens["font"]
    for size in sizes:
        font = load_font(font_path, size)
        lines = fit_lines(draw, text, font, max_width, max_lines)
        if lines is not None:
            return lines, font
    return None


def boxes_overlap(a, b, gap=0.0):
    """True when (x0, y0, x1, y1) boxes a and b intersect (with
    `gap` pixels of required clearance)."""
    return not (
        a[2] + gap <= b[0] or b[2] + gap <= a[0]
        or a[3] + gap <= b[1] or b[3] + gap <= a[1]
    )


def layout_collisions(boxes, gap=0.0):
    """Collisions among labelled boxes.

    `boxes`: [{"key": ..., "box": (x0, y0, x1, y1)}, ...].
    Returns [{"a": key, "b": key}, ...] for every overlapping
    pair — the geometry-level collision check a renderer runs
    over the label boxes it is about to draw."""
    collisions = []
    entries = [b for b in boxes if b and b.get("box")]
    for i, first in enumerate(entries):
        for second in entries[i + 1:]:
            if boxes_overlap(first["box"], second["box"], gap):
                collisions.append({
                    "a": first.get("key"), "b": second.get("key"),
                })
    return collisions


def check_labels(draw, labels):
    """Fit defects for a set of labels about to be drawn.

    Each label: {"key", "text", "font", "max_width",
    "max_lines" (default 1), "box" (optional (x0, y0, x1, y1)
    the label's lines will occupy — collisions are checked
    between every pair of boxes)}.

    Returns a list of defect dicts (empty = every label fits and
    no two boxes collide):
      {"type": "label_overflow", "key", "detail"}
      {"type": "label_collision", "a", "b"}"""
    defects = []
    boxes = []
    for label in labels or []:
        if not isinstance(label, dict):
            continue
        key = label.get("key")
        text = " ".join(str(label.get("text") or "").split())
        if label.get("box") is not None:
            boxes.append({"key": key, "box": label["box"]})
        if not text:
            continue
        lines = fit_lines(
            draw, text, label["font"],
            label.get("max_width") or 0,
            label.get("max_lines", 1),
        )
        if lines is None:
            defects.append({
                "type": "label_overflow",
                "key": key,
                "detail": (
                    f"label {text!r} does not fit "
                    f"{label.get('max_width')}px in "
                    f"{label.get('max_lines', 1)} line(s)"
                ),
            })
    for collision in layout_collisions(boxes):
        defects.append({"type": "label_collision", **collision})
    return defects


def require_labels_fit(draw, labels):
    """Raise LabelFitError on the first fit defect in `labels`
    (see check_labels); return the defect list (empty) when all
    labels fit. The exception form for renderers that validate
    deep inside a draw routine."""
    defects = check_labels(draw, labels)
    if defects:
        first = defects[0]
        raise LabelFitError(
            first.get("detail") or f"label collision: {first}",
            label=first.get("key") or first.get("a"),
        )
    return defects
