"""Phase 9 — Voice-to-Documentary (§13.4).

Upload a voice recording → transcription → script conversion
(Preserve / Clean / Improve / Expand) → review → the existing
production pipeline.

The flow's honesty rules:

- Transcription goes through services.transcription, the stable
  internal interface (§16) — the provider's real output,
  including its confidence facts and its flagged unclear
  sections. Nothing is transcribed by hand or by template.
- Preserve is the transcript verbatim. Clean / Improve / Expand
  are LLM PROPOSALS: they are returned for review and never
  touch the project until the user applies an approved text.
  The prompts forbid inventing facts or changing meaning;
  substantial changes stay reviewable because the proposal and
  the transcript are both on screen before anything applies.
- Applying an approved text does two recorded things: it
  creates a Script Studio draft (the editable review surface,
  one section per paragraph) and it sets the project's script
  through the project's own storage, with a Versioning script
  snapshot recorded under source "voice_to_documentary" — the
  same snapshot discipline as creation and run submission.
- The original recording stays as project media (§13.4): it is
  the configuration's narration.voice_recording record, stored
  under the project's config_uploads/voice/ directory, and is
  never overwritten by any derived artefact. The transcript is
  a separate artefact (voice_transcript.json) beside it.
"""

from pathlib import Path

from services import media_paths, production_config, transcription
from services.atomic_write import write_json_atomic

# Mirrors api_v1.MAX_SCRIPT_CHARS (the API's script cap);
# defined here to keep services free of an api_v1 import.
MAX_SCRIPT_CHARS = 60000

TRANSCRIPT_FILENAME = "voice_transcript.json"

_TREATMENT_INSTRUCTIONS = {
    "clean": (
        "Remove filler words, false starts and repetitions, and "
        "fix punctuation and paragraphing. Do not rephrase "
        "sentences, do not add or remove any fact, name, date or "
        "number, and do not change the meaning in any way."
    ),
    "improve": (
        "Lightly improve clarity and flow: tighten awkward "
        "phrasing and order sentences within a paragraph where "
        "it clearly helps. Do not add or remove any fact, name, "
        "date or number, and do not change the meaning."
    ),
    "expand": (
        "Expand transitions and explanations so the script "
        "reads as a complete documentary narration, developing "
        "only what the speaker already states or directly "
        "implies. Do not introduce any new fact, name, date, "
        "number or claim that is not in the recording."
    ),
}

TREATMENT_MODES = ("preserve", "clean", "improve", "expand")


def transcript_path(project_id) -> Path:
    return media_paths.project_dir(project_id) / TRANSCRIPT_FILENAME


def load_transcript(project_id) -> dict | None:
    path = transcript_path(project_id)
    if not path.is_file():
        return None
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def transcribe_project_voice(project) -> dict:
    """Transcribe the project's recorded voice upload through
    the transcription interface and persist the transcript
    artefact. Refuses (422) when no recording is attached or
    the file is gone; provider failures surface as 502."""
    config = production_config.stored_config(project.options)
    recording = (
        (config or {}).get("narration") or {}
    ).get("voice_recording")
    if not recording:
        raise production_config.ConfigError(
            422,
            "voice_recording_missing",
            "No voice recording is attached to this project yet; "
            "upload one before transcribing.",
        )
    try:
        result = transcription.transcribe(recording["file_path"])
    except transcription.TranscriptionError as error:
        status = 422 if error.code == "transcription_file_missing" else 502
        raise production_config.ConfigError(
            status, error.code, str(error)
        ) from error
    record = {
        **result,
        "recording": recording,
        "project_id": project.id,
    }
    write_json_atomic(transcript_path(project.id), record)
    return record


def treat_transcript(text, mode) -> dict:
    """One script-conversion proposal (§13.4). Preserve returns
    the transcript verbatim without any model call. The other
    modes return an LLM proposal — the caller reviews it; this
    function stores nothing and changes nothing."""
    if mode not in TREATMENT_MODES:
        raise production_config.ConfigError(
            422,
            "invalid_treatment_mode",
            f"mode must be one of: {', '.join(TREATMENT_MODES)}.",
        )
    if not text or not text.strip():
        raise production_config.ConfigError(
            422, "transcript_empty", "There is no transcript text to treat."
        )
    if mode == "preserve":
        return {"mode": mode, "text": text, "source": "transcript_verbatim"}
    from services import llm_client

    system = (
        "You convert a spoken recording's transcript into a "
        "documentary script. " + _TREATMENT_INSTRUCTIONS[mode]
        + " Return only the converted script text — no "
        "commentary, no headings, no markdown."
    )
    proposal = llm_client.llm_text(system, text)
    return {"mode": mode, "text": proposal.strip(), "source": "llm_proposal"}


def apply_script(db, project, principal, text) -> dict:
    """Apply an approved script text: a Script Studio draft the
    user can keep editing, and the project's script set through
    its own storage with a Versioning snapshot. Returns both
    records. The text is the user's approved text, applied
    verbatim — nothing is regenerated or re-treated here."""
    if not text or not text.strip():
        raise production_config.ConfigError(
            422, "script_empty", "The approved script text is empty."
        )
    if len(text) > MAX_SCRIPT_CHARS:
        raise production_config.ConfigError(
            422,
            "script_too_long",
            f"Scripts are limited to {MAX_SCRIPT_CHARS} "
            "characters; the approved text is longer.",
        )
    from services import script_studio, versioning

    paragraphs = [
        paragraph.strip()
        for paragraph in text.split("\n\n")
        if paragraph.strip()
    ]
    sections = [
        {"heading": "", "body": paragraph} for paragraph in paragraphs
    ]
    draft = script_studio.create_draft(
        principal.user_id,
        title=f"{project.title or 'Untitled'} — voice script",
        category=project.category,
        sections=sections,
    )
    project.script = text
    db.commit()
    db.refresh(project)
    versioning.try_record_script_snapshot(
        project.id, text, "voice_to_documentary",
        actor=principal.user_id,
    )
    return {
        "project_id": project.id,
        "draft": draft,
        "script_applied": True,
    }
