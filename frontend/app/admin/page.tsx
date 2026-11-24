"use client";

/** M1 — platform overview: the production pulse, from real rows. */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  formatDateTime,
  formatPercent,
  formatSeconds,
} from "@/components/admin-format";
import { ErrorState, LoadingState } from "@/components/states";
import {
  getAdminHealth,
  getAdminOverview,
  getAdminStorage,
  type AdminHealth,
  type AdminOverview,
  type AdminStorage,
} from "@/lib/api";
import { formatBytes } from "@/components/admin-format";

export default function AdminOverviewPage() {
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [health, setHealth] = useState<AdminHealth | null>(null);
  const [storage, setStorage] = useState<AdminStorage | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const [ov, he, st] = await Promise.all([
        getAdminOverview(signal),
        getAdminHealth(signal),
        getAdminStorage(signal),
      ]);
      setOverview(ov);
      setHealth(he);
      setStorage(st);
    } catch (cause) {
      if (!signal?.aborted) setError(cause);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => { void load(controller.signal); }, 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [load]);

  if (error) {
    return (
      <ErrorState error={error} onRetry={() => void load()} />
    );
  }
  if (!overview || !health || !storage) {
    return <LoadingState label="Loading platform overview…" />;
  }

  const workers = health.components.workers as {
    status: string;
    workers?: Array<unknown>;
  };
  const queues = health.components.queues as {
    ledger_queued?: number;
    ledger_running?: number;
  };
  const workerCount = Array.isArray(workers.workers)
    ? workers.workers.length
    : 0;

  const cards: Array<{ label: string; value: string; hint?: string }> = [
    { label: "Projects", value: String(overview.projects_total) },
    { label: "Registered users", value: String(overview.users_total) },
    {
      label: "Active accounts",
      value: String(overview.users_by_status.active ?? 0),
    },
    {
      label: "Runs (all time)",
      value: String(overview.runs_total),
      hint: Object.entries(overview.runs_by_status)
        .map(([status, count]) => `${status}: ${count}`)
        .join(" · "),
    },
    {
      label: `Completion rate (${overview.window_days}d)`,
      value: formatPercent(overview.completion_rate_window),
    },
    {
      label: "Median run time",
      value: formatSeconds(overview.median_wall_seconds_window),
    },
    {
      label: `Serper calls (${overview.window_days}d)`,
      value: String(overview.serper_calls_window),
    },
    {
      label: `LLM calls (${overview.window_days}d)`,
      value: String(overview.llm_calls_window),
    },
    { label: "Active runs", value: String(overview.active_runs) },
    {
      label: "Workers",
      value:
        workers.status === "ok"
          ? `${workerCount} registered`
          : workers.status === "absent"
            ? "None registered"
            : "Unknown (Redis error)",
    },
    {
      label: "Queue (ledger)",
      value: `${queues.ledger_queued ?? 0} queued · ${queues.ledger_running ?? 0} running`,
    },
    {
      label: "Media storage",
      value: formatBytes(storage.total_bytes),
      hint: `${storage.total_files} files`,
    },
  ];

  return (
    <section>
      <div className="admin-heading">
        <p className="eyebrow">PLATFORM OVERVIEW</p>
        <h1>The production pulse.</h1>
        <p className="admin-intro">
          Every figure below is computed from the platform&apos;s own
          records — projects, runs, accounts and the media tree.
          Updated {formatDateTime(overview.generated_at)}.
        </p>
      </div>
      <div className="admin-card-grid">
        {cards.map((card) => (
          <article className="admin-card" key={card.label}>
            <p className="admin-card-label">{card.label}</p>
            <p className="admin-card-value">{card.value}</p>
            {card.hint && <p className="admin-card-hint">{card.hint}</p>}
          </article>
        ))}
      </div>
      <h2 className="admin-section-title">Latest failures</h2>
      {overview.latest_failures.length === 0 ? (
        <p className="admin-empty">No failed or cancelled runs recorded.</p>
      ) : (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Run</th>
              <th>Status</th>
              <th>Stage</th>
              <th>Error</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {overview.latest_failures.map((failure) => (
              <tr key={failure.run_id}>
                <td>
                  <Link href={`/admin/runs/${failure.run_id}`}>
                    {failure.run_id.slice(0, 8)}…
                  </Link>
                </td>
                <td>{failure.status}</td>
                <td>{failure.stage ?? "—"}</td>
                <td className="admin-cell-error">{failure.error ?? "—"}</td>
                <td>{formatDateTime(failure.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
