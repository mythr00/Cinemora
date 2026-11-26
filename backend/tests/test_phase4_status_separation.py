"""Phase 4 — technical_status / release_status separation.

Covers Workstream A:

- services/release_status.py classification (fail-closed) and
  derive_run_statuses;
- run_ledger.finish_run persisting the three new columns and
  get_run returning them;
- THE BILLING SWITCH POINT
  (services/billing/generation_billing.release_settlement_policy):
  a completed-but-not-release-grade run is, under the default
  "restore" policy, restored EXACTLY like a failure (today's
  behaviour); under "settle" it is charged at the tier rate for
  the delivered duration; a release-grade completion is charged
  unchanged; and money moves exactly once however the finish is
  repeated.
"""

import json
import uuid
from datetime import datetime, timezone

import pytest

import models
from database import Base, SessionLocal, engine
from services import release_status, run_ledger
from services.billing import catalogue, credits, periods
from services.billing import generation_billing

Base.metadata.create_all(bind=engine)

SCRIPT_15MIN = " ".join(["word"] * 2175)  # 900s at 145 wpm


# ---------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        "audit_not_evaluated: no asset report was supplied",
        "missing_mandatory_evidence: sentence 3 has no evidence",
        "provenance_mismatch: soundtrack records claim X",
    ],
)
def test_technical_codes_classify_technical(failure):
    assert (
        release_status.classify_audit_failure(failure)
        == release_status.TECHNICAL
    )
    assert release_status.audit_failure_code(failure) in (
        release_status.TECHNICAL_AUDIT_CODES
    )


@pytest.mark.parametrize(
    "failure",
    [
        "coverage_incomplete: sentences 2, 5 unsearched",
        "prohibited_substitution: text_cards=2",
        "unclassified_visuals: 3 visuals have no classification",
        "illustrative_presented_as_evidence: sentence 7",
    ],
)
def test_release_codes_classify_release(failure):
    assert (
        release_status.classify_audit_failure(failure)
        == release_status.RELEASE
    )
    assert release_status.audit_failure_code(failure) in (
        release_status.RELEASE_AUDIT_CODES
    )


def test_unknown_code_is_technical_fail_closed():
    assert (
        release_status.classify_audit_failure(
            "some_future_failure: details"
        )
        == release_status.TECHNICAL
    )


def test_audit_failure_code_prefix_rules():
    assert (
        release_status.audit_failure_code("coverage_incomplete: x: y")
        == "coverage_incomplete"
    )
    assert release_status.audit_failure_code("no_colon") == "no_colon"
    assert release_status.audit_failure_code("  spaced : y") == "spaced"
    assert release_status.audit_failure_code("") == ""


def test_split_preserves_order():
    failures = [
        "coverage_incomplete: a",
        "provenance_mismatch: b",
        "prohibited_substitution: c",
        "missing_mandatory_evidence: d",
    ]
    split = release_status.split_audit_failures(failures)
    assert split == {
        "technical": [
            "provenance_mismatch: b",
            "missing_mandatory_evidence: d",
        ],
        "release": [
            "coverage_incomplete: a",
            "prohibited_substitution: c",
        ],
    }


# ---------------------------------------------------------------------
# derive_run_statuses
# ---------------------------------------------------------------------


def _clean_audit(**overrides):
    audit = {
        "evaluated": True,
        "passed": True,
        "failures": [],
        "warnings": [],
        "checks": {"classification": {"unclassified": 0}},
    }
    audit.update(overrides)
    return audit


def test_derive_clean_audit_quality_pass():
    result = release_status.derive_run_statuses(
        _clean_audit(), {"verdict": "PASS"}
    )
    assert result["technical_status"] == "passed"
    assert result["release_status"] == "release_grade"
    findings = result["release_findings"]
    assert findings["audit_release_failures"] == []
    assert findings["audit_warnings"] == []
    assert findings["quality_verdict"] == {"verdict": "PASS"}
    assert findings["classification_audit"] == {"unclassified": 0}
    json.dumps(result)  # JSON-safe


