"""End-to-end authenticity taxonomy (Master Production Recovery,
Phase 3).

Phase 0 root cause 4: authenticity broke end-to-end. The
``illustrative_stock`` stamp written by the stock providers
(``services/stock_providers.py``) was read NOWHERE, unstamped assets
defaulted to the ``direct`` tier at the timeline, and an illustrative
stock photograph could satisfy a sentence whose narration states a
fact about the real event. This module is the single classification
pathway every later stage reads:

- one enum of classifications (below);
- :func:`classify_candidate` — classify a discovery candidate from
  its provenance + verification evidence;
- :func:`classify_visual` — classify an already-selected visual
  (timeline / asset dicts) with the same precedence;
- :func:`requires_direct_evidence` — whether a shot need / scenario
  demands direct evidence of the real subject/event;
- :func:`can_satisfy` — whether a classification may serve a need.

The honesty rules are absolute and are NOT tunable thresholds:

- an asset with no usable provenance evidence classifies as
  ``UNKNOWN`` — never as authentic;
- ``ILLUSTRATIVE_STOCK`` can never satisfy a direct-evidence need
  (enforced in ``story_understanding.verify_candidate``'s path and
  re-checkable anywhere via :func:`can_satisfy`);
- the B-roll *tier* axis (direct / related / activity_broll /
  atmospheric / graphic — the candidate's relation to the narration)
  is a separate, older axis and is deliberately untouched: tier says
  how a visual relates to the sentence, classification says what the
  visual actually IS. A tier-``direct`` candidate can still be
  illustrative stock; that mismatch is exactly what this module
  exists to expose.
"""

from __future__ import annotations


# ---------------------------------------------------------------- enum

AUTHENTIC_FOOTAGE = "authentic_footage"
AUTHENTIC_PHOTO = "authentic_photo"
AUTHENTIC_DOCUMENT = "authentic_document"
ILLUSTRATIVE_STOCK = "illustrative_stock"
CONTEXTUAL_BROLL = "contextual_broll"
GENERATED_GRAPHIC = "generated_graphic"
AI_IMAGERY = "ai_imagery"
UNKNOWN = "unknown"

CLASSIFICATIONS = (
    AUTHENTIC_FOOTAGE,
    AUTHENTIC_PHOTO,
    AUTHENTIC_DOCUMENT,
    ILLUSTRATIVE_STOCK,
    CONTEXTUAL_BROLL,
    GENERATED_GRAPHIC,
    AI_IMAGERY,
    UNKNOWN,
)

_CLASSIFICATION_SET = frozenset(CLASSIFICATIONS)

# Classifications that ARE direct evidence of the real subject/event.
DIRECT_EVIDENCE_CLASSIFICATIONS = frozenset(
    (AUTHENTIC_FOOTAGE, AUTHENTIC_PHOTO, AUTHENTIC_DOCUMENT)
)

# Provenance values that mean "licensed illustrative stock", as
# stamped by services.stock_providers._stock_stamp.
_ILLUSTRATIVE_PROVENANCE = "illustrative_stock"
_ILLUSTRATIVE_PROVIDERS = frozenset(("pexels", "pixabay"))

# Tier values (the B-roll ladder's relation axis) that mean the
# visual is contextual material rather than the subject itself.
_BROLL_TIERS = frozenset(("activity_broll", "atmospheric"))


def is_valid_classification(value):
    return isinstance(value, str) and value in _CLASSIFICATION_SET


def normalise_classification(value, default=UNKNOWN):
    """Return *value* when it is a valid classification, else the
    default (``UNKNOWN`` — an unrecognised stamp is no evidence)."""
    if is_valid_classification(value):
        return value
    return default


# ------------------------------------------------------- evidence tests

def is_illustrative_stock(candidate):
    """True when the candidate carries the stock providers'
    illustrative stamp — the boolean flag, the provenance value, or
    a stock provider paired with a recorded stock licence."""
    if not isinstance(candidate, dict):
        return False
    if candidate.get("illustrative_stock"):
        return True
    if str(candidate.get("provenance") or "") == _ILLUSTRATIVE_PROVENANCE:
        return True
    provider = str(
        candidate.get("provider") or candidate.get("source") or ""
    ).strip().lower()
    licence = str(candidate.get("license") or "").strip().lower()
    if provider in _ILLUSTRATIVE_PROVIDERS and "license" in licence:
        return True
    return False


def is_generated_graphic(visual):
    if not isinstance(visual, dict):
        return False
    return bool(
        visual.get("motion_graphic")
        or visual.get("type") == "motion_graphic"
        or visual.get("visual_format") == "graphic"
        or (
            visual.get("generated")
            and str(visual.get("source") or "")
            == "cinemora_motion_graphics"
        )
    )


