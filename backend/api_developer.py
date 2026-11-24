"""Developer API management routes (founder priority #16).

Mounted from main.py under /api/v1, alongside api_admin and
api_assistant. These are the ACCOUNT surfaces of the Developer
API — creating/revoking keys and webhooks, reading deliveries,
and the documentation payload — authenticated by the normal
api_v1.require_user (session or operator token). API keys
themselves never authenticate here: a key manages nothing.

Creation responses are the only place a secret ever appears:
POST /account/api-keys returns the key secret once, POST
/account/webhooks returns the signing secret once. Every list and
read returns the stored non-secret views only.

Issuance and revocation land in the admin audit log
(services/admin_audit) with the acting principal, so the Admin
Dashboard's audit viewer shows them alongside every other
recorded action.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api_v1 import ApiError, require_user
from database import SessionLocal
from services import admin_audit, api_keys, developer_docs, webhooks
from services import auth as auth_service

router = APIRouter()


class ApiKeyCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(min_length=1)
    expires_at: datetime | None = None
    rate_limit_per_minute: int | None = Field(
        default=None,
        ge=api_keys.MIN_RATE_LIMIT,
        le=api_keys.MAX_RATE_LIMIT,
    )


class WebhookCreateIn(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    events: list[str] = Field(min_length=1)


def _validation_error(error: ValueError) -> ApiError:
    return ApiError(422, "validation_error", str(error))


# ---------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------


@router.post("/account/api-keys", status_code=201)
def create_api_key(
    body: ApiKeyCreateIn,
    principal: auth_service.Principal = Depends(require_user),
):
    if not body.name.strip():
        raise ApiError(
            422, "validation_error", "A key name is required."
        )
    if body.expires_at is not None:
        expires = body.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            raise ApiError(
                422,
                "validation_error",
                "expires_at must be in the future.",
            )
    db = SessionLocal()
    try:
        try:
            row, secret = api_keys.create_key(
                db,
                principal.user_id,
                name=body.name.strip(),
                scopes=body.scopes,
                expires_at=body.expires_at,
                rate_limit_per_minute=body.rate_limit_per_minute,
            )
        except ValueError as error:
            raise _validation_error(error) from error
        view = api_keys.key_view(row)
    finally:
        db.close()
    admin_audit.record(
        principal,
        "api_key.created",
        target_type="api_key",
        target_id=view["id"],
        detail={
            "name": view["name"],
            "scopes": view["scopes"],
            "key_prefix": view["key_prefix"],
        },
    )
    # The secret appears in exactly this one response, ever.
    return {**view, "secret": secret}


@router.get("/account/api-keys")
def list_api_keys(
    principal: auth_service.Principal = Depends(require_user),
):
    db = SessionLocal()
    try:
        rows = api_keys.list_keys(db, principal.user_id)
        return {
            "keys": [api_keys.key_view(row) for row in rows],
            "max_keys_per_user": api_keys.MAX_KEYS_PER_USER,
            "default_rate_limit_per_minute": api_keys.default_rate_limit(),
        }
    finally:
        db.close()


@router.delete("/account/api-keys/{key_id}")
def revoke_api_key(
    key_id: str,
    principal: auth_service.Principal = Depends(require_user),
):
    db = SessionLocal()
    try:
        row = api_keys.revoke_key(db, principal.user_id, key_id)
        if row is None:
            raise ApiError(
                404, "key_not_found", "No such API key on this account."
            )
        view = api_keys.key_view(row)
    finally:
        db.close()
    admin_audit.record(
        principal,
        "api_key.revoked",
        target_type="api_key",
        target_id=view["id"],
        detail={"name": view["name"], "key_prefix": view["key_prefix"]},
    )
    return view


# ---------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------


@router.post("/account/webhooks", status_code=201)
def create_webhook(
    body: WebhookCreateIn,
    principal: auth_service.Principal = Depends(require_user),
):
    db = SessionLocal()
    try:
        try:
            row, secret = webhooks.create_endpoint(
                db,
                principal.user_id,
                url=body.url.strip(),
                events=body.events,
            )
        except ValueError as error:
            raise _validation_error(error) from error
        view = webhooks.endpoint_view(row)
    finally:
        db.close()
    admin_audit.record(
        principal,
        "webhook.created",
        target_type="webhook_endpoint",
        target_id=view["id"],
        detail={"url": view["url"], "events": view["events"]},
    )
    # The signing secret appears in exactly this one response, ever.
    return {**view, "signing_secret": secret}


@router.get("/account/webhooks")
def list_webhooks(
    principal: auth_service.Principal = Depends(require_user),
):
    db = SessionLocal()
    try:
        rows = webhooks.list_endpoints(db, principal.user_id)
        return {
            "endpoints": [webhooks.endpoint_view(row) for row in rows],
            "events": sorted(webhooks.VALID_EVENTS),
        }
    finally:
        db.close()


@router.delete("/account/webhooks/{endpoint_id}")
def disable_webhook(
    endpoint_id: str,
    principal: auth_service.Principal = Depends(require_user),
):
    db = SessionLocal()
    try:
        row = webhooks.disable_endpoint(db, principal.user_id, endpoint_id)
        if row is None:
            raise ApiError(
                404,
                "webhook_not_found",
                "No such webhook endpoint on this account.",
            )
        view = webhooks.endpoint_view(row)
    finally:
        db.close()
    admin_audit.record(
        principal,
        "webhook.disabled",
        target_type="webhook_endpoint",
        target_id=view["id"],
        detail={"url": view["url"]},
    )
    return view


@router.get("/account/webhooks/{endpoint_id}/deliveries")
def list_webhook_deliveries(
    endpoint_id: str,
    limit: int = 50,
    principal: auth_service.Principal = Depends(require_user),
):
    db = SessionLocal()
    try:
        rows = webhooks.list_deliveries(
            db, principal.user_id, endpoint_id, limit=limit
        )
        if rows is None:
            raise ApiError(
                404,
                "webhook_not_found",
                "No such webhook endpoint on this account.",
            )
        return {
            "deliveries": [webhooks.delivery_view(row) for row in rows]
        }
    finally:
        db.close()


# ---------------------------------------------------------------------
# Documentation
# ---------------------------------------------------------------------


@router.get("/developer/docs")
def get_developer_docs(
    principal: auth_service.Principal = Depends(require_user),
):
    """The documentation payload, generated from the public
    router's live route table (services/developer_docs)."""
    try:
        return developer_docs.build_docs()
    except RuntimeError as error:
        raise ApiError(500, "docs_catalogue_mismatch", str(error)) from error
