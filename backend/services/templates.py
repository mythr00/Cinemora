"""Templates & production presets (founder priority #15;
gap-audit doc 01 §5, doc 06 Phase M).

The audit's finding: genre packs are genuine production presets,
but there were no user-created templates — a user who had tuned a
project's production options had no way to keep that bundle and
start from it again. This module is that layer, built on the
substrates the audit prescribes and nothing else:

- STORAGE is the Phase 8 preferences document's ``templates``
  section (``user_preferences`` — the platform's single per-user
  store; the Assistant's ``assistant`` section and the Asset
  Library's ``library`` section established the pattern). No new
  table, no parallel store.
- BUILT-IN templates are DERIVED at read time from the real genre
  packs (services.genre_packs.available_packs): one per pack,
  carrying the pack's label as the category and the pack id as
  the ``genre`` option — traceable to the pack document it came
  from, never a hand-maintained copy that can drift. Built-ins
  are read-only; copying one creates an editable user template.
  Packs record no script section structure, so built-ins carry
  none — inventing per-genre script scaffolds would be
  fabrication, not data.
- A USER template is a named bundle: category, an optional
  Script Studio target length, a script SECTION STRUCTURE
  (headings only — a template is a scaffold, not a copy of a
  film's script; bodies are never stored), and a bundle of
  project OPTIONS.
- THE FIELD AUDIT is the module's contract: a template option
  key exists here only if the production pipeline actually
  consumes it. The consumed set (each verified against the code
  in the Phase M build report): ``genre`` (genre_packs
  resolution in jobs.run_full_pipeline), ``voice_id``
  (services.narration), ``captions`` + ``caption_sync`` (jobs
  caption resolution), ``music_file`` / ``music_enabled`` /
  ``music_volume`` / ``narration_volume`` (services.music
  resolve_music_request), the seven mix keys
  (services.music.resolve_mix_config via _MIX_OPTION_KEYS) and
  ``require_plan_approval`` (the plan gate in api_v1's run
  submission). Keys the creation path stores but nothing consumes
  (unknown extras pass through ProjectOptionsIn harmlessly into
  snapshots) are REFUSED here — a template must never present a
  stored-but-inert setting as a working one. There is
  deliberately NO narration-speed key anywhere: the founder
  retired speed control (2026-11-22) and the retired per-pack
  narration_speed stays retired.
- APPLYING a template goes through the normal creation paths:
  apply-to-project builds the same ProjectCreateIn the create
  route validates and creates through the same helper (plan
  script-length enforcement included), and apply-to-draft
  creates a Script Studio draft through script_studio's own
  create path. A template can never smuggle an option past the
  validation manual creation applies — options are validated
  against the field audit at save time AND re-validated by the
  creation model at apply time.
- Phase 9's configuration presets (Clean / Cinematic / Vintage /
  Minimal / Custom visual themes, motion-graphics intensity,
  music sections) are NOT template fields: no pipeline path
  consumes them yet — visual theme derives from the genre pack —
  so presenting them here would be exactly the stored-but-inert
  failure this module exists to avoid. When Phase 9 lands its
  enforcement, its presets can ride this bundle then.

Ownership: user templates live in the caller's own preferences
document, so another user's template id is simply absent — a
404, never a leak. Built-ins are visible to every account.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from models import UserPreference
from services import genre_packs, script_studio

SECTION_KEY = "templates"
MAX_TEMPLATES = 100
MAX_SECTIONS = 60  # mirrors script_studio.MAX_SECTIONS
MAX_NAME_CHARS = 200
MAX_CATEGORY_CHARS = 120
DEFAULT_CATEGORY = script_studio.DEFAULT_CATEGORY


class TemplateError(Exception):
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


def _new_id() -> str:
    return f"tpl_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------
# The field audit: option keys the pipeline actually consumes
# ---------------------------------------------------------------------

_CAPTIONS_MODES = ("burn", "sidecar", "off")
_CAPTION_SYNC_MODES = ("word", "off")
# C-13 (Phase 7): "syllable" stays accepted for legacy templates
# but is demoted to "word" at validation — derived syllable timing
# is an internal renderer fallback, not a user-facing option.
_CAPTION_SYNC_ACCEPTED = ("word", "syllable", "off")


def _caption_sync(key, value):
    cleaned = _enum(key, value, _CAPTION_SYNC_ACCEPTED)
    return "word" if cleaned == "syllable" else cleaned


def _number(key, value, *, lo, hi, exclusive_lo=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TemplateError(
            422, "invalid_option_value", f"Option {key} must be a number."
        )
    number = float(value)
    if (number <= lo if exclusive_lo else number < lo) or number > hi:
        raise TemplateError(
            422,
            "invalid_option_value",
            f"Option {key} must be between {lo} and {hi}.",
        )
    return number


def _boolean(key, value):
    if not isinstance(value, bool):
        raise TemplateError(
            422, "invalid_option_value", f"Option {key} must be true or false."
        )
    return value


def _string(key, value, *, max_chars):
    if not isinstance(value, str) or not value.strip():
        raise TemplateError(
            422, "invalid_option_value", f"Option {key} must be a non-empty string."
        )
    return value.strip()[:max_chars]


def _enum(key, value, choices):
    if value not in choices:
        raise TemplateError(
            422,
            "invalid_option_value",
            f"Option {key} must be one of: {', '.join(choices)}.",
        )
    return value


def _genre(key, value):
    pack_id = _string(key, value, max_chars=60).lower()
    if genre_packs.load_pack(pack_id) is None:
        raise TemplateError(
            422,
            "unknown_genre",
            f"Option genre names no genre pack: {value!r}. "
            "Templates can only carry a pack the pipeline can load.",
        )
    return pack_id


#: key -> validator. This mapping IS the field audit (see the
#: module docstring for where each key is consumed); the ranges
#: mirror ProjectOptionsIn, the creation path's own model.
OPTION_VALIDATORS = {
    "genre": _genre,
    "voice_id": lambda k, v: _string(k, v, max_chars=120),
    "captions": lambda k, v: _enum(k, v, _CAPTIONS_MODES),
    "caption_sync": _caption_sync,
    "music_file": lambda k, v: _string(k, v, max_chars=1000),
    "music_enabled": _boolean,
    "music_volume": lambda k, v: _number(k, v, lo=0.0, hi=2.0),
    "narration_volume": lambda k, v: _number(k, v, lo=0.0, hi=2.0),
    "music_fade_in_seconds": lambda k, v: _number(k, v, lo=0.0, hi=30.0),
    "music_fade_out_seconds": lambda k, v: _number(k, v, lo=0.0, hi=30.0),
    "music_loop_crossfade_seconds": lambda k, v: _number(k, v, lo=0.0, hi=10.0),
    "duck_threshold": lambda k, v: _number(k, v, lo=0.0, hi=1.0, exclusive_lo=True),
    "duck_ratio": lambda k, v: _number(k, v, lo=1.0, hi=100.0),
    "duck_attack_ms": lambda k, v: _number(k, v, lo=0.0, hi=10000.0),
    "duck_release_ms": lambda k, v: _number(k, v, lo=0.0, hi=60000.0),
    "require_plan_approval": _boolean,
}

#: Keys that once existed and are refused by name, so the refusal
#: explains itself instead of reading as an arbitrary rejection.
RETIRED_OPTION_KEYS = {
    "narration_speed": (
        "Narration speed control was removed by founder ruling "
        "(2026-11-22): narration runs at the voice provider's "
        "native rate, and no template may carry a speed setting."
    ),
    "music": (
        "The legacy 'music' boolean was replaced by the Stage H "
        "music contract; use music_enabled / music_file instead."
    ),
}


def clean_options(raw) -> dict:
    """Validate an options bundle against the field audit
    (strict: unknown or invalid keys refuse the whole bundle)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TemplateError(422, "invalid_options", "Options must be an object.")
    cleaned = {}
    for key, value in raw.items():
        if value is None:
            continue
        if key in RETIRED_OPTION_KEYS:
            raise TemplateError(
                422, "retired_option", RETIRED_OPTION_KEYS[key],
                details={"key": key},
            )
        validator = OPTION_VALIDATORS.get(key)
        if validator is None:
            raise TemplateError(
                422,
                "unknown_option",
                f"Option {key!r} is not a setting the production "
                "pipeline consumes, so a template cannot carry it.",
                details={"key": key},
            )
        cleaned[key] = validator(key, value)
    return cleaned


