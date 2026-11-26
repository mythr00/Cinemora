"""Generation billing hooks (Phase 8; directive §§5, 6, 16, 18).

Wired into the run ledger's choke points so NO submission path
can bypass billing:

- run_ledger.submit_production -> reserve_for_new_run (after the
  run row is inserted; a rejection fails the run and raises
  BillingRejected, which the API layer maps to 402/503);
- run_ledger.submit_revision_render -> reserve_for_revision
  (credits-only, same rejection contract);
- run_ledger.finish_run -> settle_for_run (first-finisher-wins
  makes settlement exactly-once);
- run_ledger.request_cancel (queued) and reconcile_stale_runs ->
  settle_for_run (full restoration).

Who is charged: the PROJECT OWNER (stamped onto the run by the
ledger choke point), whoever submitted — so the legacy operator
routes and the admin paths cannot bypass billing by construction.
Operator actions submitted through the admin surface carry
options billing_source="admin" and create zero-charge
reservations (recorded, never billed).

Retry semantics: every run row carries its OWN reservation. A
failed generation restores everything (credits + entitlement),
so an automatic dispatcher retry reserves afresh and the user is
charged exactly once — for the run that completes. At most one
reservation per run, and settlement is keyed to the finishing
run, so no chain can be charged twice.

Fail posture: plan resolution is fail-soft (an unseeded or
legacy deployment behaves exactly as before). Once a user is
PROVEN metered, every billing failure is fail-closed: the
submission is rejected rather than run unbilled.

Product Improvements directive, item 8 (founder, 2026-11-26 —
"zero credits must mean zero videos"): the legacy fail-soft
posture was a live bypass for real tenants. Registration assigns
every new account an active subscription to the seeded legacy
"free" plan (billing_mode='legacy', limits-only), so an ordinary
user with zero credits resolved to a non-metered plan and
generated without any reservation at all. The rule now:

- A REAL TENANT (a User row whose role is not staff) must pay for
  every generation: an active metered plan's entitlement/credits,
  or — when their plan is legacy or missing — a credits-only
  reservation covering the full estimated cost from whatever
  credits they actually hold (e.g. purchased top-ups). With
  neither, the submission is rejected (402 insufficient_credits).
- Staff accounts (admin / super_admin, incl. the founder plan)
  and the synthetic operator principal (no User row) keep the
  operator exemption: their submissions are not metered.
- Ownerless (pre-accounts) projects keep legacy behaviour.

Workers re-validate at claim time (claim_billing_check, called
from run_ledger.claim_run): an ordinary tenant's run with no
live reservation is refused and failed, never executed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import models
from database import SessionLocal
from services import plans as plans_service
from services.billing import catalogue, credits
from services.billing import periods as periods_service

TERMINAL_STATUSES = ("completed", "failed", "cancelled")


class BillingRejected(credits.BillingError):
    """A metered submission refused by billing. Carries the API
    error code + HTTP status the routes surface."""

    def __init__(self, error_code: str, message: str,
                 status_code: int = 402):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


def _session():
    return SessionLocal()


def _resolve_owner_plan(db, project_id):
    project = db.get(models.Project, str(project_id))
    if project is None or not project.owner_id:
        return None, None
    plan = plans_service.resolve_effective_plan(db, project.owner_id)
    return project, plan


STAFF_ROLES = ("admin", "super_admin")


def _owner_user(db, owner_id):
    """The owner's User row, or None when there is none (the
    synthetic operator principal on an unmigrated database).
    Fail-soft like the rest of this module: a database without
    the accounts tables has no tenants to meter."""
    if not owner_id:
        return None
    try:
        return db.get(models.User, str(owner_id))
    except Exception:
        db.rollback()
        return None


def _is_staff_user(user) -> bool:
    return user is not None and user.role in STAFF_ROLES


def _billing_exempt(db, owner_id, source: str) -> bool:
    """True when this owner keeps the operator exemption from
    metering: admin-source submissions, staff accounts, and
    owners with no User row (operator principal / pre-accounts
    projects). Ordinary tenants are NEVER exempt (item 8)."""
    if source == "admin":
        return True
    owner = _owner_user(db, owner_id)
    return owner is None or _is_staff_user(owner)


def _insufficient(needed: int, available: int) -> BillingRejected:
    return BillingRejected(
        "insufficient_credits",
        f"This generation needs {needed} credits but only "
        f"{available} are available. Subscribe to a plan or buy "
        "a top-up credit pack to generate.",
    )


def _reserve_credits_only(db, *, run_id, project_id, owner_id,
                          script_text, options):
    """Item 8 fallback for an ordinary tenant whose effective
    plan is legacy or missing: the generation is still possible
    — at full price, from credits the user actually holds
    (purchased top-ups spend first-class here). No credits, no
    generation: InsufficientCreditsError becomes a 402."""
    estimated_seconds = catalogue.estimate_seconds(script_text)
    credits_needed = catalogue.generation_credit_cost(
        estimated_seconds
    )
    try:
        reservation = _create_reservation(
            db, run_id=run_id,
            root_run_id=str((options or {}).get("retry_of") or run_id),
            user_id=owner_id, project_id=project_id,
            period=None, entitlement=False,
            credits_needed=credits_needed,
            estimated_seconds=estimated_seconds, source="user",
        )
        db.commit()
        return {
            "metered": True, "reservation_id": reservation.id,
            "entitlement": False,
            "credits_reserved": reservation.credits_reserved,
            "source": "user", "credits_only": True,
        }
    except credits.InsufficientCreditsError as exc:
        db.rollback()
        raise _insufficient(exc.needed, exc.available) from exc
    except BillingRejected:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise BillingRejected(
            "billing_unavailable",
            "Billing is temporarily unavailable; the "
            "generation was not started and nothing was "
            "charged.",
            status_code=503,
        ) from exc


def _check_subscription_health(db, user_id: str) -> None:
    """Refuse metered submissions on a non-active metered
    subscription (past_due / cancelled / expired) instead of
    letting plan resolution fall through to a default plan.

    Fail-soft on a database without the accounts tables, exactly
    like plans.resolve_effective_plan (this module's stated fail
    posture: an unseeded or legacy deployment behaves exactly as
    before). Without the subscription table there is no metered
    subscription to be unhealthy — and a raw OperationalError
    here turned run submission on legacy fixture DBs into a 500.
    """
    try:
        latest = (
            db.query(models.UserSubscription)
            .filter_by(user_id=user_id)
            .order_by(models.UserSubscription.created_at.desc())
            .first()
        )
    except Exception:
        db.rollback()
        return
    if latest is None or latest.status == "active":
        return
    plan = db.get(models.Plan, latest.plan_id)
    if plan is None or plan.billing_mode != "metered":
        return
    if latest.status == "past_due":
        raise BillingRejected(
            "subscription_past_due",
            "Your subscription payment is past due. Update your "
            "payment method to keep generating.",
        )
    raise BillingRejected(
        "subscription_inactive",
        "Your subscription is not active. Resubscribe to keep "
        "generating.",
    )


def _create_reservation(
    db,
    *,
    run_id: str,
    root_run_id: str,
    user_id: str,
    project_id: str,
    period,
    entitlement: bool,
    credits_needed: int,
    estimated_seconds: float,
    source: str,
) -> models.GenerationReservation:
    reservation = models.GenerationReservation(
        id=str(uuid.uuid4()),
        run_id=str(run_id),
        root_run_id=str(root_run_id),
        user_id=user_id,
        project_id=str(project_id),
        billing_period_id=period.id if period else None,
        entitlement_reserved=entitlement,
        credits_reserved=0,
        estimated_seconds=estimated_seconds,
        source=source,
        status="active",
    )
    db.add(reservation)
    db.flush()
    if credits_needed > 0:
        split = credits.reserve(
            db,
            user_id,
            credits_needed,
            reservation_id=reservation.id,
            run_id=str(run_id),
            project_id=str(project_id),
            billing_period_id=period.id if period else None,
            source=source,
        )
        reservation.credits_reserved = sum(split.values())
    return reservation


def reserve_for_new_run(run_id, project_id, script_text, options=None):
    """Reserve billing for a freshly inserted production run.

    Returns None when the project owner is not on a metered plan
    (legacy behaviour, unchanged), else a summary dict. Raises
    BillingRejected when a metered submission cannot be billed.
    """
    options = options or {}
    db = _session()
    try:
        project, plan = _resolve_owner_plan(db, project_id)
        if project is None:
            return None
        owner_id = project.owner_id
        source = (
            "admin"
            if options.get("billing_source") == "admin"
            else "user"
        )
        if source == "user":
            # Health before the metered check: a past-due /
            # cancelled METERED subscription must refuse here,
            # not fall through plan resolution to a default plan.
            _check_subscription_health(db, owner_id)
        if plan is None or plan.billing_mode != "metered":
            # Item 8 (2026-11-26): a legacy/missing plan is no
            # longer free admission for a real tenant. Staff and
            # the operator principal keep the exemption; an
            # ordinary tenant pays full price from held credits
            # or is rejected — zero credits means zero videos.
            if _billing_exempt(db, owner_id, source):
                return None
            return _reserve_credits_only(
                db, run_id=run_id, project_id=project_id,
                owner_id=owner_id, script_text=script_text,
                options=options,
            )
        try:
            root_run_id = str(options.get("retry_of") or run_id)
            estimated_seconds = catalogue.estimate_seconds(script_text)

            if source == "admin":
                reservation = _create_reservation(
                    db, run_id=run_id, root_run_id=root_run_id,
                    user_id=owner_id, project_id=project_id,
                    period=None, entitlement=False, credits_needed=0,
                    estimated_seconds=estimated_seconds, source="admin",
                )
                db.commit()
                return {
                    "metered": True, "reservation_id": reservation.id,
                    "entitlement": False, "credits_reserved": 0,
                    "source": "admin",
                }

            period = periods_service.ensure_current_period(db, owner_id)

            entitlement = False
            credits_needed: int
            if period is not None and periods_service.consume_allowance(
                db, period, "generations"
            ):
                entitlement = True
                excess = estimated_seconds - catalogue.STANDARD_GENERATION_SECONDS
                credits_needed = (
                    catalogue.generation_credit_cost(excess)
                    if excess > 0
                    else 0
                )
            else:
                credits_needed = catalogue.generation_credit_cost(
                    estimated_seconds
                )

            reservation = _create_reservation(
                db, run_id=run_id, root_run_id=root_run_id,
                user_id=owner_id, project_id=project_id,
                period=period, entitlement=entitlement,
                credits_needed=credits_needed,
                estimated_seconds=estimated_seconds, source="user",
            )
            db.commit()
            return {
                "metered": True, "reservation_id": reservation.id,
                "entitlement": entitlement,
                "credits_reserved": reservation.credits_reserved,
                "source": "user",
            }
        except BillingRejected:
            db.rollback()
            raise
        except credits.InsufficientCreditsError as exc:
            db.rollback()
            raise BillingRejected(
                "insufficient_credits",
                f"This generation needs {exc.needed} credits but "
                f"only {exc.available} are available. Top up or "
                "upgrade your plan to generate.",
            ) from exc
        except Exception as exc:
            db.rollback()
            raise BillingRejected(
                "billing_unavailable",
                "Billing is temporarily unavailable; the "
                "generation was not started and nothing was "
                "charged.",
                status_code=503,
            ) from exc
    finally:
        db.close()


def reserve_for_revision(run_id, project_id, script_text):
    """Reserve credits for an editor revision render (credits
    only — renders never consume generation entitlements).
    Same contract as reserve_for_new_run."""
    db = _session()
    try:
        project, plan = _resolve_owner_plan(db, project_id)
        if project is None:
            return None
        owner_id = project.owner_id
        _check_subscription_health(db, owner_id)
        metered = plan is not None and plan.billing_mode == "metered"
        if not metered and _billing_exempt(db, owner_id, "user"):
            return None
        try:
            # Ensure the period so the subscription's monthly
            # credits are allocated before the render draws on
            # the wallet. (Metered plans only: an unmetered tenant
            # pays credits-only, item 8, and has no period.)
            if metered:
                periods_service.ensure_current_period(db, owner_id)
            estimated_seconds = catalogue.estimate_seconds(script_text)
            credits_needed = catalogue.generation_credit_cost(
                estimated_seconds
            )
            reservation = _create_reservation(
                db, run_id=run_id, root_run_id=run_id,
                user_id=owner_id, project_id=project_id,
                period=None, entitlement=False,
                credits_needed=credits_needed,
                estimated_seconds=estimated_seconds, source="user",
            )
            db.commit()
            return {
                "metered": True, "reservation_id": reservation.id,
                "entitlement": False,
                "credits_reserved": reservation.credits_reserved,
                "source": "user",
            }
        except BillingRejected:
            db.rollback()
            raise
        except credits.InsufficientCreditsError as exc:
            db.rollback()
            raise BillingRejected(
                "insufficient_credits",
                f"This render needs {exc.needed} credits but only "
                f"{exc.available} are available.",
            ) from exc
        except Exception as exc:
            db.rollback()
            raise BillingRejected(
                "billing_unavailable",
                "Billing is temporarily unavailable; the render "
                "was not started and nothing was charged.",
                status_code=503,
            ) from exc
    finally:
        db.close()


def _extract_actual_seconds(reservation, execution) -> float:
    if isinstance(execution, dict):
        for key in (
            "duration_seconds",
            "final_duration_seconds",
            "video_duration_seconds",
        ):
            value = execution.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
    if reservation.estimated_seconds:
        return float(reservation.estimated_seconds)
    return float(catalogue.STANDARD_GENERATION_SECONDS)


def claim_billing_check(run_id) -> tuple[bool, str | None]:
    """Worker-side re-validation (item 8): may this run execute?

    Called from run_ledger.claim_run after the atomic claim. An
    ordinary tenant's run must carry a live reservation (active,
    or settled by an earlier finisher) — created at submission,
    or at begin_run for the fail-soft caller-less path. A run
    with NO reservation was never billed (legacy bypass, direct
    ledger insert, or a reservation lost before this fix) and is
    refused: (False, "insufficient_credits").

    Exempt, mirroring reserve-time rules: staff owners, owners
    with no User row (operator principal), ownerless projects,
    and admin-source reservations (which exist as zero-charge
    rows, so they pass on their own). Infrastructure errors are
    fail-soft (True): a transient read failure must not kill a
    legitimately billed run — only a PROVEN missing reservation
    refuses.
    """
    if not run_id:
        return True, None
    db = _session()
    try:
        run_row = db.get(models.ProductionRun, str(run_id))
        if run_row is None:
            return True, None
        owner_id = run_row.owner_id
        if not owner_id and run_row.project_id:
            project = db.get(models.Project, str(run_row.project_id))
            owner_id = project.owner_id if project else None
        if not owner_id:
            return True, None
        owner = _owner_user(db, owner_id)
        if owner is None or _is_staff_user(owner):
            return True, None
        reservation = (
            db.query(models.GenerationReservation)
            .filter_by(run_id=str(run_id))
            .one_or_none()
        )
        hops = 0
        while reservation is None and run_row is not None and hops < 10:
            options = run_row.options or {}
            parent = options.get("retry_of")
            if not parent:
                break
            reservation = (
                db.query(models.GenerationReservation)
                .filter_by(run_id=str(parent))
                .one_or_none()
            )
            run_row = db.get(models.ProductionRun, str(parent))
            hops += 1
        if reservation is not None and reservation.status in (
            "active",
            "settled",
        ):
            return True, None
        return False, "insufficient_credits"
    except Exception as error:  # fail-soft: see docstring
        print(
            f"WARNING: claim billing check failed for run "
            f"{run_id}: {error}"
        )
        try:
            db.rollback()
        except Exception:
            pass
        return True, None
    finally:
        db.close()


def settle_for_run(run_id, status, execution=None) -> None:
    """Settle or restore the reservation for a finished run.

    Fail-soft by contract (it is called from the ledger's finish
    paths, which must never be disturbed): internal failures are
    printed, and the reservation stays 'active' for the lifecycle
    sweeper to reconcile rather than losing the user's money
    silently. Idempotent: a non-active reservation is untouched,
    and a run that is not actually terminal is never settled.
    """
    if not run_id:
        return
    db = _session()
    try:
        reservation = (
            db.query(models.GenerationReservation)
            .filter_by(run_id=str(run_id))
            .one_or_none()
        )
        if reservation is None:
            # A retry finishes under its own run id only when its
            # reservation was created for the chain root (pre-Phase-8
            # chains); walk the retry links to find it.
            run_row = db.get(models.ProductionRun, str(run_id))
            hops = 0
            while reservation is None and run_row is not None and hops < 10:
                options = run_row.options or {}
                parent = options.get("retry_of")
                if not parent:
                    break
                reservation = (
                    db.query(models.GenerationReservation)
                    .filter_by(run_id=str(parent))
                    .one_or_none()
                )
                run_row = db.get(models.ProductionRun, str(parent))
                hops += 1
        if reservation is None or reservation.status != "active":
            return
        run_row = db.get(models.ProductionRun, str(run_id))
        if run_row is not None and run_row.status not in TERMINAL_STATUSES:
            return

        if status == "completed":
            actual = _extract_actual_seconds(reservation, execution)
            if reservation.entitlement_reserved:
                excess = actual - catalogue.STANDARD_GENERATION_SECONDS
                cost = (
                    catalogue.generation_credit_cost(excess)
                    if excess > 0
                    else 0
                )
            else:
                cost = catalogue.generation_credit_cost(actual)
            charged = 0
            if cost > 0:
                result = credits.charge_reservation(
                    db,
                    reservation.user_id,
                    reservation_id=reservation.id,
                    total_charge=cost,
                    run_id=reservation.run_id,
                    project_id=reservation.project_id,
                    billing_period_id=reservation.billing_period_id,
                    source="system",
                )
                charged = int(result["charged"])
            if reservation.credits_reserved:
                credits.release_reservation(
                    db,
                    reservation.user_id,
                    reservation_id=reservation.id,
                    run_id=reservation.run_id,
                    project_id=reservation.project_id,
                    source="system",
                    reason="unused reservation released at settlement",
                )
            reservation.status = "settled"
            reservation.charged_credits = charged
            reservation.actual_seconds = actual
        else:
            if reservation.credits_reserved:
                credits.release_reservation(
                    db,
                    reservation.user_id,
                    reservation_id=reservation.id,
                    run_id=reservation.run_id,
                    project_id=reservation.project_id,
                    source="system",
                    reason=f"generation {status}: full restoration",
                )
            if reservation.entitlement_reserved and reservation.billing_period_id:
                period = db.get(
                    models.BillingPeriod, reservation.billing_period_id
                )
                if period is not None:
                    periods_service.release_allowance(
                        db, period, "generations"
                    )
            reservation.status = "released"
            reservation.charged_credits = 0
        reservation.settled_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as error:  # fail-soft: never disturb the finish
        try:
            db.rollback()
        except Exception:
            pass
        print(f"WARNING: billing settlement failed for run {run_id}: {error}")
    finally:
        db.close()