def is_ai_imagery(visual):
    """AI-generated imagery. No AI-image provider exists in the
    pipeline today; the flags are recognised so the day one is
    integrated its output can never pass as photography."""
    if not isinstance(visual, dict):
        return False
    return bool(
        visual.get("ai_generated")
        or visual.get("ai_imagery")
        or str(visual.get("provenance") or "") == "ai_generated"
    )


def _kind_of(item):
    return str(
        item.get("kind") or item.get("type") or ""
    ).strip().lower()


def _has_provenance_signal(item):
    """Any recorded evidence of where the asset came from: a source
    URL, a provider/source name, or a licence. Without at least one
    of these there is nothing to classify FROM — the asset is
    unknown, however relevant it scored."""
    return bool(
        str(item.get("source_url") or item.get("url") or "").strip()
        or str(item.get("provider") or item.get("source") or "").strip()
        or str(item.get("license") or "").strip()
    )


# ---------------------------------------------------------- classify

def classify_candidate(candidate, verification=None):
    """Classify a discovery candidate.

    Precedence (first match wins):
      1. generated graphic (the motion-graphics pipeline's own
         record shape);
      2. AI imagery (explicit generation flags);
      3. illustrative stock (the stock providers' stamp);
      4. contextual B-roll (tier axis says the visual is related
         activity / atmosphere, not the subject);
      5. document kind -> authentic document;
      6. video -> authentic footage; image -> authentic photo —
         ONLY with at least one provenance signal;
      7. otherwise UNKNOWN.
    """
    if not isinstance(candidate, dict):
        return UNKNOWN
    stamped = candidate.get("classification")
    if is_valid_classification(stamped):
        return stamped
    if is_generated_graphic(candidate):
        return GENERATED_GRAPHIC
    if is_ai_imagery(candidate):
        return AI_IMAGERY
    if is_illustrative_stock(candidate):
        return ILLUSTRATIVE_STOCK
    tier = str(candidate.get("tier") or "").strip().lower()
    if tier in _BROLL_TIERS:
        return CONTEXTUAL_BROLL
    if not _has_provenance_signal(candidate):
        return UNKNOWN
    kind = _kind_of(candidate)
    if kind in ("document", "pdf", "scan", "page"):
        return AUTHENTIC_DOCUMENT
    if kind in ("video", "footage", "clip"):
        return AUTHENTIC_FOOTAGE
    if kind in ("image", "photo", "still", "web"):
        # "web" is the discovery kind for sourced web imagery
        # (screenshots / page media with a recorded source URL).
        return AUTHENTIC_PHOTO
    if candidate.get("video_url"):
        return AUTHENTIC_FOOTAGE
    if candidate.get("image_url"):
        return AUTHENTIC_PHOTO
    return UNKNOWN


def classify_visual(visual):
    """Classify an already-selected visual (asset / timeline item).

    Same precedence as :func:`classify_candidate`; a valid stamped
    ``classification`` always wins, so the value decided at
    verification time is the value the timeline, the rights manifest
    and the certificate report."""
    return classify_candidate(visual)


# ------------------------------------------------- direct evidence

def requires_direct_evidence(need):
    """Whether a shot need / scenario demands direct evidence of the
    real subject or event.

    The pipeline's existing expression of that demand is the
    resolved visual category: ``story_understanding`` resolves a
    need to the ``evidence`` category exactly when its specificity
    is ``exact`` or its role is evidence-bearing (see
    ``resolve_visual_category`` / ``CATEGORY_PROFILES``). The check
    is duplicated here structurally (specificity / role / shot type)
    so this module never imports the monolith's dependents; the two
    definitions are pinned to each other by tests.
    """
    if not isinstance(need, dict):
        return False
    if str(need.get("specificity") or "").strip().lower() == "exact":
        return True
    if str(need.get("role") or "").strip().lower() == "evidence":
        return True
    shot_type = str(
        need.get("shot_type") or need.get("type") or ""
    ).strip().lower()
    if shot_type in ("document", "object"):
        return True
    return False


def can_satisfy(classification, need):
    """Whether an asset with *classification* may serve *need*.

    Only the direct-evidence rule restricts: a need that demands
    direct evidence accepts only the authentic classifications.
    Every other need accepts any classification — illustrative stock
    and B-roll remain legitimate there (the founder's B-roll
    principle), they simply may not impersonate evidence.
    """
    if not requires_direct_evidence(need):
        return True
    return classification in DIRECT_EVIDENCE_CLASSIFICATIONS