def filter_options(raw):
    """The lenient form for save-from-project: keep the consumed
    keys, drop the rest, and REPORT every drop with its reason —
    a project's stored options may carry snapshot extras that
    nothing consumes, and silently keeping or silently dropping
    them would both misdescribe the template."""
    cleaned, dropped = {}, []
    if not isinstance(raw, dict):
        return cleaned, dropped
    for key, value in raw.items():
        if value is None:
            continue
        if key in RETIRED_OPTION_KEYS:
            dropped.append({"key": key, "reason": "retired_option"})
            continue
        validator = OPTION_VALIDATORS.get(key)
        if validator is None:
            dropped.append({"key": key, "reason": "not_consumed_by_pipeline"})
            continue
        try:
            cleaned[key] = validator(key, value)
        except TemplateError:
            dropped.append({"key": key, "reason": "invalid_value"})
    return cleaned, dropped


# ---------------------------------------------------------------------
# Record cleaning
# ---------------------------------------------------------------------


def _clean_name(value, *, required=True):
    name = str(value or "").strip()[:MAX_NAME_CHARS]
    if required and not name:
        raise TemplateError(422, "name_required", "A template needs a name.")
    return name


def _clean_category(value):
    category = str(value or "").strip()[:MAX_CATEGORY_CHARS]
    return category or DEFAULT_CATEGORY


