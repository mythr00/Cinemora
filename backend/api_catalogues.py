"""Health + Catalogues routes — extracted from api_v1.py (B-14, step 1).

The first bounded step of the api_v1 modularization: the health
probes and the IA-06 catalogues are the lowest-entanglement route
family in api_v1 — their only ties to the host module are the
``require_user`` auth dependency and the services layer
(genre_packs / narration / readiness), never project or run state.

``build_router(require_user)`` is a factory rather than a module-
level router importing api_v1, so there is no import cycle:
api_v1 builds this router with its own require_user (the very
same callable the routes used inline) and includes it on the v1
router at the position the routes originally occupied. Every
route path, method, dependency and response is identical to the
pre-extraction definitions.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from services import genre_packs, narration


def build_router(require_user) -> APIRouter:
    router = APIRouter()

    # ---------------------------------------------------------------------
    # Health (the only unauthenticated v1 route)
    # ---------------------------------------------------------------------


    @router.get("/health")
    def health():
        return {"status": "ok", "service": "cinemora-api", "version": "v1"}


    @router.get("/health/live")
    def health_live():
        """B-15 public liveness: exactly {"status": "ok"} — no
        dependency state or diagnostics (FS rules; readiness detail
        is admin-only at /admin/readiness)."""
        from services import readiness as readiness_service

        return readiness_service.liveness()


    # ---------------------------------------------------------------------
    # Catalogues (IA-06) — read-only views over the REAL sources of truth
    # ---------------------------------------------------------------------
    # Genres come from services.genre_packs (the pack JSON documents in
    # services/genre_packs/data/, loaded by genre_packs.available_packs)
    # and voices from services.narration.VOICE_CATALOGUE (Stage 3J: the
    # catalogue of voices actually installed). Nothing is hardcoded
    # here: installing a pack or a voice changes these responses with
    # no API change. The music library routes below were activated by
    # the founder mandate of 2026-11-27 (CINEMATIC MUSIC LIBRARY):
    # the catalogue itself is read through
    # services.production_config's loader over the
    # CINEMORA_MUSIC_CATALOGUE seam (bundled library by default);
    # the packs' music metadata fields (music_mood / mix_preset) are
    # not catalogue data and are not exposed here.


    def _genre_view(pack: dict) -> dict:
        import copy

        return {
            "id": pack["id"],
            # The pack documents carry no separate "name" field; their
            # human-readable name IS the "label" field.
            "name": pack.get("label"),
            "label": pack.get("label"),
            "description": pack.get("description"),
            "style_family": pack.get("style_family"),
            "graphics_motifs": copy.deepcopy(pack.get("graphics_motifs") or []),
            "format_preferences": copy.deepcopy(pack.get("format_preferences") or {}),
            "entity_handling": copy.deepcopy(pack.get("entity_handling") or []),
            "research_query_templates": copy.deepcopy(
                pack.get("research_query_templates") or []
            ),
            "pacing": copy.deepcopy(pack.get("pacing") or {}),
        }


    @router.get("/catalogues/genres")
    def list_genre_catalogue(principal=Depends(require_user)):
        packs = [p for p in genre_packs.available_packs() if p is not None]
        return {"genres": [_genre_view(pack) for pack in packs]}


    def _voice_view(voice_id: str, model, config) -> dict:
        # The alias structure is declared by the narration module
        # itself (VOICE_ALIASES). It used to be derived from shared
        # model files, but with Kokoro every voice in the pack
        # shares ONE model + voices file, so file-pair inference
        # would call every voice an alias of the first. Today:
        # "default" aliases the primary voice af_heart; every real
        # voice aliases nothing.
        is_default = voice_id == narration.DEFAULT_VOICE_ID
        # The voice's true language/region/gender from the pack
        # metadata (the alias dereferences to the primary voice it
        # names), so consumers can filter and label by language
        # instead of guessing from the id.
        meta = narration.KOKORO_VOICE_META[
            narration.VOICE_ALIASES.get(voice_id, voice_id)
        ]
        return {
            "id": voice_id,
            "is_default": is_default,
            "alias_of": narration.VOICE_ALIASES.get(voice_id),
            "model": model.name,
            "model_file": model.name,
            "config_file": config.name,
            "installed": bool(model.exists() and config.exists()),
            "language": meta["language"],
            "region": meta["region"],
            "gender": meta["gender"],
            "name": meta["name"],
        }


    @router.get("/catalogues/languages")
    def list_language_catalogue(principal=Depends(require_user)):
        """Item 6: the language catalogue with honest per-language
        support facts (narration voices that genuinely exist,
        script and translation support), consumed by the
        pre-generation language selector."""
        from services import languages as languages_service

        return {
            "languages": languages_service.language_catalogue(),
            "default_language": languages_service.DEFAULT_LANGUAGE,
        }

    @router.get("/catalogues/voices")
    def list_voice_catalogue(principal=Depends(require_user)):
        voices = [
            _voice_view(voice_id, model, config)
            for voice_id, (model, config) in narration.VOICE_CATALOGUE.items()
        ]
        return {
            "voices": voices,
            "default_voice_id": narration.DEFAULT_VOICE_ID,
        }

    # ---------------------------------------------------------------------
    # Music library (founder mandate 2026-11-27) — browse + preview
    # ---------------------------------------------------------------------

    @router.get("/catalogues/music")
    def list_music_catalogue(principal=Depends(require_user)):
        """The configured music library for the selection UI:
        the same library_state view the configuration catalogue
        carries (configured / available / real tracks)."""
        from services import production_config

        return {"music_library": production_config.library_state()}

    @router.get("/catalogues/music/{track_id}/preview")
    def music_track_preview(track_id: str, principal=Depends(require_user)):
        """Stream one library track's preview clip (its full file
        when the record carries no preview asset). Unknown tracks
        and missing files are honest 404s — a preview is never
        silently swapped for another track."""
        from services import production_config

        catalogue, root = production_config.load_library()
        if catalogue is None or catalogue.empty:
            raise HTTPException(
                status_code=404, detail="No music library is configured."
            )
        track = catalogue.by_id(track_id)
        if track is None:
            raise HTTPException(
                status_code=404,
                detail=f"Music track {track_id!r} is not in the library.",
            )
        relative = track.assets.preview or track.assets.full
        path = (root / relative).resolve()
        if root.resolve() not in path.parents or not path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"Audio for music track {track_id!r} is unavailable.",
            )
        return FileResponse(
            path,
            media_type="audio/mpeg",
            filename=path.name,
        )

    return router
