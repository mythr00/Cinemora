"""Context retrieval for the orchestrator (design assistant/04
§2.5): the compact, factual block attached to a turn.

Everything here is tool-grade data — the same values the READ
tools return, assembled from the same reads — never model-written
text. The pack is labelled with the project and run it came from,
and a project that is missing or no longer the caller's simply
yields no pack (the conversation continues unbound rather than
erroring or, worse, leaking).
"""

from __future__ import annotations


def project_context_pack(db, principal, project_id: str | None) -> dict | None:
    if not project_id:
        return None
    import api_v1
    from services import media_paths, run_ledger

    try:
        project = api_v1._get_owned_project_or_404(db, project_id, principal)
    except Exception:
        return None
    view = api_v1._project_view(db, project, detail=False)
    state = api_v1._production_state(media_paths.project_dir(project.id))
    latest = run_ledger.latest_run_for(project.id)
    pack = {
        "project": view,
        "latest_run": api_v1._run_view(latest),
        "current_stage": (state or {}).get("current_stage"),
        "stages": {
            name: {"status": record.get("status"), "error": record.get("error")}
            for name, record in ((state or {}).get("stages") or {}).items()
            if isinstance(record, dict)
        }
        if state
        else None,
    }
    # FS-H (FS-4): this pack is assembled in-process, bypassing the
    # HTTP response layer — pass it through the same FS-A sanitizer
    # so a non-admin session's context carries the generic failure
    # label instead of per-stage error text. Admins pass unchanged.
    return api_v1.sanitize_failure_detail(pack, principal)


def account_context_pack(principal) -> dict:
    return {
        "user": {
            "email": principal.email,
            "role": principal.role,
            "auth_method": principal.auth_method,
        }
    }
