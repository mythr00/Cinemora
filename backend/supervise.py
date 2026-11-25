"""Deployment supervisor: run the web API and the RQ workers as one
supervised unit (B-03/B-04).

Why this exists
---------------
Before this module, no deployment artefact started a worker at all:
the Dockerfile CMD was uvicorn-only, docker-compose had no worker
service, and the panel launcher (repo-root ``app.py``) ran migrations
+ uvicorn only. A deployment from any of them accepted productions
that could never execute. The only launcher that spawned workers was
the out-of-repo preview script.

This supervisor is the single place that turns
``CINEMORA_WORKER_COUNT`` into actual worker processes:

- default mode: start uvicorn plus ``CINEMORA_WORKER_COUNT`` copies
  of ``worker.py`` (default 2), restart any worker that dies, and
  shut everything down (returning the web exit code) if the web
  process dies.
- ``--web-only``: uvicorn only — for hosts that scale web and worker
  processes separately (e.g. the compose ``worker`` service, or a
  panel with separate apps). Also enabled by ``CINEMORA_WEB_ONLY=1``.
- ``--workers-only``: workers only, supervised and restarted — used
  by the compose ``worker`` service so the count is honoured there
  too, not just in the all-in-one container.

``CINEMORA_RENDER_CONCURRENCY`` is passed through to the workers
unchanged (it is read by ``services/render_slots.py`` inside each
worker; the supervisor only guarantees it is present in the child
environment, defaulting to ``1`` like the rest of the stack).

If ``CINEMORA_RUN_MIGRATIONS=1`` is set (the Docker image sets it),
Alembic migrations run once before anything is spawned in
web/all modes. Workers-only mode never migrates: ``worker.py``'s own
schema gate exits a too-early worker and this supervisor restarts it,
so a worker service that starts before migrations finish converges
once the web service has migrated.

Signals: SIGTERM/SIGINT received by the supervisor are forwarded to
every child, then the supervisor exits.
"""

import argparse
import os
import signal
import subprocess
import sys
import time

DEFAULT_WORKER_COUNT = 2
MAX_WORKER_COUNT = 32

WEB_ONLY_ENV = "CINEMORA_WEB_ONLY"


def parse_worker_count(value):
    """Parse a CINEMORA_WORKER_COUNT value.

    Unset/invalid values fall back to the default of 2 (matching the
    dispatcher's ``platform_cap`` default in services/dispatcher.py);
    negative values clamp to 0 (workers disabled deliberately) and
    absurd values clamp to MAX_WORKER_COUNT.
    """
    if value is None or str(value).strip() == "":
        return DEFAULT_WORKER_COUNT
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_WORKER_COUNT
    return max(0, min(count, MAX_WORKER_COUNT))


def worker_count_from_env(env=None):
    env = os.environ if env is None else env
    return parse_worker_count(env.get("CINEMORA_WORKER_COUNT"))


def web_command(env=None):
    env = os.environ if env is None else env
    port = env.get("PORT") or "8000"
    return [
        sys.executable, "-m", "uvicorn", "main:app",
        "--host", "0.0.0.0", "--port", str(port),
    ]


def worker_command():
    return [sys.executable, "worker.py"]


def build_child_env(env, worker_count):
    """Environment for spawned children: the deployment environment,
    with the worker count written back (so dispatcher cap, admin
    display and actual process count cannot diverge — B-04) and the
    render concurrency guaranteed present for workers."""
    child_env = dict(env)
    child_env["CINEMORA_WORKER_COUNT"] = str(worker_count)
    child_env.setdefault("CINEMORA_RENDER_CONCURRENCY", "1")
    return child_env


class WorkerPool:
    """Owns the worker child processes: spawn N, restart the dead,
    terminate all on shutdown. Used by the supervisor below and by
    the repo-root panel launcher (app.py)."""

    def __init__(self, count, spawn=None, env=None, cwd=None):
        self.count = count
        self._spawn = spawn or subprocess.Popen
        self._env = env
        self._cwd = cwd
        self.workers = []
        self.restart_counts = []

    def _spawn_worker(self):
        return self._spawn(worker_command(), env=self._env, cwd=self._cwd)

    def start(self):
        self.workers = [self._spawn_worker() for _ in range(self.count)]
        self.restart_counts = [0] * self.count
        return self.workers

    def restart_dead(self):
        """Restart any worker that has exited. Returns how many were
        restarted in this pass."""
        restarted = 0
        for index, proc in enumerate(self.workers):
            if proc.poll() is not None:
                self.workers[index] = self._spawn_worker()
                self.restart_counts[index] += 1
                restarted += 1
        return restarted

    def alive_count(self):
        return sum(1 for proc in self.workers if proc.poll() is None)

    def terminate_all(self):
        for proc in self.workers:
            if proc.poll() is None:
                proc.terminate()
        for proc in self.workers:
            try:
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


