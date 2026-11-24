"use client";

/** M7 — the admin audit log viewer (read-only by design). */

import { useCallback, useEffect, useState } from "react";
import { formatDateTime } from "@/components/admin-format";
import { ErrorState, LoadingState } from "@/components/states";
import { listAdminAuditLog, type AdminAuditEntry } from "@/lib/api";

export default function AdminAuditPage() {
  const [entries, setEntries] = useState<AdminAuditEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setBusy(true);
    try {
      const data = await listAdminAuditLog(
        {
          action: action || undefined,
          actor: actor || undefined,
          target_id: target || undefined,
        },
        signal,
      );
      setEntries(data.entries);
      setTotal(data.total);
    } catch (cause) {
      if (!signal?.aborted) setError(cause);
    } finally {
      if (!signal?.aborted) setBusy(false);
    }
  }, [action, actor, target]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => void load(controller.signal), 200);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [load]);

  return (
    <section>
      <div className="admin-heading">
        <p className="eyebrow">AUDIT LOG</p>
        <h1>What administration did, and when.</h1>
        <p className="admin-intro">
          Append-only: entries cannot be edited or deleted through
          any interface. Sign-ins, denied attempts, run actions,
          account changes and plan assignments are all recorded
          with their actor and target.
        </p>
      </div>
      <div className="admin-toolbar">
        <span>{busy ? "Loading entries…" : `${total} entries`}</span>
        <div className="admin-filters">
          <label className="admin-filter">
            <span>Action</span>
            <input
              type="search"
              value={action}
              onChange={(event) => setAction(event.target.value)}
              placeholder="run.cancel"
              aria-label="Filter by action"
            />
          </label>
          <label className="admin-filter">
            <span>Actor</span>
            <input
              type="search"
              value={actor}
              onChange={(event) => setActor(event.target.value)}
              placeholder="email"
              aria-label="Filter by actor"
            />
          </label>
          <label className="admin-filter">
            <span>Target</span>
            <input
              type="search"
              value={target}
              onChange={(event) => setTarget(event.target.value)}
              placeholder="id"
              aria-label="Filter by target id"
            />
          </label>
        </div>
      </div>
      {!!error && <ErrorState error={error} onRetry={() => void load()} />}
      {busy && !error && <LoadingState label="Loading audit log…" />}
      {!busy && !error && entries.length === 0 && (
        <p className="admin-empty">No entries match.</p>
      )}
      {!busy && !error && entries.length > 0 && (
        <table className="admin-table">
          <thead>
            <tr>
              <th>When</th>
              <th>Actor</th>
              <th>Action</th>
              <th>Target</th>
              <th>Result</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={entry.id}>
                <td>{formatDateTime(entry.created_at)}</td>
                <td>
                  {entry.actor_identity}
                  <span className="admin-cell-sub">
                    {" "}
                    · {entry.actor_role} · {entry.auth_method}
                  </span>
                </td>
                <td>{entry.action}</td>
                <td>
                  {entry.target_type
                    ? `${entry.target_type}: ${entry.target_id ?? ""}`
                    : "—"}
                </td>
                <td>{entry.result}</td>
                <td className="admin-cell-detail">
                  {entry.error
                    ? entry.error
                    : entry.detail
                      ? JSON.stringify(entry.detail)
                      : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
