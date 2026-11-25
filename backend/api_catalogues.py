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

from fastapi import APIRouter, Depends

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
    # no API change. There is deliberately NO music catalogue route —
    # the music library is founder-gated (P3) and stays unactivated;
    # the packs' music metadata fields (music_mood / mix_preset) are
    # likewise not catalogue data and are not exposed here.


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
        # The canonical id for an alias is the non-default catalogue id
        # backed by the same installed model files (today: "default"
        # aliases "en_US-lessac-medium"; both map to the one installed
        # Piper model). Derived from the catalogue itself, never named
        # literally here.
        canonical = None
        for other_id, (other_model, other_config) in narration.VOICE_CATALOGUE.items():
            if other_id == narration.DEFAULT_VOICE_ID:
                continue
            if other_model == model and other_config == config:
                canonical = other_id
                break
        is_default = voice_id == narration.DEFAULT_VOICE_ID
        return {
            "id": voice_id,
            "is_default": is_default,
            "alias_of": None if voice_id == canonical else canonical,
            "model": model.name,
            "model_file": model.name,
            "config_file": config.name,
            "installed": bool(model.exists() and config.exists()),
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

    return router
