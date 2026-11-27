"""Language catalogue and support facts (Product Improvements
item 6, founder directive 2026-11-26).

Language is selected BEFORE generation and is authoritative for
the project. This module is the single source of truth for what
Cinemora can genuinely do in each language — built from the
actual installed capabilities, never aspirational:

- NARRATION: the narration adapter (services.narration) runs
  Kokoro-82M with its genuine v1.0 voice pack installed: 54
  voices across English (US and UK), Spanish, French, Italian,
  Portuguese, Hindi, Japanese and Mandarin Chinese. Those
  languages have genuine narration support; any other language
  selected for a production is reported as unsupported BEFORE
  charging — narration is never silently substituted with
  another language's audio under a false label (the dishonesty
  services.localization was built to avoid).
- SCRIPT GENERATION: the script writer can produce narration
  text in every language listed here (the same set the
  localization engine translates into, plus English).
- TEXT TRANSLATION: scripts and captions can be translated
  between English and the listed languages via the localization
  engine (text assets; narration stays in its own language).

When a genuinely supported non-English voice is installed, its
language joins the narration set here and nowhere else needs to
change.
"""

from __future__ import annotations

# code -> display name. The non-English set mirrors
# services.localization.LANGUAGES exactly (kept in step by a
# test) so script/translation support claims can never drift
# from the engine that performs them.
LANGUAGE_NAMES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "pt": "Portuguese",
    "it": "Italian",
    "nl": "Dutch",
    "pl": "Polish",
    "tr": "Turkish",
    "ar": "Arabic",
    "hi": "Hindi",
    "id": "Indonesian",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Mandarin Chinese",
}

DEFAULT_LANGUAGE = "en"


def is_known(code) -> bool:
    return str(code or "").strip().lower() in LANGUAGE_NAMES


def normalize(code) -> str:
    """A known language code or the default. Callers that must
    REFUSE unknown codes check is_known() first."""
    value = str(code or "").strip().lower()
    return value if value in LANGUAGE_NAMES else DEFAULT_LANGUAGE


def name_of(code) -> str:
    return LANGUAGE_NAMES.get(normalize(code), "English")


def narration_voices(code) -> list[dict]:
    """The voices that genuinely speak this language, read live
    from the installed narration catalogue (Kokoro-82M voice
    pack metadata). Empty for languages the pack has no voice
    for — by fact, not by policy."""
    code = normalize(code)
    from services import narration

    voices = []
    for voice_id in narration.VOICE_CATALOGUE:
        canonical = narration.VOICE_ALIASES.get(voice_id, voice_id)
        meta = narration.KOKORO_VOICE_META[canonical]
        if meta["language"] != code:
            continue
        label = (
            f"{meta['region']} — {meta['name']} "
            f"({meta['gender']})"
        )
        voices.append(
            {"id": voice_id, "language": code, "label": label}
        )
    return voices


def narration_supported(code) -> bool:
    return bool(narration_voices(code))


def language_catalogue() -> list[dict]:
    """The customer-facing catalogue: every language with its
    honest support facts."""
    catalogue = []
    for code, name in LANGUAGE_NAMES.items():
        voices = narration_voices(code)
        catalogue.append(
            {
                "code": code,
                "name": name,
                "narration_supported": bool(voices),
                "voices": voices,
                "script_supported": True,
                "translation_supported": True,
            }
        )
    return catalogue


def production_block_reason(code) -> str | None:
    """Why a production must not start in this language, or None
    when it may. The message is customer-facing and honest about
    both the limitation and the available alternatives."""
    code = normalize(code)
    if narration_supported(code):
        return None
    name = LANGUAGE_NAMES[code]
    return (
        f"Narration is not available in {name} yet: Cinemora's "
        "installed voices cover English, Spanish, French, "
        "Italian, Portuguese, Hindi, Japanese and Mandarin "
        f"Chinese, and a {name} production would otherwise be "
        "another language's audio under a false label. You can "
        "generate in a supported language and add translated "
        f"{name} captions and script afterwards in "
        "Localization, or choose a supported production "
        "language."
    )


def project_language(project) -> str:
    """The project's authoritative language: the flat projected
    option first, then the production_config document, then the
    default."""
    options = getattr(project, "options", None) or {}
    if isinstance(options, dict):
        flat = options.get("language")
        if is_known(flat):
            return normalize(flat)
        config = options.get("production_config")
        if isinstance(config, dict):
            narration = config.get("narration") or {}
            if is_known(narration.get("language")):
                return normalize(narration.get("language"))
    return DEFAULT_LANGUAGE
