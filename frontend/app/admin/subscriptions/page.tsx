"use client";

/** Subscriptions & plans — records only; no payment collection. */

import { useCallback, useEffect, useState } from "react";
import { formatBytes, formatDateTime } from "@/components/admin-format";
import { ErrorState, LoadingState } from "@/components/states";
import {
  getAdminSubscriptions,
  type AdminSubscriptions,
} from "@/lib/api";

function limit(value: number | null): string {
  return value === null ? "Unlimited" : String(value);
}

export default function AdminSubscriptionsPage() {
  const [data, setData] = useState<AdminSubscriptions | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setData(await getAdminSubscriptions(signal));
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
  if (!data) return <LoadingState label="Loading subscriptions…" />;

  return (
    <section>
      <div className="admin-heading">
        <p className="eyebrow">SUBSCRIPTIONS & PLANS</p>
        <h1>Plans and who holds them.</h1>
        <p className="admin-intro">{data.billing_note}</p>
      </div>
      <h2 className="admin-section-title">Plans</h2>
      <table className="admin-table">
        <thead>
          <tr>
            <th>Plan</th>
            <th>Concurrent runs</th>
            <th>Runs / period</th>
            <th>Script chars</th>
            <th>Storage</th>
            <th>Upload</th>
            <th>Priority tier</th>
            <th>Active subscribers</th>
          </tr>
        </thead>
        <tbody>
          {data.plans.map((plan) => (
            <tr key={plan.id}>
              <td>
                {plan.name}
                {plan.is_default ? " (default)" : ""}
              </td>
              <td>{plan.max_concurrent_runs}</td>
              <td>{limit(plan.max_runs_per_period)}</td>
              <td>{limit(plan.max_script_chars)}</td>
              <td>
                {plan.max_storage_bytes === null
                  ? "Unlimited"
                  : formatBytes(plan.max_storage_bytes)}
              </td>
              <td>
                {plan.max_upload_bytes === null
                  ? "Unlimited"
                  : formatBytes(plan.max_upload_bytes)}
              </td>
              <td>{plan.priority_tier}</td>
              <td>{plan.active_subscribers}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h2 className="admin-section-title">Subscriptions</h2>
      {data.subscriptions.length === 0 ? (
        <p className="admin-empty">No subscriptions recorded.</p>
      ) : (
        <table className="admin-table">
          <thead>
            <tr>
              <th>User</th>
              <th>Plan</th>
              <th>Status</th>
              <th>Period start</th>
              <th>Period end</th>
              <th>Billing provider</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {data.subscriptions.map((sub) => (
              <tr key={sub.id}>
                <td>{sub.user_email ?? sub.user_id}</td>
                <td>{sub.plan ?? "—"}</td>
                <td>{sub.status}</td>
                <td>{formatDateTime(sub.current_period_start)}</td>
                <td>{formatDateTime(sub.current_period_end)}</td>
                <td>{sub.billing_provider ?? "—"}</td>
                <td>{formatDateTime(sub.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