def test_derive_release_only_failures():
    failures = ["coverage_incomplete: sentences 2, 5"]
    result = release_status.derive_run_statuses(
        _clean_audit(passed=False, failures=failures),
        {"verdict": "PASS"},
    )
    assert result["technical_status"] == "passed"
    assert result["release_status"] == "not_release_grade"
    assert result["release_findings"]["audit_release_failures"] == failures


def test_derive_release_failures_field_counts():
    result = release_status.derive_run_statuses(
        _clean_audit(release_failures=["unclassified_visuals: 2"]),
        None,
    )
    assert result["technical_status"] == "passed"
    assert result["release_status"] == "not_release_grade"
    assert result["release_findings"]["audit_release_failures"] == [
        "unclassified_visuals: 2"
    ]


def test_derive_technical_failure():
    result = release_status.derive_run_statuses(
        _clean_audit(
            passed=False, failures=["provenance_mismatch: x"]
        ),
        {"verdict": "PASS"},
    )
    assert result["technical_status"] == "failed"


def test_derive_unevaluated_is_technical_failed():
    result = release_status.derive_run_statuses(
        _clean_audit(
            evaluated=False,
            passed=False,
            failures=["audit_not_evaluated: no asset report"],
        ),
        None,
    )
    assert result["technical_status"] == "failed"
    result = release_status.derive_run_statuses({}, None)
    assert result["technical_status"] == "failed"


def test_derive_quality_fail_alone_holds_release():
    result = release_status.derive_run_statuses(
        _clean_audit(), {"verdict": "FAIL"}
    )
    assert result["technical_status"] == "passed"
    assert result["release_status"] == "not_release_grade"


def test_derive_quality_warning_alone_stays_release_grade():
    result = release_status.derive_run_statuses(
        _clean_audit(), {"verdict": "WARNING"}
    )
    assert result["technical_status"] == "passed"
    assert result["release_status"] == "release_grade"


def test_derive_malformed_inputs():
    for bad in (None, "junk", 42, ["not", "a", "dict"]):
        result = release_status.derive_run_statuses(bad, None)
        assert result["technical_status"] == "failed"
        assert result["release_status"] == "not_release_grade"
        findings = result["release_findings"]
        assert findings["audit_release_failures"] == []
        assert findings["audit_warnings"] == []
        assert findings["quality_verdict"] is None
        assert findings["classification_audit"] is None
        json.dumps(result)
    result = release_status.derive_run_statuses(
        None, {"verdict": "PASS"}
    )
    assert result["release_findings"]["quality_verdict"] == {
        "verdict": "PASS"
    }


# ---------------------------------------------------------------------
# finish_run persistence
# ---------------------------------------------------------------------


@pytest.fixture()
def db():
    session = SessionLocal()
    catalogue.ensure_billing_catalogue(session)
    session.commit()
    yield session
    session.close()


# Users created by this module's billing tests, for the module
# teardown cleanup below.
_CREATED_USER_IDS = []


def _make_user(db, plan_name=None):
    user = models.User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4()}@example.invalid",
        password_hash="x",
    )
    _CREATED_USER_IDS.append(user.id)
    db.add(user)
    db.flush()
    if plan_name is not None:
        plan = db.query(models.Plan).filter_by(name=plan_name).one()
        now = datetime.now(timezone.utc)
        start, end = periods.month_bounds(now)
        db.add(
            models.UserSubscription(
                id=str(uuid.uuid4()),
                user_id=user.id,
                plan_id=plan.id,
                status="active",
                current_period_start=start,
                current_period_end=end,
            )
        )
    db.flush()
    return user


def _make_project(db, owner_id, script=SCRIPT_15MIN):
    project = models.Project(
        id=str(uuid.uuid4()), script=script, owner_id=owner_id
    )
    db.add(project)
    db.flush()
    return project


def _wallet(db, user_id):
    account = credits.get_or_create_account(db, user_id)
    db.commit()
    return credits.balances(account)


def _reservation_for(db, run_id):
    return (
        db.query(models.GenerationReservation)
        .filter_by(run_id=run_id)
        .one_or_none()
    )


