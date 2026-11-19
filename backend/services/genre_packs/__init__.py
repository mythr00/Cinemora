"""Genre packs as DATA (Stage 5A).

A genre pack is a JSON document in data/ — never an if/else branch
in pipeline code. Each pack declares:

- research_query_templates: "{name} {era} historical record"-style
  templates the Stage 3A research pass formats per bible entity,
- entity_handling: editorial care notes for the genre (for true
  crime: never invent evidence/dates/locations; victim dignity),
- format_preferences: format-intent priorities. These stay WITHIN
  the existing bounded rules — format intent remains a bounded
  ranking signal (max 0.10 x confidence in the unified selection);
  the pack declares priorities, it does not raise any cap,
- graphics_motifs: ordered Motion Engine template preferences for
  the targeted-graphics chain,
- style_family: the motion-engine style family for the genre
  (history -> "history", true_crime -> "true_crime", general ->
  None, meaning the historical per-template defaults apply),
- pacing: informational targets only (narration is not retimed),
- music_mood: a metadata label only. NO music library is
  integrated — that selection is founder-gated.

Resolution order in resolve_genre_pack: an explicit known genre in
the run options wins; otherwise a conservative script detector;
otherwise the "general" pack. The detector needs a clear margin —
an ambiguous script stays general rather than being forced into a
genre.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent / "data"

PACK_IDS = ("general", "history", "true_crime")


@lru_cache(maxsize=None)
def load_pack(pack_id):
    """The pack dict for an id, or None when unknown. Returned
    dicts are shared — treat them as read-only."""
    if not pack_id:
        return None
    path = _DATA_DIR / f"{str(pack_id).strip().lower()}.json"
    if not path.exists():
        return None
    try:
        pack = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(pack, dict) or pack.get("id") != path.stem:
        return None
    return pack


def available_packs():
    return [load_pack(p) for p in PACK_IDS if load_pack(p) is not None]


# ---------------------------------------------------------------------------
# Conservative script-based detection
# ---------------------------------------------------------------------------

_TRUE_CRIME_CUES = [
    r"\bmurder(?:ed|s|ing)?\b", r"\bhomicide\b", r"\bvictims?\b",
    r"\bsuspects?\b", r"\barrested\b", r"\bcharged with\b",
    r"\bconvicted\b", r"\bguilty\b", r"\btrial\b", r"\bdetectives?\b",
    r"\binvestigat(?:ion|ors?|ed)\b", r"\bcrime scene\b",
    r"\bkidnapp(?:ed|ing)\b", r"\bdisappeared\b", r"\bmissing person\b",
    r"\bsentenced\b", r"\bbody was found\b", r"\bpolice said\b",
    r"\bforensic\b", r"\bdna\b",
]

_HISTORY_CUES = [
    r"\bcentury\b", r"\bempire\b", r"\bancient\b", r"\bdynasty\b",
    r"\btreaty\b", r"\brevolution\b", r"\bworld war\b", r"\bcivil war\b",
    r"\bkingdom\b", r"\bcolon(?:y|ial|ies)\b", r"\bmedieval\b",
    r"\bexpedition\b", r"\bfounded in\b", r"\bcenturies\b",
]

_YEAR_RE = re.compile(r"\b(1[5-9]\d\d|20[0-2]\d)\b")


def _cue_score(text, cues):
    return sum(1 for cue in cues if re.search(cue, text))


def detect_genre(script_text):
    """Score a script for the history / true_crime packs.

    Returns {"pack_id", "scores", "pre_1961_years"}. A genre wins
    only with >= 3 distinct cue hits AND a strictly higher score
    than the other genre; anything else is "general". History also
    earns up to +4 from distinct pre-1961 year mentions (a script
    anchored in the deep past is a history signal even with few
    genre words).
    """
    text = str(script_text or "").lower()
    crime = _cue_score(text, _TRUE_CRIME_CUES)
    history = _cue_score(text, _HISTORY_CUES)
    years = {int(y) for y in _YEAR_RE.findall(text)}
    pre_1961 = sorted(y for y in years if y <= 1960)
    history += min(4, len(pre_1961))
    scores = {"history": history, "true_crime": crime}
    winner = "general"
    if crime >= 3 and crime > history:
        winner = "true_crime"
    elif history >= 3 and history > crime:
        winner = "history"
    return {"pack_id": winner, "scores": scores, "pre_1961_years": pre_1961}


def resolve_genre_pack(options=None, script_text=""):
    """Resolve the active pack for a run.

    Returns {"pack", "pack_id", "source", "requested", "detection"}
    where source is "option" (explicit known genre in options),
    "detected" (the script detector picked a non-general genre) or
    "default" (the general pack — no option, or an unknown option
    string with an undetectable script; the requested value is
    still recorded verbatim in "requested").
    """
    requested = None
    if isinstance(options, dict):
        requested = options.get("genre") or options.get("genre_pack")
    if requested:
        pack = load_pack(str(requested))
        if pack is not None:
            return {
                "pack": pack,
                "pack_id": pack["id"],
                "source": "option",
                "requested": str(requested),
                "detection": None,
            }
    detection = detect_genre(script_text)
    if detection["pack_id"] != "general":
        pack = load_pack(detection["pack_id"])
        return {
            "pack": pack,
            "pack_id": pack["id"],
            "source": "detected",
            "requested": str(requested) if requested else None,
            "detection": detection,
        }
    pack = load_pack("general")
    return {
        "pack": pack,
        "pack_id": "general",
        "source": "default",
        "requested": str(requested) if requested else None,
        "detection": detection,
    }
