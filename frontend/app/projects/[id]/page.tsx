"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { StatusBadge } from "@/components/status-badge";
import { ProjectTabs } from "@/components/project-tabs";
import { ErrorState, LoadingState } from "@/components/states";
import { ArrowLeft } from "lucide-react";
import { AuthenticatedDownloadLink } from "@/components/authenticated-media";
import {
  cancelRun,
  getOutputs,
  getProjectStatus,
  getRun,
  listAssets,
  listRuns,
  type AssetDecisionRow,
  type AssetsPage,
  type ProjectOutputs,
  type ProjectStatus,
  type RunSummary,
} from "@/lib/api";

/**
 * Project detail = the API v1 overview (status, outputs, run
 * history, plan and certificate links).
 *
 * Brief Stage 3 (C-02): the legacy scene editor that used to share
 * this page was removed. Every endpoint it called (GET/PUT
 * /projects/{id}/editor, candidates, render-options, versions,
 * rerender, asset upload/review, editor history) is absent from the
 * committed backend — re-verified 404 under TestClient at HEAD
 * bfc5a06 — so the editor branch could never load and only ever
 * fell back to this overview. The original file, editor included,
 * is preserved verbatim at
 * frontend/preserved/project-legacy-editor-page.tsx. The real
 * revision editor is its own screen at /projects/[id]/edit
 * (register C-03, Brief Stage 7), linked below.
 *
 * Brief Stage 5 (C-07): the run history and the asset ledger gained
 * their missing UI on this page — run details fetched per run
 * (getRun), cooperative run cancellation (cancelRun, confirmed,
 * then re-read from the server — never optimistically faked), and
 * the paged asset-evidence section below (listAssets). The run
 * record the API returns carries no options field, so no options
 * summary is shown for a run; a project's options live on the
 * project record, not the run view.
 */

type Overview = { status: ProjectStatus; outputs: ProjectOutputs | null; runs: RunSummary[] };

const TERMINAL_STATUSES = ["completed", "failed", "cancelled"];
// Backend semantics (services/run_ledger.request_cancel): any
// non-terminal run accepts a cancellation request — a queued run
// is cancelled immediately, a running or awaiting run is flagged
// and stops at its next stage boundary (an awaiting run's flag is
// recorded on the run). Terminal runs refuse (the endpoint reports
// cancel_requested: false).
const CANCELLABLE_STATUSES = ["queued", "running", "awaiting_approval"];
const ASSETS_PAGE_SIZE = 50;

function verificationLabel(verification: AssetDecisionRow["verification"]): string {
  if (!verification) return "—";
  if (verification.accepted === true) return "accepted";
  if (verification.accepted === false) {
    return `rejected${verification.reason ? ` (${String(verification.reason)})` : ""}`;
  }
  if (typeof verification.reason === "string" && verification.reason) return verification.reason;
  return "recorded";
}

function rightsLabel(rights: AssetDecisionRow["rights"]): string {
  if (!rights) return "—";
  const license = typeof rights.license === "string" && rights.license ? rights.license : "unknown";
  const status = typeof rights.rights_status === "string" && rights.rights_status ? rights.rights_status : null;
  return status ? `${license} · ${status}` : license;
}

