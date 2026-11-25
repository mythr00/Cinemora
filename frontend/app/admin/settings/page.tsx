"use client";

/** M8 — effective configuration, read-only overview and secret-safe.
 *
 * Phase 6 (A-15): there are NO backend update endpoints for these
 * settings — configuration is environment-based and applies on
 * restart (services/admin_settings says so itself). The page is
 * therefore labelled honestly as a read-only overview; no fake
 * management controls are offered. */

import { useCallback, useEffect, useState } from "react";
import { ErrorState, LoadingState } from "@/components/states";
import { getAdminSettings, type AdminSettings } from "@/lib/api";

const GROUP_LABELS: Record<string, string> = {
  credentials: "Credentials (configured state only)",
  providers: "Providers",
  auth_and_limits: "Auth & limits",
  pipeline: "Pipeline",
  pricing: "Pricing",
};

export default function AdminSettingsPage() {
  const [settings, setSettings] = useState<AdminSettings | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setSettings(await getAdminSettings(signal));
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
  if (!settings) return <LoadingState label="Loading settings…" />;

  return (
    <section>
      <div className="admin-heading">
        <p className="eyebrow">SYSTEM SETTINGS — READ-ONLY OVERVIEW</p>
        <h1>What the platform is actually running with.</h1>
        <p className="admin-intro">{settings.note}</p>
        <p className="admin-intro">
          Read-only: this page cannot change settings. Configuration
          is set through the deployment environment and applies when
          the service restarts.
        </p>
      </div>
      {Object.entries(settings.groups).map(([group, entries]) => (
        <div key={group}>
          <h2 className="admin-section-title">
            {GROUP_LABELS[group] ?? group}
          </h2>
          <table className="admin-table">
            <thead>
              <tr>
                <th>Setting</th>
                <th>Value</th>
                <th>Applies</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.name}>
                  <td>{entry.name}</td>
                  <td>
                    {entry.secret
                      ? entry.configured
                        ? "Configured"
                        : "Not configured"
                      : entry.value === null ||
                          entry.value === undefined ||
                          entry.value === ""
                        ? "Not set"
                        : String(entry.value)}
                    {entry.note && (
                      <span className="admin-cell-sub"> — {entry.note}</span>
                    )}
                  </td>
                  <td>
                    {entry.applies_on_restart ? "On restart" : "Immediately"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}
