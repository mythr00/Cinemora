"use client";

/** M4 — provider usage: call counts now; cost when instrumented. */

import { useCallback, useEffect, useState } from "react";
import { formatBytes } from "@/components/admin-format";
import { ErrorState, LoadingState } from "@/components/states";
import { getAdminProviderUsage, type AdminProviderUsage } from "@/lib/api";

export default function AdminProvidersPage() {
  const [usage, setUsage] = useState<AdminProviderUsage | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setUsage(await getAdminProviderUsage(signal));
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
  if (!usage) return <LoadingState label="Loading provider usage…" />;

  const totals = usage.totals;
  const cards = [
    {
      label: `Search calls (${usage.window_days}d)`,
      value: String(totals.search_calls.total),
      hint: `web ${totals.search_calls.web} · images ${totals.search_calls.images} · video ${totals.search_calls.video}`,
    },
    {
      label: "BrewKeg LLM calls",
      value: String(totals.llm_calls.total),
      hint: `${totals.llm_calls.total_tokens} tokens returned by the gateway`,
    },
    {
      label: "Archive.org searches",
      value: String(totals.archive_org_searches),
    },
    {
      label: "Wikimedia searches",
      value: String(totals.wikimedia_searches),
    },
    {
      label: "Candidates discovered",
      value: String(totals.candidates_discovered),
    },
    {
      label: "Downloads",
      value: `${totals.downloads.succeeded} of ${totals.downloads.attempted} succeeded`,
      hint: formatBytes(totals.downloads.bytes_downloaded),
    },
    {
      label: "Monetary cost",
      value: "Not instrumented",
      hint: usage.monetary_cost_note,
    },
  ];

  return (
    <section>
      <div className="admin-heading">
        <p className="eyebrow">AI PROVIDER USAGE</p>
        <h1>Calls counted; money not yet.</h1>
        <p className="admin-intro">
          Aggregated from each run&apos;s execution snapshot — the
          same record the production report carries. Cost stays
          unreported until a price table exists; it is never
          estimated.
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
      <h2 className="admin-section-title">Per run</h2>
      {usage.per_run.length === 0 ? (
        <p className="admin-empty">
          No runs with execution snapshots in this window.
        </p>
      ) : (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Run</th>
              <th>Status</th>
              <th>Search</th>
              <th>LLM</th>
              <th>Downloads</th>
            </tr>
          </thead>
          <tbody>
            {usage.per_run.map((row) => {
              const search = (row.search_calls ?? {}) as { total?: number };
              const llm = (row.llm_calls ?? {}) as { total?: number };
              const downloads = (row.downloads ?? {}) as {
                attempted?: number;
                succeeded?: number;
              };
              return (
                <tr key={String(row.run_id)}>
                  <td>{String(row.run_id).slice(0, 8)}…</td>
                  <td>{String(row.status)}</td>
                  <td>{search.total ?? 0}</td>
                  <td>{llm.total ?? 0}</td>
                  <td>
                    {downloads.succeeded ?? 0} / {downloads.attempted ?? 0}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}
