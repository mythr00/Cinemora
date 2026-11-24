"""Phase 7 read services: admin_stats / admin_storage / admin_health.

Every figure is checked against the rows and files the test itself
created — the M1/M4/M6 acceptance rule (figures reproducible from
their named sources) applied at the service layer.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest

from database import SessionLocal
from models import Base, ProductionRun, Project, User
from services import admin_health, admin_stats, admin_storage


def _uid():
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _seed(monkeypatch, tmp_path):
    Base.metadata.create_all(bind=SessionLocal().get_bind())
    media_root = tmp_path / "media"
    media_root.mkdir()
    monkeypatch.setenv("CINEMORA_MEDIA_ROOT", str(media_root))
    admin_storage.invalidate_cache()
    admin_health.set_redis_client(
        fakeredis.FakeRedis(decode_responses=True)
    )
    db = SessionLocal()
    try:
        for model in (ProductionRun, Project, User):
            db.query(model).delete()
        db.commit()
        owner = User(
            id=_uid(), email="owner@example.com", password_hash="x",
            role="user", status="active",
        )
        db.add(owner)
        project = Project(
            id=_uid(), script="A script.", title="Seeded Project",
            category="Documentary", status="completed",
            owner_id=owner.id,
        )
        db.add(project)
        now = datetime.now(timezone.utc)
        runs = [
            ProductionRun(
                id=_uid(), project_id=project.id, owner_id=owner.id,
                status="completed", stage="done",
                created_at=now - timedelta(days=1),
                started_at=now - timedelta(days=1),
                finished_at=now - timedelta(days=1) + timedelta(seconds=120),
                execution={
                    "wall_time_seconds": 120,
                    "serper_calls": {"total": 10, "web": 4, "images": 3,
                                     "video": 3},
                    "llm_calls": {"total": 5, "total_tokens": 900},
                    "archive_org_searches": 2,
                    "wikimedia_searches": 1,
                    "candidates_discovered": 40,
                    "downloads": {"attempted": 8, "succeeded": 7,
                                  "failed": 1, "bytes_downloaded": 2048},
                    "monetary_cost_usd": None,
                },
            ),
            ProductionRun(
                id=_uid(), project_id=project.id, owner_id=owner.id,
                status="failed", stage="asset_manager",
                error="boom", created_at=now - timedelta(hours=2),
                execution={
                    "wall_time_seconds": 60,
                    "serper_calls": {"total": 5, "web": 5, "images": 0,
                                     "video": 0},
                    "llm_calls": {"total": 2, "total_tokens": 100},
                    "downloads": {"attempted": 1, "succeeded": 0,
                                  "failed": 1, "bytes_downloaded": 0},
                },
            ),
            ProductionRun(
                id=_uid(), project_id=project.id, owner_id=owner.id,
                status="queued", created_at=now,
            ),
        ]
        for run in runs:
            db.add(run)
        db.commit()
        yield {"project": project, "runs": runs, "media_root": media_root}
    finally:
        db.close()
        admin_health.reset_redis_client()
        admin_storage.invalidate_cache()


def test_overview_counts_match_seeded_rows(_seed):
    db = SessionLocal()
    try:
        out = admin_stats.overview(db)
        assert out["projects_total"] == 1
        assert out["users_total"] == 1
        assert out["users_by_status"] == {"active": 1}
        assert out["runs_total"] == 3
        assert out["runs_by_status"] == {
            "completed": 1, "failed": 1, "queued": 1,
        }
        assert out["completion_rate_window"] == 0.5
        assert out["median_wall_seconds"] == 90.0
        assert out["serper_calls_window"] == 15
        assert out["llm_calls_window"] == 7
        assert out["active_runs"] == 1
        assert out["latest_failures"][0]["error"] == "boom"
    finally:
        db.close()


def test_provider_usage_totals_reconcile_and_cost_stays_none(_seed):
    db = SessionLocal()
    try:
        out = admin_stats.provider_usage(db)
        totals = out["totals"]
        assert totals["serper_calls"] == {
            "total": 15, "web": 9, "images": 3, "video": 3,
        }
        assert totals["llm_calls"] == {"total": 7, "total_tokens": 1000}
        assert totals["archive_org_searches"] == 2
        assert totals["downloads"]["bytes_downloaded"] == 2048
        assert out["runs_with_execution"] == 2
        # The honesty rule: no price table -> cost is None, never 0.
        assert out["monetary_cost_usd"] is None
    finally:
        db.close()


def test_list_runs_and_detail(_seed):
    db = SessionLocal()
    try:
        out = admin_stats.list_runs(db)
        assert out["total"] == 3
        queued = [r for r in out["runs"] if r["status"] == "queued"][0]
        assert queued["presence"] is not None  # active runs carry it
        completed = [r for r in out["runs"] if r["status"] == "completed"][0]
        assert completed["presence"] is None
        detail = admin_stats.run_detail(db, completed["run_id"])
        assert detail["project_title"] == "Seeded Project"
        assert detail["execution"]["serper_calls"]["total"] == 10
        assert admin_stats.run_detail(db, "no-such-run") is None
    finally:
        db.close()


def test_storage_walk_and_orphans(_seed):
    project = _seed["project"]
    media_root = _seed["media_root"]
    project_dir = media_root / project.id
    (project_dir / "runs" / "run-a").mkdir(parents=True)
    (project_dir / "film.mp4").write_bytes(b"x" * 100)
    (project_dir / "runs" / "run-a" / "report.json").write_bytes(b"y" * 50)
    orphan_dir = media_root / "ghost-project"
    orphan_dir.mkdir()
    (orphan_dir / "a.bin").write_bytes(b"z" * 10)
    admin_storage.invalidate_cache()

    db = SessionLocal()
    try:
        out = admin_storage.summary(db)
        assert out["total_bytes"] == 160
        mine = [p for p in out["projects"] if p["project_id"] == project.id][0]
        assert mine["bytes"] == 150
        assert mine["runs"] == [{"run_id": "run-a", "bytes": 50}]
        assert out["largest_artefacts"][0]["bytes"] == 100

        report = admin_storage.orphans(db)
        assert [o["project_id"] for o in
                report["media_directories_without_project"]] == [
            "ghost-project"
        ]
        assert report["projects_without_media_directory"] == []
    finally:
        db.close()


def test_composite_health_components(_seed):
    db = SessionLocal()
    try:
        out = admin_health.composite_health(db)
        components = out["components"]
        assert components["api"]["status"] == "ok"
        # The suite DB is create_all (unstamped legacy): check_schema
        # reports that state rather than raising.
        assert components["database"]["status"] == "ok"
        assert components["redis"]["status"] == "ok"
        assert components["workers"]["status"] == "absent"
        queues = components["queues"]
        assert queues["ledger_queued"] == 1
        assert queues["ledger_waiting_undispatched"] == 1
        assert queues["rq_queue_lengths"]["video_generation"] == 0
        assert components["providers"]["backed_off_scopes"] == []
        assert components["public_site"]["status"] == "not_configured"
    finally:
        db.close()


def test_health_redis_down_flips_only_redis_components():
    class DeadRedis:
        def ping(self):
            raise ConnectionError("down")

        def scan_iter(self, match=None):
            raise ConnectionError("down")

        def llen(self, key):
            raise ConnectionError("down")

    admin_health.set_redis_client(DeadRedis())
    db = SessionLocal()
    try:
        out = admin_health.composite_health(db)
        components = out["components"]
        assert components["redis"]["status"] == "error"
        assert components["workers"]["status"] == "error"
        assert components["providers"]["status"] == "error"
        # The ledger-derived queue counts survive a Redis outage.
        assert components["queues"]["ledger_queued"] == 1
        assert components["queues"]["rq_queue_lengths"][
            "video_generation"
        ] is None
        assert components["database"]["status"] == "ok"
    finally:
        db.close()