export default function ProjectPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [overview, setOverview] = useState<Overview | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);

  // C-07 run details + cancellation.
  const [openRunId, setOpenRunId] = useState<string | null>(null);
  const [runDetails, setRunDetails] = useState<Record<string, RunSummary>>({});
  const [runDetailErrors, setRunDetailErrors] = useState<Record<string, unknown>>({});
  const [confirmCancelId, setConfirmCancelId] = useState<string | null>(null);
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const [cancelNote, setCancelNote] = useState("");

  // C-07 asset evidence (paged ledger decisions for the latest run).
  const [assets, setAssets] = useState<AssetsPage | null>(null);
  const [assetsBusy, setAssetsBusy] = useState(true);
  const [assetsError, setAssetsError] = useState<unknown>(null);

  const loadOverview = useCallback(async () => {
    const [statusResult, outputsResult, runsResult] = await Promise.all([
      getProjectStatus(projectId),
      getOutputs(projectId).catch(() => null),
      listRuns(projectId).catch(() => ({ runs: [] as RunSummary[] })),
    ]);
    setOverview({ status: statusResult, outputs: outputsResult, runs: runsResult.runs || [] });
    setLoadError(null);
  }, [projectId]);

  const loadAssets = useCallback(async (offset: number) => {
    setAssetsBusy(true);
    setAssetsError(null);
    try {
      setAssets(await listAssets(projectId, ASSETS_PAGE_SIZE, offset));
    } catch (cause) {
      setAssetsError(cause);
    } finally {
      setAssetsBusy(false);
    }
  }, [projectId]);

  const loadRunDetail = useCallback(async (runId: string) => {
    try {
      const run = await getRun(runId);
      setRunDetails((current) => ({ ...current, [runId]: run }));
      setRunDetailErrors((current) => ({ ...current, [runId]: null }));
    } catch (cause) {
      setRunDetailErrors((current) => ({ ...current, [runId]: cause }));
    }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => {
      void loadOverview().catch((error) => setLoadError(error));
      void loadAssets(0);
    }, 0);
    return () => clearTimeout(timer);
  }, [loadOverview, loadAssets]);

  // While a production is active, keep the overview current (the
  // legacy page polled status the same way, via v1).
  const statusName = overview?.status.status;
  useEffect(() => {
    if (!statusName || TERMINAL_STATUSES.includes(statusName)) return;
    const timer = setInterval(() => { void loadOverview().catch(() => undefined); }, 3000);
    return () => clearInterval(timer);
  }, [statusName, loadOverview]);

  async function onToggleRunDetail(run: RunSummary) {
    if (openRunId === run.run_id) {
      setOpenRunId(null);
      return;
    }
    setOpenRunId(run.run_id);
    if (!runDetails[run.run_id]) void loadRunDetail(run.run_id);
  }

  async function onCancelRun(run: RunSummary) {
    setCancellingId(run.run_id);
    setCancelNote("");
    try {
      const result = await cancelRun(run.run_id);
      setCancelNote(
        result.cancel_requested
          ? "Cancellation recorded on the server. A queued run is cancelled immediately; a running run stops at its next stage boundary."
          : "Cancellation was not applied — the server reports the run had already reached a final state. The history below shows its actual state.",
      );
      // Never fake the outcome: re-read the overview (and the open
      // run detail) from the server.
      await loadOverview().catch(() => undefined);
      if (openRunId === run.run_id) void loadRunDetail(run.run_id);
    } catch (cause) {
      setCancelNote(cause instanceof Error ? cause.message : "The cancellation request failed.");
    } finally {
      setCancellingId(null);
      setConfirmCancelId(null);
    }
  }

  const assetsOffset = assets?.offset ?? 0;
  const assetsTotal = assets?.total ?? 0;

  return <main className="project-editor-page"><AppNavigation /><ProjectTabs projectId={projectId} /><section className="projects-content"><Link href="/projects" className="editor-back"><ArrowLeft size={14} strokeWidth={2} aria-hidden /> Projects</Link>
    {!overview && loadError ? <>
      <p className="eyebrow">PROJECT OVERVIEW</p>
      <h1>Project unavailable</h1>
      <ErrorState error={loadError} onRetry={() => { void loadOverview().catch((error) => setLoadError(error)); }} />
    </> : !overview ? <LoadingState className="projects-state" label="Loading project…" /> : <>
      <p className="eyebrow">PROJECT OVERVIEW</p>
      <h1>{overview.status.title || `${overview.status.category} production`}</h1>
      <p className="overview-status-line">Status: <StatusBadge status={overview.status.status} />{overview.status.current_stage ? ` · Stage: ${overview.status.current_stage.replaceAll("_", " ")}` : ""}</p>
      {overview.status.error && <p className="projects-error">{overview.status.error}</p>}
      <p><Link className="button button-primary" href={`/projects/${projectId}/plan`}>Production plan</Link>{" "}<Link className="button button-secondary" href={`/projects/${projectId}/edit`}>Edit video</Link>{" "}<Link className="button button-secondary" href={`/projects/${projectId}/certificate`}>Provenance certificate</Link>{" "}<a className="button button-secondary" href="#asset-evidence">Asset evidence</a></p>
      {overview.outputs && <section aria-label="Outputs"><h2>Outputs</h2><ul className="overview-outputs">
        {overview.outputs.film.exists && <li>Final film — <AuthenticatedDownloadLink path={overview.outputs.film.media_url || ""} filename={overview.outputs.film.filename}>download MP4</AuthenticatedDownloadLink></li>}
        {overview.outputs.captions.srt.exists && <li>Captions (SRT) — <AuthenticatedDownloadLink path={overview.outputs.captions.srt.media_url || ""} filename={overview.outputs.captions.srt.filename}>download</AuthenticatedDownloadLink></li>}
        {overview.outputs.captions.vtt.exists && <li>Captions (VTT) — <AuthenticatedDownloadLink path={overview.outputs.captions.vtt.media_url || ""} filename={overview.outputs.captions.vtt.filename}>download</AuthenticatedDownloadLink></li>}
        {overview.outputs.asset_report.exists && <li>Asset report — <AuthenticatedDownloadLink path={overview.outputs.asset_report.media_url || ""} filename={overview.outputs.asset_report.filename}>download JSON</AuthenticatedDownloadLink></li>}
        {overview.outputs.rights_manifest.exists && <li>Rights manifest — <AuthenticatedDownloadLink path={overview.outputs.rights_manifest.media_url || ""} filename={overview.outputs.rights_manifest.filename}>download JSON</AuthenticatedDownloadLink></li>}
        {overview.outputs.run_manifest.exists && <li>Run manifest — <AuthenticatedDownloadLink path={overview.outputs.run_manifest.media_url || ""} filename={overview.outputs.run_manifest.filename}>download JSON</AuthenticatedDownloadLink></li>}
        {!overview.outputs.film.exists && !overview.outputs.asset_report.exists && <li>No outputs recorded yet.</li>}
      </ul></section>}
      {overview.runs.length > 0 && <section aria-label="Production history"><h2>Production history</h2>
        {cancelNote && <p className="plan-message" role="status">{cancelNote}</p>}
        <ul className="overview-runs">{overview.runs.map((run) => {
          const detail = runDetails[run.run_id];
          const detailError = runDetailErrors[run.run_id];
          const cancellable = CANCELLABLE_STATUSES.includes(run.status) && !run.cancel_requested;
          return <li key={run.run_id} className="overview-run">
            <div className="overview-run-line">
              <StatusBadge status={run.status} /> · started {run.created_at ? new Date(run.created_at).toLocaleString() : "—"}{run.finished_at ? ` · finished ${new Date(run.finished_at).toLocaleString()}` : ""}{run.cancel_requested ? " · cancellation requested" : ""}{run.error ? ` · ${run.error}` : ""}
              {" "}<button type="button" className="run-action" aria-expanded={openRunId === run.run_id} onClick={() => void onToggleRunDetail(run)}>{openRunId === run.run_id ? "Hide details" : "Details"}</button>
              {cancellable && confirmCancelId !== run.run_id && (
                <button type="button" className="run-action run-action-danger" onClick={() => setConfirmCancelId(run.run_id)}>Cancel run</button>
              )}
              {cancellable && confirmCancelId === run.run_id && (
                <span className="run-cancel-confirm">
                  Cancel this run?{" "}
                  <button type="button" className="run-action run-action-danger" disabled={cancellingId === run.run_id} onClick={() => void onCancelRun(run)}>
                    {cancellingId === run.run_id ? "Cancelling…" : "Yes, cancel it"}
                  </button>{" "}
                  <button type="button" className="run-action" disabled={cancellingId === run.run_id} onClick={() => setConfirmCancelId(null)}>Keep running</button>
                </span>
              )}
            </div>
            {openRunId === run.run_id && (
              <div className="run-detail">
                {detailError != null ? (
                  <ErrorState error={detailError} onRetry={() => void loadRunDetail(run.run_id)} />
                ) : !detail ? (
                  <LoadingState label="Loading run details…" />
                ) : (
                  <dl className="run-detail-grid">
                    <div><dt>Run</dt><dd><code>{detail.run_id}</code></dd></div>
                    <div><dt>Status</dt><dd><StatusBadge status={detail.status} /></dd></div>
                    <div><dt>Stage</dt><dd>{detail.stage ? detail.stage.replaceAll("_", " ") : "—"}</dd></div>
                    <div><dt>Cancellation requested</dt><dd>{detail.cancel_requested ? "Yes" : "No"}</dd></div>
                    <div><dt>Created</dt><dd>{detail.created_at ? new Date(detail.created_at).toLocaleString() : "—"}</dd></div>
                    <div><dt>Started</dt><dd>{detail.started_at ? new Date(detail.started_at).toLocaleString() : "—"}</dd></div>
                    <div><dt>Finished</dt><dd>{detail.finished_at ? new Date(detail.finished_at).toLocaleString() : "—"}</dd></div>
                    <div><dt>Script hash</dt><dd>{detail.script_hash ? <code>{detail.script_hash.slice(0, 12)}…</code> : "—"}</dd></div>
                    {detail.error && <div><dt>Error</dt><dd>{detail.error}</dd></div>}
                  </dl>
                )}
              </div>
            )}
          </li>;
        })}</ul></section>}
      <section id="asset-evidence" aria-label="Asset evidence">
        <h2>Asset evidence</h2>
        <p className="asset-evidence-intro">
          The decision ledger for the latest run: every visual the production selected,
          rotated, generated, replaced, rejected, or left missing — with the verification
          outcome and rights classification recorded at decision time. A licence recorded
          as <strong>unknown</strong> is not cleared for publication.
        </p>
        {assetsBusy && <LoadingState className="projects-state" label="Loading asset evidence…" />}
        {!assetsBusy && assetsError != null && <ErrorState error={assetsError} onRetry={() => void loadAssets(assetsOffset)} />}
        {!assetsBusy && !assetsError && assets && assets.total === 0 && (
          <p className="asset-evidence-empty">
            No asset decisions are recorded for this project yet. The ledger fills as a
            production run selects and verifies its visuals.
          </p>
        )}
        {!assetsBusy && !assetsError && assets && assets.total > 0 && <>
          <div className="plan-table-wrap">
            <table className="plan-table">
              <thead><tr>
                <th scope="col">Sentence</th>
                <th scope="col">Scene</th>
                <th scope="col">Decision</th>
                <th scope="col">Asset</th>
                <th scope="col">Provider / source</th>
                <th scope="col">Evidence</th>
                <th scope="col">Verification</th>
                <th scope="col">Rights</th>
                <th scope="col">Recorded</th>
              </tr></thead>
              <tbody>
                {assets.items.map((row) => (
                  <tr key={row.id}>
                    <td>{row.sentence_number ?? "—"}</td>
                    <td>{row.scene_number ?? "—"}</td>
                    <td>{row.decision_type.replaceAll("_", " ")}</td>
                    <td>{row.asset_key ? <code title={row.asset_key}>{row.asset_key.slice(0, 16)}…</code> : "—"}</td>
                    <td>
                      {row.provider || "—"}
                      {row.source_url ? <> · <a href={row.source_url} target="_blank" rel="noreferrer">view source</a></> : null}
                    </td>
                    <td>{row.evidence_class || "—"}</td>
                    <td>{verificationLabel(row.verification)}</td>
                    <td>{rightsLabel(row.rights)}</td>
                    <td>{row.created_at ? new Date(row.created_at).toLocaleString() : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="asset-evidence-pager">
            <span>Showing {assetsOffset + 1}–{assetsOffset + assets.items.length} of {assetsTotal} decisions{assets.run_id ? <> for run <code>{assets.run_id.slice(0, 8)}…</code></> : null}</span>
            <button type="button" disabled={assetsOffset === 0} onClick={() => void loadAssets(Math.max(0, assetsOffset - ASSETS_PAGE_SIZE))}>Previous</button>
            <button type="button" disabled={assetsOffset + assets.items.length >= assetsTotal} onClick={() => void loadAssets(assetsOffset + ASSETS_PAGE_SIZE)}>Next</button>
          </p>
        </>}
      </section>
    </>}
  </section></main>;
}
