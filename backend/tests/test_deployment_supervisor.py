"""Phase 2a (B-03/B-04) — deployment supervisor tests.

The supervisor is what turns CINEMORA_WORKER_COUNT into actual
worker processes in the deployment artefacts (Docker image CMD,
docker-compose worker service, repo-root app.py panel launcher).
These tests stub the spawn function entirely: NO real worker, web
server, Redis or database is ever started here.
"""

from pathlib import Path

import pytest

import supervise


class FakeProcess:
    """Minimal Popen stand-in: alive until killed or told to die."""

    def __init__(self, cmd, env=None, cwd=None):
        self.cmd = list(cmd)
        self.env = env
        self.cwd = cwd
        self.exit_code = None
        self.terminated = False
        self.killed = False
        self.signals = []

    def poll(self):
        return self.exit_code

    def die(self, code=1):
        self.exit_code = code

    def terminate(self):
        self.terminated = True
        if self.exit_code is None:
            self.exit_code = -15

    def kill(self):
        self.killed = True
        if self.exit_code is None:
            self.exit_code = -9

    def wait(self, timeout=None):
        return self.exit_code

    def send_signal(self, signum):
        self.signals.append(signum)


class SpawnLog:
    def __init__(self):
        self.processes = []

    def __call__(self, cmd, env=None, cwd=None):
        proc = FakeProcess(cmd, env=env, cwd=cwd)
        self.processes.append(proc)
        return proc

    @property
    def workers(self):
        return [p for p in self.processes if p.cmd[-1] == "worker.py"]

    @property
    def webs(self):
        return [p for p in self.processes if "uvicorn" in p.cmd]


BASE_ENV = {
    "DATABASE_URL": "sqlite:////tmp/never-touched.db",
    "REDIS_URL": "redis://127.0.0.1:6399/0",
    "CINEMORA_RENDER_CONCURRENCY": "3",
}


# -- worker-count parsing (B-04) ------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [(None, 2), ("", 2), ("2", 2), ("3", 3), ("0", 0), ("-1", 0),
     ("banana", 2), ("9999", supervise.MAX_WORKER_COUNT)],
)
def test_parse_worker_count(value, expected):
    assert supervise.parse_worker_count(value) == expected


def test_worker_count_from_env():
    assert supervise.worker_count_from_env({}) == 2
    assert supervise.worker_count_from_env({"CINEMORA_WORKER_COUNT": "5"}) == 5


# -- spawning --------------------------------------------------------

def test_supervisor_spawns_web_plus_n_workers():
    spawn = SpawnLog()
    env = dict(BASE_ENV, CINEMORA_WORKER_COUNT="3")
    sup = supervise.Supervisor(spawn=spawn, env=env)
    sup.start()
    assert len(spawn.webs) == 1
    assert len(spawn.workers) == 3
    assert sup.pool.alive_count() == 3


def test_worker_env_carries_count_and_render_concurrency():
    spawn = SpawnLog()
    env = dict(BASE_ENV, CINEMORA_WORKER_COUNT="4")
    sup = supervise.Supervisor(spawn=spawn, env=env)
    sup.start()
    for proc in spawn.workers:
        # The count written back into the child env is what the
        # dispatcher cap and the admin display read — they can no
        # longer diverge from the real process count (B-04).
        assert proc.env["CINEMORA_WORKER_COUNT"] == "4"
        assert proc.env["CINEMORA_RENDER_CONCURRENCY"] == "3"
        assert proc.env["DATABASE_URL"] == BASE_ENV["DATABASE_URL"]
        assert proc.env["REDIS_URL"] == BASE_ENV["REDIS_URL"]


def test_render_concurrency_defaults_when_unset():
    spawn = SpawnLog()
    env = {"CINEMORA_WORKER_COUNT": "1"}
    supervise.Supervisor(spawn=spawn, env=env).start()
    assert spawn.workers[0].env["CINEMORA_RENDER_CONCURRENCY"] == "1"


def test_web_only_mode_spawns_no_workers():
    spawn = SpawnLog()
    env = dict(BASE_ENV, CINEMORA_WORKER_COUNT="3")
    sup = supervise.Supervisor(spawn=spawn, env=env, mode="web-only")
    sup.start()
    assert len(spawn.webs) == 1
    assert spawn.workers == []


