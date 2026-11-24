"""Developer API — the public surface (founder priority #16;
gap-audit doc 06 Phase N).

Mounted from main.py under /api/public/v1. This is an EXPOSURE of
the existing internal API, not a second implementation: every
handler delegates to the corresponding api_v1 route function with
a principal resolved from a per-user API key, so ownership choke
points (_get_owned_project_or_404 and friends), plan admission,
deduplication and response shapes are exactly the product's own.

Authentication is key-only and deliberately separate from
api_v1.require_user: sessions and the legacy operator token do
not authenticate here, and API keys do not authenticate anywhere
else. The dual-auth window in api_v1 is untouched.

Per route, in order: the presented key must resolve (unknown /
revoked / expired / disabled owner -> 401), carry the route's
scope (else 403 insufficient_scope), and fit the key's per-minute
rate budget (else 429, the codebase's existing limiter). Every
authenticated request is recorded against the key's usage
counters from this real traffic.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

import api_v1
from api_v1 import ApiError
from database import SessionLocal
from services import api_keys
from services import auth as auth_service

router = APIRouter()

# The dedicated per-key limiter (api_v1's limiter class, keyed by
# key id rather than user id: two keys of one user have separate
# budgets, as the key records promise).
public_limiter = api_v1._RateLimiter()

# The documented catalogue, appended by @public_route as routes are
# defined below. services/developer_docs.py builds the in-product
# documentation from this list cross-checked against the live
# route table — an entry that does not resolve to a real route is
# a build error there, never a documented fiction.
PUBLIC_ROUTES: list[dict] = []

BASE_PATH = "/api/public/v1"


def require_public_key(required_scope: str):
    """Dependency factory: resolve the X-API-Key header to a
    principal carrying the key's scopes, enforcing `required_scope`
    and the key's rate budget."""

    def dependency(request: Request) -> auth_service.Principal:
        presented = request.headers.get("X-API-Key") or ""
        if not presented:
            raise ApiError(
                401,
                "unauthorized",
                "Provide a Developer API key in the X-API-Key header.",
            )
        db = SessionLocal()
        try:
            resolved = api_keys.resolve_key(db, presented)
            if resolved is None:
                raise ApiError(
                    401,
                    "unauthorized",
                    "The presented API key is invalid, revoked or "
                    "expired.",
                )
            key_row, user = resolved
            if required_scope not in list(key_row.scopes or []):
                raise ApiError(
                    403,
                    "insufficient_scope",
                    f"This route requires the '{required_scope}' "
                    "scope, which this key does not carry.",
                )
            limit = (
                key_row.rate_limit_per_minute
                if key_row.rate_limit_per_minute is not None
                else api_keys.default_rate_limit()
            )
            public_limiter.check(f"apikey:{key_row.id}", limit=limit)
            api_keys.record_use(db, key_row.id)
            return auth_service.Principal(
                user_id=user.id,
                email=user.email,
                role=user.role,
                auth_method="api_key",
                api_key_id=key_row.id,
                scopes=tuple(key_row.scopes or []),
            )
        finally:
            db.close()

    return dependency


def public_route(method: str, path: str, scope: str, summary: str,
                 status_code: int | None = None):
    """Register a public route and record it in the documented
    catalogue in the same breath, so the docs can never drift from
    the surface."""

    def wrap(fn):
        PUBLIC_ROUTES.append(
            {
                "method": method,
                "path": BASE_PATH + path,
                "scope": scope,
                "summary": summary,
            }
        )
        kwargs = {}
        if status_code is not None:
            kwargs["status_code"] = status_code
        getattr(router, method.lower())(path, **kwargs)(fn)
        return fn

    return wrap


# ---------------------------------------------------------------------
# Projects + runs (the audit's documented subset: projects/runs/outputs)
# ---------------------------------------------------------------------


@public_route("GET", "/projects", "read", "List your projects.")
def list_projects(principal=Depends(require_public_key("read"))):
    return api_v1.list_projects(principal)


@public_route(
    "POST", "/projects", "write", "Create a project.", status_code=201
)
def create_project(
    body: api_v1.ProjectCreateIn,
    principal=Depends(require_public_key("write")),
):
    return api_v1.create_project(body, principal)


@public_route("GET", "/projects/{project_id}", "read", "Get one project.")
def get_project(
    project_id: str, principal=Depends(require_public_key("read"))
):
    return api_v1.get_project(project_id, principal)


@public_route(
    "GET",
    "/projects/{project_id}/status",
    "read",
    "Get a project's production status and latest run.",
)
def get_project_status(
    project_id: str, principal=Depends(require_public_key("read"))
):
    return api_v1.get_project_status(project_id, principal)


@public_route(
    "POST",
    "/projects/{project_id}/runs",
    "write",
    "Submit a production run for a project.",
    status_code=202,
)
def submit_run(
    project_id: str,
    body: api_v1.RunSubmitIn | None = None,
    principal=Depends(require_public_key("write")),
):
    return api_v1.submit_run(project_id, body, principal)


@public_route(
    "GET", "/projects/{project_id}/runs", "read", "List a project's runs."
)
def list_project_runs(
    project_id: str, principal=Depends(require_public_key("read"))
):
    return api_v1.list_project_runs(project_id, principal)


@public_route("GET", "/runs/{run_id}", "read", "Get one run by id.")
def get_run(run_id: str, principal=Depends(require_public_key("read"))):
    return api_v1.get_run(run_id, principal)


@public_route(
    "POST", "/runs/{run_id}/cancel", "write", "Request a run's cancellation."
)
def cancel_run(
    run_id: str, principal=Depends(require_public_key("write"))
):
    return api_v1.cancel_run(run_id, principal)


# ---------------------------------------------------------------------
# Outputs + reports
# ---------------------------------------------------------------------


@public_route(
    "GET",
    "/projects/{project_id}/outputs",
    "read",
    "Get a project's recorded outputs (film, captions, reports).",
)
def get_outputs(
    project_id: str, principal=Depends(require_public_key("read"))
):
    return api_v1.get_outputs(project_id, principal)


@public_route(
    "GET",
    "/projects/{project_id}/qa-report",
    "read",
    "Get the QA report for a project's latest production run.",
)
def get_latest_qa_report(
    project_id: str, principal=Depends(require_public_key("read"))
):
    return api_v1.get_latest_qa_report(project_id, principal)


@public_route(
    "GET",
    "/projects/{project_id}/runs/{run_id}/qa-report",
    "read",
    "Get the QA report for one run.",
)
def get_run_qa_report(
    project_id: str,
    run_id: str,
    principal=Depends(require_public_key("read")),
):
    return api_v1.get_run_qa_report(project_id, run_id, principal)


@public_route(
    "GET",
    "/projects/{project_id}/versions",
    "read",
    "Get a project's version timeline (runs, revisions, scripts).",
)
def get_versions(
    project_id: str, principal=Depends(require_public_key("read"))
):
    return api_v1.get_versions(project_id, principal)


# ---------------------------------------------------------------------
# The calling key's own usage
# ---------------------------------------------------------------------


@public_route(
    "GET", "/usage", "read", "Get the calling key's limits and usage."
)
def get_usage(principal=Depends(require_public_key("read"))):
    db = SessionLocal()
    try:
        row = api_keys.get_owned_key(
            db, principal.user_id, principal.api_key_id
        )
        if row is None:
            raise ApiError(
                404, "key_not_found", "The calling key no longer exists."
            )
        view = api_keys.key_view(row)
        view["requests_last_minute"] = public_limiter.peek(
            f"apikey:{row.id}"
        )
        return {"key": view}
    finally:
        db.close()
