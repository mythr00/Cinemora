"use client";

/** M2 — cross-project production console (list). */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { formatDateTime, formatSeconds } from "@/components/admin-format";
import { ErrorState, LoadingState } from "@/components/states";
import { listAdminRuns, type AdminRunListEntry } from "@/lib/api";

const STATUS_FILTERS = [
  "all",
  "queued",
  "running",
  "awaiting_approval",
  "completed",
  "failed",
  "cancelled",
];

export default function AdminRunsPage() {
  const [runs, setRuns] = useState<AdminRunListEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState("all");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setBusy(true);
      try {
        const data = await listAdminRuns(
          { status: status === "all" ? undefined : status, limit: 100 },
          signal,
        );
        setRuns(data.runs);
        setTotal(data.total);
      } catch (cause) {
        if (!signal?.aborted) setError(cause);
      } finally {
        if (!signal?.aborted) setBusy(false);
      }
    },
    [status],
  );

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => { void load(controller.signal); }, 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [load]);

  return (
    <section>
      <div className="admin-heading">
        <p className="eyebrow">PRODUCTION MONITORING</p>
        <h1>Every run, across every project.</h1>
        <p className="admin-intro">
          The run ledger is the durable record — this list reads it
          directly, with live presence for active runs.
        </p>
      </div>
      <div className="admin-toolbar">
        <span>
          {busy ? "Loading runs…" : `${runs.length} of ${total} runs`}
        </span>
        <label className="admin-filter">
          <span>Status</span>
          <select
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            aria-label="Filter runs by status"
          >
            {STATUS_FILTERS.map((option) => (
              <option key={option} value={option}>
                {option === "all" ? "All statuses" : option}
              </option>
            ))}
          </select>
        </label>
      </div>
      {!!error && (
        <ErrorState error={error} onRetry={() => void load()} />
      )}
      {busy && !error && <LoadingState label="Loading runs…" />}
      {!busy && !error && runs.length === 0 && (
        <p className="admin-empty">No runs match this filter.</p>
      )}
      {!busy && !error && runs.length > 0 && (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Run</th>
              <th>Project</th>
              <th>Status</th>
              <th>Stage</th>
              <th>Presence</th>
              <th>Duration</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.run_id}>
                <td>
                  <Link href={`/admin/runs/${run.run_id}`}>
                    {run.run_id.slice(0, 8)}…
                  </Link>
                </td>
                <td>{run.project_title ?? run.project_id.slice(0, 8) + "…"}</td>
                <td>{run.status}</td>
                <td>{run.stage ?? "—"}</td>
                <td>
                  {run.presence
                    ? run.presence.live
                      ? `live · ${run.presence.worker_id ?? "worker"}`
                      : "stale heartbeat"
                    : "—"}
                </td>
                <td>{formatSeconds(run.wall_seconds)}</td>
                <td>{formatDateTime(run.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