def test_web_only_env_flag():
    spawn = SpawnLog()
    env = dict(BASE_ENV, CINEMORA_WORKER_COUNT="3", CINEMORA_WEB_ONLY="1")
    sup = supervise.Supervisor(spawn=spawn, env=env)
    assert sup.mode == "web-only"
    sup.start()
    assert spawn.workers == []


def test_workers_only_mode_spawns_no_web():
    spawn = SpawnLog()
    env = dict(BASE_ENV, CINEMORA_WORKER_COUNT="2")
    sup = supervise.Supervisor(spawn=spawn, env=env, mode="workers-only")
    sup.start()
    assert spawn.webs == []
    assert len(spawn.workers) == 2


def test_zero_worker_count_spawns_web_only_by_count():
    spawn = SpawnLog()
    env = dict(BASE_ENV, CINEMORA_WORKER_COUNT="0")
    supervise.Supervisor(spawn=spawn, env=env).start()
    assert len(spawn.webs) == 1
    assert spawn.workers == []


def test_web_command_uses_port_env():
    cmd = supervise.web_command({"PORT": "9123"})
    assert cmd[-1] == "9123"
    assert "main:app" in cmd


# -- supervision behaviour -------------------------------------------

def test_dead_worker_is_restarted():
    spawn = SpawnLog()
    env = dict(BASE_ENV, CINEMORA_WORKER_COUNT="2")
    sup = supervise.Supervisor(spawn=spawn, env=env)
    sup.start()
    spawn.workers[0].die(code=1)
    assert sup.step() is None
    assert len(spawn.workers) == 3  # one replacement spawned
    assert sup.pool.alive_count() == 2
    assert sup.pool.restart_counts[0] == 1


def test_web_death_propagates_and_stops_workers():
    spawn = SpawnLog()
    env = dict(BASE_ENV, CINEMORA_WORKER_COUNT="2")
    sup = supervise.Supervisor(spawn=spawn, env=env)
    sup.start()
    spawn.webs[0].die(code=3)
    assert sup.step() == 3
    assert all(p.terminated for p in spawn.workers)


def test_run_returns_web_exit_code():
    spawn = SpawnLog()
    env = dict(BASE_ENV, CINEMORA_WORKER_COUNT="1")
    sup = supervise.Supervisor(spawn=spawn, env=env, sleep=lambda _s: None)
    original_start = sup.start

    def start_then_kill_web():
        original_start()
        spawn.webs[0].die(code=5)

    sup.start = start_then_kill_web
    assert sup.run() == 5


def test_stop_request_shuts_everything_down():
    spawn = SpawnLog()
    env = dict(BASE_ENV, CINEMORA_WORKER_COUNT="2")
    sup = supervise.Supervisor(spawn=spawn, env=env)
    sup.start()
    sup.request_stop()
    assert sup.step() == 0
    assert spawn.webs[0].terminated
    assert all(p.terminated for p in spawn.workers)


def test_signal_is_forwarded_to_children():
    spawn = SpawnLog()
    env = dict(BASE_ENV, CINEMORA_WORKER_COUNT="1")
    sup = supervise.Supervisor(spawn=spawn, env=env)
    sup.start()
    sup._on_signal(15, None)
    assert 15 in spawn.webs[0].signals
    assert 15 in spawn.workers[0].signals
    assert sup.stop_requested


# -- artefact wiring (static, no processes started) ------------------

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_cmd_is_the_supervisor():
    dockerfile = (REPO_ROOT / "backend" / "Dockerfile").read_text()
    assert "supervise.py" in dockerfile
    assert "CINEMORA_WORKER_COUNT" in dockerfile


def test_app_py_launcher_supervises_workers():
    app_py = (REPO_ROOT / "app.py").read_text()
    assert "WorkerPool" in app_py
    assert "worker_count_from_env" in app_py


def test_compose_has_worker_service_honouring_count():
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    services = compose["services"]
    assert "worker" in services
    assert "backend" in services
    worker_env = services["worker"]["environment"]
    assert "CINEMORA_WORKER_COUNT" in str(worker_env)
    assert "CINEMORA_RENDER_CONCURRENCY" in str(worker_env)
    assert "workers-only" in str(services["worker"]["command"])
    depends = services["worker"]["depends_on"]
    assert "postgres" in depends and "redis" in depends
