"use client";

/** M5 — composite infrastructure health, per component. */

import { useCallback, useEffect, useState } from "react";
import { ErrorState, LoadingState } from "@/components/states";
import { getAdminHealth, type AdminHealth } from "@/lib/api";

const LABELS: Record<string, string> = {
  api: "API process",
  database: "Database",
  redis: "Redis",
  workers: "Workers",
  queues: "Queues",
  providers: "Provider backoff",
  storage: "Storage",
  public_site: "Public site",
};

function describe(component: { status: string } & Record<string, unknown>) {
  const facts: string[] = [];
  if (component.expected_head) {
    facts.push(
      `schema ${String(component.current_revision ?? "unstamped")} / head ${String(component.expected_head)}`,
    );
  }
  if (typeof component.latency_ms === "number") {
    facts.push(`ping ${component.latency_ms}ms`);
  }
  if (Array.isArray(component.workers)) {
    facts.push(`${component.workers.length} registered`);
  }
  if (typeof component.ledger_queued === "number") {
    facts.push(
      `${component.ledger_queued} queued · ${String(component.ledger_running)} running (ledger)`,
    );
  }
  if (Array.isArray(component.backed_off_scopes)) {
    facts.push(
      component.backed_off_scopes.length === 0
        ? "no scope backed off"
        : `${component.backed_off_scopes.length} scope(s) backed off`,
    );
  }
  if (typeof component.media_total_bytes === "number") {
    facts.push(
      `${(component.media_total_bytes / 1_000_000).toFixed(1)} MB of media`,
    );
  }
  if (typeof component.http_status === "number") {
    facts.push(`HTTP ${component.http_status} from ${String(component.url)}`);
  }
  if (typeof component.note === "string") facts.push(component.note);
  if (typeof component.error === "string") facts.push(component.error);
  return facts.join(" · ");
}

export default function AdminHealthPage() {
  const [health, setHealth] = useState<AdminHealth | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setHealth(await getAdminHealth(signal));
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
  if (!health) return <LoadingState label="Loading health…" />;

  return (
    <section>
      <div className="admin-heading">
        <p className="eyebrow">INFRASTRUCTURE HEALTH</p>
        <h1>Each component, on its own evidence.</h1>
        <p className="admin-intro">
          No single green light: a component is only as healthy as
          its own check. The legacy /health endpoints stay static for
          compatibility and are not used here.
        </p>
      </div>
      <div className="admin-card-grid">
        {Object.entries(health.components).map(([name, component]) => (
          <article className="admin-card" key={name}>
            <p className="admin-card-label">{LABELS[name] ?? name}</p>
            <p
              className={`admin-health-status is-${component.status}`}
            >
              {component.status}
            </p>
            <p className="admin-card-hint">{describe(component)}</p>
          </article>
        ))}
      </div>
    </section>
  );
}
