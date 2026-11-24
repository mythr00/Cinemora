"use client";

/** Phase N — Developer API visibility: every key and webhook on
 * the platform with its recorded usage. Secrets and hashes are
 * never part of this surface; issuance/revocation events live in
 * the audit log. */

import { useCallback, useEffect, useState } from "react";
import { ErrorState, LoadingState } from "@/components/states";
import { formatDateTime } from "@/components/admin-format";
import {
  getAdminDeveloperOverview,
  type AdminDeveloperOverview,
} from "@/lib/api";

export default function AdminDeveloperPage() {
  const [overview, setOverview] = useState<AdminDeveloperOverview | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setOverview(await getAdminDeveloperOverview(signal));
    } catch (cause) {
      if (!signal?.aborted) setError(cause);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void load(controller.signal);
    }, 0);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [load]);

  if (error) return <ErrorState error={error} onRetry={() => void load()} />;
  if (!overview)
    return <LoadingState label="Loading developer API overview…" />;

  const { totals } = overview;
  return (
    <section>
      <div className="admin-heading">
        <p className="eyebrow">DEVELOPER API</p>
        <h1>Keys, webhooks and recorded API usage.</h1>
        <p className="admin-intro">
          {totals.api_keys} API keys ({totals.active_api_keys} active) with{" "}
          {totals.api_requests_recorded} requests recorded from real
          traffic; {totals.webhooks} webhook endpoints with{" "}
          {totals.webhook_deliveries} delivery attempts. Key issuance and
          revocation events are in the audit log.
        </p>
      </div>

      <h2 className="admin-section-title">API keys</h2>
      {overview.api_keys.length === 0 ? (
        <p>No API keys have been created.</p>
      ) : (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Owner</th>
              <th>Name</th>
              <th>Prefix</th>
              <th>Scopes</th>
              <th>Status</th>
              <th>Requests</th>
              <th>Last used</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {overview.api_keys.map((key) => (
              <tr key={key.id}>
                <td>{key.owner_email ?? "—"}</td>
                <td>{key.name}</td>
                <td>
                  <code>{key.key_prefix}…</code>
                </td>
                <td>{key.scopes.join(", ")}</td>
                <td>{key.status}</td>
                <td>{key.request_count}</td>
                <td>{formatDateTime(key.last_used_at)}</td>
                <td>{formatDateTime(key.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2 className="admin-section-title">Webhook endpoints</h2>
      {overview.webhooks.length === 0 ? (
        <p>No webhook endpoints have been registered.</p>
      ) : (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Owner</th>
              <th>URL</th>
              <th>Events</th>
              <th>Status</th>
              <th>Deliveries</th>
              <th>Registered</th>
            </tr>
          </thead>
          <tbody>
            {overview.webhooks.map((endpoint) => (
              <tr key={endpoint.id}>
                <td>{endpoint.owner_email ?? "—"}</td>
                <td>
                  <code>{endpoint.url}</code>
                </td>
                <td>{endpoint.events.join(", ")}</td>
                <td>{endpoint.status}</td>
                <td>{endpoint.delivery_count}</td>
                <td>{formatDateTime(endpoint.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
