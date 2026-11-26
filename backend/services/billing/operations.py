"""Priced AI operations + generation estimates (Phase 8;
directive §§5, 7).

Script Studio and thumbnail operations are charged on a
begin/refund contract with the routes:

- begin_operation consumes one allowance unit when the user's
  current period has one left; otherwise it debits the
  operation's credit price from the wallet. Item 8 extension
  (2026-11-26): non-metered (legacy/no-plan) ORDINARY accounts
  are charged the same listed credit price — the old unmetered
  posture was the same bypass class as free video generation.
  Only staff/operator accounts are unmetered (receipt None).
- The route runs the operation; on ANY failure it calls
  refund_operation, which returns the allowance unit or refunds
  the debited credits to their original buckets. A user is never
  charged for a failed operation, and never gets a successful
  operation free.

send_to_production is deliberately NOT an operation: the
generation itself is billed at the run-ledger choke point, and
charging for the hand-off too would bill one action twice.
"""

from __future__ import annotations

import uuid

import models
from database import SessionLocal
from services import plans as plans_service
from services.billing import catalogue, credits
from services.billing import periods as periods_service
from services.billing.generation_billing import BillingRejected

_KIND_TABLES = {
    "scripts": catalogue.SCRIPT_OPERATION_CREDITS,
    "thumbnails": catalogue.THUMBNAIL_OPERATION_CREDITS,
}


def _session():
    return SessionLocal()


def begin_operation(user_id: str, kind: str, operation: str):
    """Consume an allowance unit or debit credits for one AI
    operation. Returns a receipt dict, or None for unmetered
    (legacy-plan) users. Raises BillingRejected when a metered
    user can neither use an allowance nor pay the credit price.
    """
    table = _KIND_TABLES[kind]
    if operation not in table:
        raise credits.BillingError(
            f"unknown {kind} operation {operation!r}"
        )
    db = _session()
    try:
        plan = plans_service.resolve_effective_plan(db, user_id)
        if plan is None or plan.billing_mode != "metered":
            from services.billing import generation_billing

            user_row = db.get(models.User, user_id)
            if user_row is None or (
                (user_row.role or "")
                in generation_billing.STAFF_ROLES
            ):
                return None
            return _charge_operation_credits(
                db, user_id, kind, operation, int(table[operation]),
                allowance_exhausted=False,
            )
        period = periods_service.ensure_current_period(db, user_id)
        if period is not None and periods_service.consume_allowance(
            db, period, kind
        ):
            db.commit()
            return {
                "via": "allowance",
                "kind": kind,
                "operation": operation,
                "period_id": period.id,
                "credits_charged": 0,
            }
        return _charge_operation_credits(
            db, user_id, kind, operation, int(table[operation]),
            allowance_exhausted=True,
        )
    finally:
        db.close()


def _charge_operation_credits(db, user_id, kind, operation, price,
                              *, allowance_exhausted: bool):
    """Debit one operation's listed credit price, or raise the
    customer-facing BillingRejected. Shared by the metered path
    (allowance used up) and the non-metered path (item 8: an
    ordinary account pays the price or does not run)."""
    key = f"op:{uuid.uuid4().hex}"
    try:
        split = credits.spend(
            db,
            user_id,
            price,
            idempotency_key=key,
            reason=f"{kind} operation: {operation}",
            details={"operation": operation, "kind": kind},
        )
    except credits.InsufficientCreditsError as exc:
        db.rollback()
        message = (
            f"This {kind[:-1]} operation needs {exc.needed} "
            f"credits but only {exc.available} are available."
        )
        if allowance_exhausted:
            message += (
                " Your included allowance for this period is "
                "used up."
            )
        message += (
            " Subscribe to a plan or buy a top-up credit pack "
            "to continue."
        )
        raise BillingRejected(
            "insufficient_credits", message
        ) from exc
    db.commit()
    return {
        "via": "credits",
        "kind": kind,
        "operation": operation,
        "split": split,
        "key": key,
        "credits_charged": price,
    }


def refund_operation(user_id: str, receipt) -> None:
    """Reverse a begin_operation after the operation failed.
    Fail-soft (a refund failure is printed, never raised into the
    error path that is already reporting the real failure)."""
    if not receipt:
        return
    db = _session()
    try:
        if receipt["via"] == "allowance":
            period = db.get(models.BillingPeriod, receipt["period_id"])
            if period is not None:
                periods_service.release_allowance(
                    db, period, receipt["kind"]
                )
        else:
            for bucket, amount in (receipt.get("split") or {}).items():
                if amount > 0:
                    credits.grant(
                        db,
                        user_id,
                        bucket,
                        amount,
                        entry_type="refund",
                        idempotency_key=(
                            f"{receipt['key']}:refund:{bucket}"
                        ),
                        source="system",
                        reason=(
                            "failed operation refund: "
                            f"{receipt['kind']}/{receipt['operation']}"
                        ),
                    )
            account = credits.get_or_create_account(db, user_id)
            account.lifetime_spent = max(
                0,
                int(account.lifetime_spent)
                - int(receipt.get("credits_charged") or 0),
            )
        db.commit()
    except Exception as error:
        try:
            db.rollback()
        except Exception:
            pass
        print(
            f"WARNING: operation refund failed for user {user_id}: "
            f"{error}"
        )
    finally:
        db.close()


def estimate_for_project(user_id: str, project_id: str) -> dict:
    """The pre-generation estimate (directive §5): measured from
    the project's actual script, showing exactly what a Generate
    click will consume — an entitlement when one remains (plus
    credit cost for estimated minutes beyond the standard 15),
    or the full credit cost. Legacy users get metered=False and
    no numbers are invented for them."""
    db = _session()
    try:
        project = db.get(models.Project, str(project_id))
        if project is None:
            raise LookupError(f"No project {project_id}.")
        plan = plans_service.resolve_effective_plan(db, user_id)
        seconds = catalogue.estimate_seconds(project.script or "")
        result = {
            "project_id": str(project_id),
            "estimated_seconds": round(seconds, 1),
            "estimated_minutes": round(seconds / 60.0, 2),
            "metered": bool(
                plan is not None and plan.billing_mode == "metered"
            ),
            "plan_name": plan.name if plan is not None else None,
            "standard_generation_credits":
                catalogue.standard_generation_credits(),
            "duration_cost_table": catalogue.duration_cost_table(),
        }
        if not result["metered"]:
            return result
        # Ensure (not just read) the period: the estimate is what
        # a Generate click will face, and generation ensures the
        # period lazily too — creating it here (committed below)
        # keeps the two in lockstep for a first-time subscriber.
        period = periods_service.ensure_current_period(db, user_id)
        entitlements_left = periods_service.allowance_remaining(
            period, "generations"
        )
        account = credits.get_or_create_account(db, user_id)
        excess = seconds - catalogue.STANDARD_GENERATION_SECONDS
        if entitlements_left > 0:
            result.update(
                {
                    "will_use_entitlement": True,
                    "entitlements_remaining": entitlements_left,
                    "credits_needed": (
                        catalogue.generation_credit_cost(excess)
                        if excess > 0
                        else 0
                    ),
                }
            )
        else:
            result.update(
                {
                    "will_use_entitlement": False,
                    "entitlements_remaining": 0,
                    "credits_needed":
                        catalogue.generation_credit_cost(seconds),
                }
            )
        result["credits_available"] = credits.total_available(account)
        result["can_afford"] = (
            result["credits_available"] >= result["credits_needed"]
        )
        db.commit()
        return result
    finally:
        db.close()