def _clean_target(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TemplateError(
            422, "invalid_target", "Target length must be minutes."
        )
    minutes = float(value)
    if minutes <= 0 or minutes > 600:
        raise TemplateError(
            422, "invalid_target", "Target length must be between 0 and 600 minutes."
        )
    return minutes


def _clean_sections(raw):
    """Section STRUCTURE only: a list of headings. Bodies in the
    input are discarded by construction — a template is a
    scaffold, never a copy of a script."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TemplateError(422, "invalid_sections", "Sections must be a list.")
    if len(raw) > MAX_SECTIONS:
        raise TemplateError(
            422, "too_many_sections",
            f"A template may carry at most {MAX_SECTIONS} sections.",
        )
    sections = []
    for entry in raw:
        if isinstance(entry, dict):
            heading = str(entry.get("heading") or "").strip()[:200]
        elif isinstance(entry, str):
            heading = entry.strip()[:200]
        else:
            raise TemplateError(
                422, "invalid_section", "Each section must carry a heading."
            )
        if heading:
            sections.append({"heading": heading})
    return sections


# ---------------------------------------------------------------------
# Store: the preferences document's `templates` section
# ---------------------------------------------------------------------


def _load_templates(db, user_id) -> list:
    if not user_id:
        return []
    row = db.get(UserPreference, user_id)
    document = (row.document if row else None) or {}
    section = document.get(SECTION_KEY)
    if not isinstance(section, dict):
        return []
    records = section.get("templates")
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict) and r.get("id")]


def _save_templates(db, user_id, records) -> None:
    if not user_id:
        raise TemplateError(
            403, "no_user", "Templates need a user account."
        )
    row = db.get(UserPreference, user_id)
    document = dict((row.document if row else None) or {})
    document[SECTION_KEY] = {"templates": records}
    if row is None:
        db.add(UserPreference(user_id=user_id, document=document))
    else:
        row.document = document
    db.commit()


def _find_record(records, template_id):
    for record in records:
        if record.get("id") == template_id:
            return record
    raise TemplateError(404, "template_not_found", "No such template.")


# ---------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------


def _public(record, *, kind="user"):
    return {
        "id": record.get("id"),
        "kind": kind,
        "name": record.get("name"),
        "category": record.get("category"),
        "target_minutes": record.get("target_minutes"),
        "options": dict(record.get("options") or {}),
        "sections": [
            {"heading": s.get("heading")}
            for s in record.get("sections") or []
            if isinstance(s, dict)
        ],
        "source": record.get("source"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


def builtin_templates() -> list:
    """One built-in template per REAL genre pack, derived at read
    time from the pack documents themselves — the pack's label is
    the category, the pack id is the genre option, and the pack's
    own description/style/theme ride along as provenance. Packs
    record no script structure, so sections are empty (see the
    module docstring)."""
    builtins = []
    for pack in genre_packs.available_packs():
        if pack is None:
            continue
        record = {
            "id": f"builtin:{pack['id']}",
            "name": pack.get("label") or pack["id"],
            "category": pack.get("label") or pack["id"],
            "target_minutes": None,
            "options": {"genre": pack["id"]},
            "sections": [],
            "source": {
                "kind": "genre_pack",
                "pack_id": pack["id"],
                "label": pack.get("label"),
                "description": pack.get("description"),
                "style_family": pack.get("style_family"),
                "visual_theme": pack.get("visual_theme"),
            },
            "created_at": None,
            "updated_at": None,
        }
        builtins.append(_public(record, kind="builtin"))
    return builtins


def _is_builtin_id(template_id) -> bool:
    return isinstance(template_id, str) and template_id.startswith("builtin:")


def resolve_template(db, principal, template_id) -> dict:
    """The public view of a built-in or one of the caller's own
    templates. Another user's template is absent from the
    caller's store — a 404, never a leak."""
    if _is_builtin_id(template_id):
        pack_id = template_id.split(":", 1)[1]
        for view in builtin_templates():
            if view["id"] == template_id and genre_packs.load_pack(pack_id):
                return view
        raise TemplateError(404, "template_not_found", "No such template.")
    records = _load_templates(db, principal.user_id)
    return _public(_find_record(records, template_id))


# ---------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------


def list_templates(db, principal) -> dict:
    records = _load_templates(db, principal.user_id)
    return {
        "templates": [_public(r) for r in records],
        "builtins": builtin_templates(),
    }


def _new_record(*, name, category, target_minutes, options, sections, source):
    now = _now_iso()
    return {
        "id": _new_id(),
        "name": name,
        "category": category,
        "target_minutes": target_minutes,
        "options": options,
        "sections": sections,
        "source": source,
        "created_at": now,
        "updated_at": now,
    }


def create_template(db, principal, *, name=None, category=None,
                    target_minutes=None, options=None, sections=None,
                    source=None) -> dict:
    records = _load_templates(db, principal.user_id)
    if len(records) >= MAX_TEMPLATES:
        raise TemplateError(
            422, "too_many_templates",
            f"A user may keep at most {MAX_TEMPLATES} templates.",
        )
    record = _new_record(
        name=_clean_name(name),
        category=_clean_category(category),
        target_minutes=_clean_target(target_minutes),
        options=clean_options(options),
        sections=_clean_sections(sections),
        source=source or {"kind": "manual"},
    )
    records.insert(0, record)
    _save_templates(db, principal.user_id, records)
    return _public(record)


def update_template(db, principal, template_id, *, name=None,
                    category=None, target_minutes="__unset__",
                    options=None, sections=None) -> dict:
    if _is_builtin_id(template_id):
        raise TemplateError(
            422,
            "template_builtin_read_only",
            "Built-in templates come from the genre packs and "
            "cannot be edited; copy one to make it yours.",
        )
    records = _load_templates(db, principal.user_id)
    record = _find_record(records, template_id)
    if name is not None:
        record["name"] = _clean_name(name)
    if category is not None:
        record["category"] = _clean_category(category)
    if target_minutes != "__unset__":
        record["target_minutes"] = _clean_target(target_minutes)
    if options is not None:
        record["options"] = clean_options(options)
    if sections is not None:
        record["sections"] = _clean_sections(sections)
    record["updated_at"] = _now_iso()
    _save_templates(db, principal.user_id, records)
    return _public(record)


def delete_template(db, principal, template_id) -> dict:
    if _is_builtin_id(template_id):
        raise TemplateError(
            422,
            "template_builtin_read_only",
            "Built-in templates come from the genre packs and "
            "cannot be deleted.",
        )
    records = _load_templates(db, principal.user_id)
    record = _find_record(records, template_id)
    records = [r for r in records if r.get("id") != template_id]
    _save_templates(db, principal.user_id, records)
    return {"deleted": record.get("id")}


def copy_builtin(db, principal, pack_id, *, name=None) -> dict:
    pack = genre_packs.load_pack(str(pack_id or "").strip().lower())
    if pack is None:
        raise TemplateError(
            404, "template_not_found", f"No genre pack {pack_id!r}."
        )
    records = _load_templates(db, principal.user_id)
    if len(records) >= MAX_TEMPLATES:
        raise TemplateError(
            422, "too_many_templates",
            f"A user may keep at most {MAX_TEMPLATES} templates.",
        )
    record = _new_record(
        name=_clean_name(name) if name else (pack.get("label") or pack["id"]),
        category=pack.get("label") or pack["id"],
        target_minutes=None,
        options={"genre": pack["id"]},
        sections=[],
        source={"kind": "builtin_copy", "pack_id": pack["id"]},
    )
    records.insert(0, record)
    _save_templates(db, principal.user_id, records)
    return _public(record)


# ---------------------------------------------------------------------
# Save from an existing project / Script Studio draft
# ---------------------------------------------------------------------


def template_from_project(db, principal, project, *, name=None):
    """Save a project's configuration as a template: its category
    plus its stored options FILTERED through the field audit —
    consumed keys kept, everything else dropped and reported.
    The project's script is never copied (a project records no
    section structure, and a template is not a script clone)."""
    records = _load_templates(db, principal.user_id)
    if len(records) >= MAX_TEMPLATES:
        raise TemplateError(
            422, "too_many_templates",
            f"A user may keep at most {MAX_TEMPLATES} templates.",
        )
    options, dropped = filter_options(project.options or {})
    record = _new_record(
        name=_clean_name(name) if name else (
            f"{project.title or 'Untitled'} — template"
        )[:MAX_NAME_CHARS],
        category=_clean_category(project.category),
        target_minutes=None,
        options=options,
        sections=[],
        source={"kind": "project", "project_id": project.id},
    )
    records.insert(0, record)
    _save_templates(db, principal.user_id, records)
    return _public(record), dropped


def template_from_draft(db, principal, script_id, *, name=None) -> dict:
    """Save a Script Studio draft's STRUCTURE as a template: its
    category, target length and section HEADINGS. The draft's
    narration bodies are the author's script and are never
    copied into a template."""
    try:
        draft = script_studio.get_draft(principal.user_id, script_id)
    except script_studio.ScriptError as error:
        raise TemplateError(
            error.status_code, error.code, error.message
        ) from error
    records = _load_templates(db, principal.user_id)
    if len(records) >= MAX_TEMPLATES:
        raise TemplateError(
            422, "too_many_templates",
            f"A user may keep at most {MAX_TEMPLATES} templates.",
        )
    record = _new_record(
        name=_clean_name(name) if name else (
            f"{draft.get('title') or 'Untitled script'} — template"
        )[:MAX_NAME_CHARS],
        category=_clean_category(draft.get("category")),
        target_minutes=_clean_target(draft.get("target_minutes")),
        options={},
        sections=_clean_sections(draft.get("sections")),
        source={"kind": "script_draft", "script_id": script_id},
    )
    records.insert(0, record)
    _save_templates(db, principal.user_id, records)
    return _public(record)


# ---------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------


def apply_to_draft(db, principal, template_id, *, title=None) -> dict:
    """Create a Script Studio draft from the template through the
    studio's own creation path (its section/target validation
    applies unchanged): the template's headings become sections
    with empty bodies for the author to write."""
    view = resolve_template(db, principal, template_id)
    sections = [
        {"heading": s["heading"], "body": ""}
        for s in view["sections"]
    ]
    return script_studio.create_draft(
        principal.user_id,
        title=title or view["name"],
        category=view["category"],
        target_minutes=view["target_minutes"],
        sections=sections or None,
    )


def template_creation_payload(db, principal, template_id) -> dict:
    """The {category, options} a project creation from this
    template must use. The API layer feeds these through the
    SAME ProjectCreateIn model + creation helper as manual
    creation, so plan limits and option validation are the
    creation path's own — never a template-side bypass."""
    view = resolve_template(db, principal, template_id)
    return {
        "template": view,
        "category": view["category"],
        "options": dict(view["options"] or {}),
    }