class Supervisor:
    """Web + workers supervised together (see module docstring)."""

    def __init__(
        self,
        worker_count=None,
        mode="all",
        spawn=None,
        env=None,
        cwd=None,
        poll_interval=0.5,
        sleep=None,
    ):
        self.env = dict(os.environ if env is None else env)
        self.worker_count = (
            worker_count_from_env(self.env)
            if worker_count is None
            else worker_count
        )
        if mode == "all" and self.env.get(WEB_ONLY_ENV) == "1":
            mode = "web-only"
        self.mode = mode
        self._spawn = spawn or subprocess.Popen
        self._cwd = cwd
        self._poll_interval = poll_interval
        self._sleep = sleep or time.sleep
        self.child_env = build_child_env(self.env, self.worker_count)
        self.web = None
        self.pool = WorkerPool(
            self.worker_count if mode in ("all", "workers-only") else 0,
            spawn=self._spawn,
            env=self.child_env,
            cwd=cwd,
        )
        self.stop_requested = False

    # -- lifecycle -------------------------------------------------
    def start(self):
        if self.mode in ("all", "web-only"):
            self.web = self._spawn(
                web_command(self.env), env=self.child_env, cwd=self._cwd
            )
        if self.mode in ("all", "workers-only"):
            self.pool.start()

    def request_stop(self, signum=None, frame=None):
        self.stop_requested = True

    def _forward_signal(self, signum):
        for proc in self._all_children():
            if proc.poll() is None:
                try:
                    proc.send_signal(signum)
                except Exception:
                    pass

    def _all_children(self):
        children = list(self.pool.workers)
        if self.web is not None:
            children.append(self.web)
        return children

    def shutdown(self):
        if self.web is not None and self.web.poll() is None:
            self.web.terminate()
        self.pool.terminate_all()
        if self.web is not None:
            try:
                self.web.wait(timeout=10)
            except Exception:
                try:
                    self.web.kill()
                except Exception:
                    pass

    def step(self):
        """One supervision pass. Returns an exit code when the unit
        should stop, or None to keep running."""
        if self.stop_requested:
            self.shutdown()
            return 0
        if self.web is not None:
            web_code = self.web.poll()
            if web_code is not None:
                # Web death propagates: a deployment unit without its
                # API must exit so the host restarts the whole unit.
                self.pool.terminate_all()
                return web_code
        self.pool.restart_dead()
        return None

    def run(self):
        self.start()
        installed = []
        if threading_main():
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    previous = signal.signal(sig, self._on_signal)
                    installed.append((sig, previous))
                except (ValueError, OSError):
                    pass
        try:
            while True:
                code = self.step()
                if code is not None:
                    return code
                self._sleep(self._poll_interval)
        except KeyboardInterrupt:
            self.shutdown()
            return 0
        finally:
            for sig, previous in installed:
                try:
                    signal.signal(sig, previous)
                except (ValueError, OSError):
                    pass

    def _on_signal(self, signum, frame):
        self._forward_signal(signum)
        self.request_stop(signum, frame)


def threading_main():
    import threading

    return threading.current_thread() is threading.main_thread()


def run_migrations():
    """Alembic upgrade to head, from the backend directory (same call
    the panel launcher makes). Only invoked when
    CINEMORA_RUN_MIGRATIONS=1."""
    from alembic import command
    from alembic.config import Config

    backend = os.path.dirname(os.path.abspath(__file__))
    command.upgrade(Config(os.path.join(backend, "alembic.ini")), "head")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--web-only", action="store_true",
                        help="run only the web API (no workers)")
    parser.add_argument("--workers-only", action="store_true",
                        help="run only the supervised workers (no web)")
    args = parser.parse_args(argv)
    if args.web_only and args.workers_only:
        parser.error("--web-only and --workers-only are mutually exclusive")
    mode = "all"
    if args.web_only:
        mode = "web-only"
    elif args.workers_only:
        mode = "workers-only"

    env = dict(os.environ)
    count = worker_count_from_env(env)
    if mode in ("all", "web-only") and env.get("CINEMORA_RUN_MIGRATIONS") == "1":
        run_migrations()
    print(
        f"supervise: mode={mode} workers={count if mode != 'web-only' else 0} "
        f"render_concurrency={env.get('CINEMORA_RENDER_CONCURRENCY', '1')}",
        flush=True,
    )
    supervisor = Supervisor(worker_count=count, mode=mode, env=env)
    return supervisor.run()


if __name__ == "__main__":
    raise SystemExit(main())