@pytest.fixture(scope="module", autouse=True)
def _cleanup_billing_state():
    """Remove this module's billing rows at module teardown.

    The suite shares one database, and some neighbours assert on
    GLOBAL ledger sums (test_phase9's distinct-reservation test
    totals every "reserve" entry): this module's real reservations
    would otherwise leak into those totals. Fail-soft — cleanup
    must never fail a test."""
    yield
    if not _CREATED_USER_IDS:
        return
    session = SessionLocal()
    try:
        ids = list(_CREATED_USER_IDS)
        session.query(models.CreditLedgerEntry).filter(
            models.CreditLedgerEntry.user_id.in_(ids)
        ).delete(synchronize_session=False)
        session.query(models.GenerationReservation).filter(
            models.GenerationReservation.user_id.in_(ids)
        ).delete(synchronize_session=False)
        session.query(models.BillingPeriod).filter(
            models.BillingPeriod.user_id.in_(ids)
        ).delete(synchronize_session=False)
        session.query(models.CreditAccount).filter(
            models.CreditAccount.user_id.in_(ids)
        ).delete(synchronize_session=False)
        session.query(models.UserSubscription).filter(
            models.UserSubscription.user_id.in_(ids)
        ).delete(synchronize_session=False)
        session.query(models.ProductionRun).filter(
            models.ProductionRun.owner_id.in_(ids)
        ).delete(synchronize_session=False)
        session.query(models.Project).filter(
            models.Project.owner_id.in_(ids)
        ).delete(synchronize_session=False)
        session.query(models.User).filter(
            models.User.id.in_(ids)
        ).delete(synchronize_session=False)
        session.commit()
    except Exception as error:  # noqa: BLE001 - fail-soft teardown
        session.rollback()
        print(f"WARNING: phase4 test cleanup failed: {error}")
    finally:
        session.close()


def test_finish_run_persists_status_fields(db):
    project = _make_project(db, None)
    run_id = str(uuid.uuid4())
    db.add(
        models.ProductionRun(
            id=run_id, project_id=project.id, status="running"
        )
    )
    db.commit()

    findings = {
        "audit_release_failures": ["coverage_incomplete: s2"],
        "audit_warnings": [],
        "quality_verdict": {"verdict": "FAIL"},
        "classification_audit": None,
    }
    assert run_ledger.finish_run(
        run_id,
        "completed",
        technical_status="passed",
        release_status="not_release_grade",
        release_findings=findings,
    ) is True

    run = run_ledger.get_run(run_id)
    assert run["technical_status"] == "passed"
    assert run["release_status"] == "not_release_grade"
    assert run["release_findings"] == findings


def test_finish_run_without_status_fields_leaves_null(db):
    project = _make_project(db, None)
    run_id = str(uuid.uuid4())
    db.add(
        models.ProductionRun(
            id=run_id, project_id=project.id, status="running"
        )
    )
    db.commit()
    assert run_ledger.finish_run(run_id, "failed", error="boom")
    run = run_ledger.get_run(run_id)
    assert run["technical_status"] is None
    assert run["release_status"] is None
    assert run["release_findings"] is None


# ---------------------------------------------------------------------
# Billing switch — the money tests
# ---------------------------------------------------------------------


def _submit(db, plan_name="creator"):
    user = _make_user(db, plan_name)
    project = _make_project(db, user.id)
    db.commit()
    submission = run_ledger.submit_production(
        project.id, project.script, options={}
    )
    assert submission and not submission["deduplicated"]
    reservation = _reservation_for(db, submission["run_id"])
    assert reservation is not None
    assert reservation.credits_reserved == 90  # 15 min x 6 (Creator)
    return user, submission["run_id"]


def test_policy_default_is_restore(monkeypatch):
    monkeypatch.delenv(
        "CINEMORA_RELEASE_SETTLEMENT_POLICY", raising=False
    )
    assert (
        generation_billing.release_settlement_policy()
        == generation_billing.RELEASE_SETTLEMENT_RESTORE
    )
    monkeypatch.setenv(
        "CINEMORA_RELEASE_SETTLEMENT_POLICY", "nonsense"
    )
    assert (
        generation_billing.release_settlement_policy()
        == generation_billing.RELEASE_SETTLEMENT_RESTORE
    )
    monkeypatch.setenv("CINEMORA_RELEASE_SETTLEMENT_POLICY", "settle")
    assert (
        generation_billing.release_settlement_policy()
        == generation_billing.RELEASE_SETTLEMENT_SETTLE
    )


