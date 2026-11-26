"use client";

/** M2 — run detail with the two audited admin writes. */

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { formatDateTime, formatSeconds } from "@/components/admin-format";
import { ErrorState, LoadingState } from "@/components/states";
import {
  cancelAdminRun,
  getAdminRun,
  submitAdminReplacement,
  type AdminRunDetail,
} from "@/lib/api";

const ACTIVE = ["queued", "running", "awaiting_approval"];

export default function AdminRunDetailPage() {
  const params = useParams<{ id: string }>();
  const runId = params.id;
  const [run, setRun] = useState<AdminRunDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [acting, setActing] = useState(false);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        setRun(await getAdminRun(runId, signal));
      } catch (cause) {
        if (!signal?.aborted) setError(cause);
      }
    },
    [runId],
  );

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => { void load(controller.signal); }, 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [load]);

  if (error) return <ErrorState error={error} onRetry={() => void load()} />;
  if (!run) return <LoadingState label="Loading run…" />;

  const isActive = ACTIVE.includes(run.status);

  async function onCancel() {
    if (!window.confirm("Request cancellation of this run?")) return;
    setActing(true);
    setNotice(null);
    try {
      const result = await cancelAdminRun(runId);
      setNotice(
        result.cancel_requested
          ? "Cancellation requested. The worker records the run as cancelled at its next checkpoint; this action is in the audit log."
          : "The run was not in a cancellable state, so nothing changed.",
      );
      await load();
    } catch (cause) {
      setError(cause);
    } finally {
      setActing(false);
    }
  }

  async function onReplacement() {
    if (
      !window.confirm(
        "Submit a new run for this project? Completed stages are adopted where possible; the old run is not resumed.",
      )
    )
      return;
    setActing(true);
    setNotice(null);
    try {
      const result = await submitAdminReplacement(runId);
      setNotice(
        result.run
          ? `New run submitted: ${result.run.run_id} (${result.run.status}). This action is in the audit log.`
          : "The submission was deduplicated against an already-active run.",
      );
      await load();
    } catch (cause) {
      setError(cause);
    } finally {
      setActing(false);
    }
  }

  const execution = (run.execution ?? {}) as Record<string, unknown>;
  const search = ((execution.search_calls as Record<string, unknown> | undefined)?.searxng ?? {}) as Record<string, number>;
  const llm = (execution.llm_calls ?? {}) as Record<string, number>;
  const downloads = (execution.downloads ?? {}) as Record<string, number>;
  const report = (run.asset_report ?? {}) as Record<string, unknown>;
  const rejectionReasons = (report.rejection_reasons ?? {}) as Record<
    string,
    number
  >;

  return (
    <section>
      <div className="admin-heading">
        <p className="eyebrow">RUN DETAIL</p>
        <h1>{run.run_id.slice(0, 13)}…</h1>
        <p className="admin-intro">
          Project{" "}
          <Link href={run.studio_project_path}>
            {run.project_title ?? run.project_id}
          </Link>{" "}
          (opens in the studio). Metadata and health only — content
          stays in the studio.
        </p>
      </div>
      <dl className="admin-detail-grid">
        <div><dt>Status</dt><dd>{run.status}</dd></div>
        <div><dt>Stage</dt><dd>{run.stage ?? "—"}</dd></div>
        <div>
          <dt>Presence</dt>
          <dd>
            {run.presence
              ? run.presence.live
                ? `live — ${run.presence.worker_id ?? "worker"}, heartbeat ${formatDateTime(run.presence.heartbeat_at)}`
                : "no fresh heartbeat"
              : "—"}
          </dd>
        </div>
        <div><dt>Duration</dt><dd>{formatSeconds(run.wall_seconds)}</dd></div>
        <div><dt>Created</dt><dd>{formatDateTime(run.created_at)}</dd></div>
        <div><dt>Started</dt><dd>{formatDateTime(run.started_at)}</dd></div>
        <div><dt>Finished</dt><dd>{formatDateTime(run.finished_at)}</dd></div>
        <div>
          <dt>Cancel requested</dt>
          <dd>{run.cancel_requested ? "Yes" : "No"}</dd>
        </div>
      </dl>
      {run.error && (
        <div className="admin-error-block">
          <h2>Error</h2>
          <p>{run.error}</p>
        </div>
      )}
      <div className="admin-actions">
        {isActive && (
          <button
            type="button"
            className="button button-danger"
            onClick={() => void onCancel()}
            disabled={acting}
          >
            Cancel run
          </button>
        )}
        {!isActive && (
          <button
            type="button"
            className="button button-primary"
            onClick={() => void onReplacement()}
            disabled={acting}
          >
            Submit replacement run
          </button>
        )}
      </div>
      {notice && <p className="admin-notice">{notice}</p>}
      <h2 className="admin-section-title">Execution metrics</h2>
      {run.execution ? (
        <dl className="admin-detail-grid">
          <div><dt>Search calls</dt><dd>{search.total ?? 0} (web {search.web ?? 0} · images {search.images ?? 0} · video {search.video ?? 0})</dd></div>
          <div><dt>LLM calls</dt><dd>{llm.total ?? 0} · {llm.total_tokens ?? 0} tokens</dd></div>
          <div><dt>Downloads</dt><dd>{downloads.succeeded ?? 0} of {downloads.attempted ?? 0} succeeded</dd></div>
          <div>
            <dt>Monetary cost</dt>
            <dd>Not instrumented — no price table is configured.</dd>
          </div>
        </dl>
      ) : (
        <p className="admin-empty">
          No execution snapshot recorded for this run.
        </p>
      )}
      {Object.keys(rejectionReasons).length > 0 && (
        <>
          <h2 className="admin-section-title">Rejection taxonomy</h2>
          <table className="admin-table">
            <thead>
              <tr><th>Reason</th><th>Count</th></tr>
            </thead>
            <tbody>
              {Object.entries(rejectionReasons)
                .sort((a, b) => b[1] - a[1])
                .map(([reason, count]) => (
                  <tr key={reason}>
                    <td>{reason}</td>
                    <td>{count}</td>
                  </tr>
                ))}
            </tbody>
          </table>
        </>
      )}
      {run.production_state && (
        <>
          <h2 className="admin-section-title">Stage records</h2>
          <table className="admin-table">
            <thead>
              <tr><th>Stage</th><th>Status</th></tr>
            </thead>
            <tbody>
              {Object.entries(run.production_state.stages).map(
                ([stage, status]) => (
                  <tr key={stage}>
                    <td>{stage}</td>
                    <td>{status ?? "—"}</td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
