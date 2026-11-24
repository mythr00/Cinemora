"use client";

/** M3 — worker & queue monitoring (ledger first, RQ alongside). */

import { useCallback, useEffect, useState } from "react";
import { formatDateTime } from "@/components/admin-format";
import { ErrorState, LoadingState } from "@/components/states";
import { getAdminWorkers } from "@/lib/api";

type WorkerRow = {
  name: string;
  state: string;
  queues: string[];
  current_job_id: string | null;
  successful_job_count: number;
  failed_job_count: number;
  last_heartbeat: string | null;
};

export default function AdminWorkersPage() {
  const [data, setData] = useState<Awaited<
    ReturnType<typeof getAdminWorkers>
  > | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setData(await getAdminWorkers(signal));
    } catch (cause) {
      if (!signal?.aborted) setError(cause);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => { void load(controller.signal); }, 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [load]);

  if (error) return <ErrorState error={error} onRetry={() => void load()} />;
  if (!data) return <LoadingState label="Loading workers…" />;

  const workers = data.workers as {
    status: string;
    workers?: WorkerRow[];
    error?: string;
  };
  const queues = data.queues as {
    ledger_queued: number;
    ledger_waiting_undispatched: number;
    ledger_running: number;
    stuck_suspected_run_ids: string[];
    rq_queue_lengths: Record<string, number | null>;
    rq_failed_registry_sizes: Record<string, number | null>;
    job_timeout_seconds: number;
    retry_policy: string;
  };
  const providers = data.providers as {
    status: string;
    backed_off_scopes?: Array<{ scope: string; backed_off_until: number }>;
  };

  return (
    <section>
      <div className="admin-heading">
        <p className="eyebrow">WORKERS & QUEUES</p>
        <h1>Who is producing, and what is waiting.</h1>
        <p className="admin-intro">
          The run ledger is the durable queue truth — it survives a
          Redis restart and matches what the dispatcher will claim.
          RQ figures ride alongside; if the two ever diverge, both
          are shown.
        </p>
      </div>
      <h2 className="admin-section-title">Workers</h2>
      {workers.status === "error" && (
        <p className="admin-empty">
          Worker registry unavailable: {workers.error ?? "Redis error"}.
        </p>
      )}
      {workers.status === "absent" && (
        <p className="admin-empty">
          No worker is currently registered. Productions will wait in
          the ledger until a worker starts.
        </p>
      )}
      {workers.status === "ok" && (workers.workers ?? []).length > 0 && (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Worker</th>
              <th>State</th>
              <th>Queues</th>
              <th>Current job</th>
              <th>Jobs done / failed</th>
              <th>Last heartbeat</th>
            </tr>
          </thead>
          <tbody>
            {(workers.workers ?? []).map((worker) => (
              <tr key={worker.name}>
                <td>{worker.name}</td>
                <td>{worker.state}</td>
                <td>{worker.queues.join(", ")}</td>
                <td>{worker.current_job_id ?? "—"}</td>
                <td>
                  {worker.successful_job_count} / {worker.failed_job_count}
                </td>
                <td>{formatDateTime(worker.last_heartbeat)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h2 className="admin-section-title">Queues</h2>
      <dl className="admin-detail-grid">
        <div><dt>Queued (ledger)</dt><dd>{queues.ledger_queued}</dd></div>
        <div>
          <dt>Waiting for dispatch</dt>
          <dd>{queues.ledger_waiting_undispatched}</dd>
        </div>
        <div><dt>Running (ledger)</dt><dd>{queues.ledger_running}</dd></div>
        <div>
          <dt>Job timeout</dt>
          <dd>{queues.job_timeout_seconds}s</dd>
        </div>
        {Object.entries(queues.rq_queue_lengths).map(([name, length]) => (
          <div key={name}>
            <dt>RQ length · {name}</dt>
            <dd>{length === null ? "unavailable" : length}</dd>
          </div>
        ))}
        {Object.entries(queues.rq_failed_registry_sizes).map(
          ([name, size]) => (
            <div key={name}>
              <dt>RQ failed registry · {name}</dt>
              <dd>{size === null ? "unavailable" : size}</dd>
            </div>
          ),
        )}
      </dl>
      <p className="admin-note">{queues.retry_policy}</p>
      {queues.stuck_suspected_run_ids.length > 0 && (
        <>
          <h2 className="admin-section-title">Stuck-suspected runs</h2>
          <p className="admin-note">
            Flagged by the stage watchdog — flagged, never killed: a
            slow render and a hung render look identical from outside.
          </p>
          <ul className="admin-list">
            {queues.stuck_suspected_run_ids.map((runId) => (
              <li key={runId}>{runId}</li>
            ))}
          </ul>
        </>
      )}
      <h2 className="admin-section-title">Provider backoff</h2>
      {providers.status === "ok" &&
      (providers.backed_off_scopes ?? []).length === 0 ? (
        <p className="admin-empty">No provider scope is backed off.</p>
      ) : providers.status === "ok" ? (
        <ul className="admin-list">
          {(providers.backed_off_scopes ?? []).map((scope) => (
            <li key={scope.scope}>
              {scope.scope} — backed off until{" "}
              {formatDateTime(
                new Date(scope.backed_off_until * 1000).toISOString(),
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="admin-empty">Provider state unavailable.</p>
      )}
    </section>
  );
}