def test_not_release_grade_default_policy_restores_everything(
    db, monkeypatch
):
    monkeypatch.delenv(
        "CINEMORA_RELEASE_SETTLEMENT_POLICY", raising=False
    )
    user, run_id = _submit(db)
    assert run_ledger.finish_run(
        run_id,
        "completed",
        execution={"duration_seconds": 900},
        technical_status="passed",
        release_status="not_release_grade",
        release_findings={"audit_release_failures": []},
    )
    db.expire_all()
    reservation = _reservation_for(db, run_id)
    # EXACTLY today's outcome for a quality-failed run.
    assert reservation.status == "released"
    assert reservation.charged_credits == 0
    wallet = _wallet(db, user.id)
    assert wallet["subscription"]["available"] == 990
    assert wallet["subscription"]["reserved"] == 0
    assert wallet["lifetime_spent"] == 0
    period = periods.current_period(db, user.id)
    assert period.generations_used == 0  # entitlement restored


def test_not_release_grade_settle_policy_charges_tier_rate(
    db, monkeypatch
):
    monkeypatch.setenv(
        "CINEMORA_RELEASE_SETTLEMENT_POLICY", "settle"
    )
    user, run_id = _submit(db)
    assert run_ledger.finish_run(
        run_id,
        "completed",
        execution={"duration_seconds": 900},
        technical_status="passed",
        release_status="not_release_grade",
        release_findings={"audit_release_failures": []},
    )
    db.expire_all()
    reservation = _reservation_for(db, run_id)
    assert reservation.status == "settled"
    assert reservation.charged_credits == 90  # 15 min x 6 (Creator)
    wallet = _wallet(db, user.id)
    assert wallet["subscription"]["available"] == 990 - 90
    assert wallet["lifetime_spent"] == 90
    period = periods.current_period(db, user.id)
    assert period.generations_used == 1  # delivered video counts


def test_release_grade_default_policy_settles_unchanged(db, monkeypatch):
    monkeypatch.delenv(
        "CINEMORA_RELEASE_SETTLEMENT_POLICY", raising=False
    )
    user, run_id = _submit(db)
    assert run_ledger.finish_run(
        run_id,
        "completed",
        execution={"duration_seconds": 900},
        technical_status="passed",
        release_status="release_grade",
        release_findings={"audit_release_failures": []},
    )
    db.expire_all()
    reservation = _reservation_for(db, run_id)
    assert reservation.status == "settled"
    assert reservation.charged_credits == 90
    assert _wallet(db, user.id)["lifetime_spent"] == 90


def test_double_finish_moves_money_exactly_once(db, monkeypatch):
    monkeypatch.delenv(
        "CINEMORA_RELEASE_SETTLEMENT_POLICY", raising=False
    )
    user, run_id = _submit(db)
    assert run_ledger.finish_run(
        run_id,
        "completed",
        execution={"duration_seconds": 900},
        technical_status="passed",
        release_status="release_grade",
    ) is True
    # Second finish loses the first-finisher-wins race...
    assert run_ledger.finish_run(
        run_id,
        "completed",
        execution={"duration_seconds": 900},
        technical_status="passed",
        release_status="release_grade",
    ) is False
    # ...and a direct repeat settle is an idempotent no-op.
    generation_billing.settle_for_run(
        run_id,
        "completed",
        execution={"duration_seconds": 900},
        release_status="release_grade",
    )
    db.expire_all()
    reservation = _reservation_for(db, run_id)
    assert reservation.status == "settled"
    assert reservation.charged_credits == 90
    wallet = _wallet(db, user.id)
    assert wallet["lifetime_spent"] == 90
    assert wallet["subscription"]["available"] == 990 - 90
