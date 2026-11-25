"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { StatusBadge } from "@/components/status-badge";
import { ProjectTabs } from "@/components/project-tabs";
import { StageProgress } from "@/components/stage-progress";
import { ErrorState, LoadingState } from "@/components/states";
import { ArrowLeft, ArrowUpRight, Sparkles } from "lucide-react";
import { AuthenticatedDownloadLink, AuthenticatedVideo } from "@/components/authenticated-media";
import {
  cancelRun,
  getCurrentUser,
  getOutputs,
  getProjectStatus,
  getRun,
  listRuns,
  type ProjectOutputs,
  type ProjectStatus,
  type RunSummary,
} from "@/lib/api";

/**
 * Project overview = the workspace hub (redesign F1, plan §3.5).
 *
 * Persistent project header + tabs (ProjectTabs); body: Status
 * panel (status, StageProgress, latest run summary, the run's
 * two-step cancel, run failure status — FS-4: simple generic
 * message for ordinary users, full detail only for admins), Delivery panel (final
 * film + film/SRT/VTT downloads — reports and manifests live on
 * the Delivery tab), Runs panel (history with expandable run
 * detail), and a Next action area computed from real state.
 * The asset-evidence ledger that used to sit at the bottom of
 * this page is now the Assets tab (§3.6) — same listAssets data.
 *
 * Brief Stage 3 (C-02): the legacy scene editor that used to share
 * this page was removed (its endpoints are absent from the
 * committed backend); the original file is preserved verbatim at
 * frontend/preserved/project-legacy-editor-page.tsx. The real
 * revision editor is its own screen at /projects/[id]/edit.
 *
 * Brief Stage 5 (C-07): run details are fetched per run (getRun);
 * run cancellation is cooperative (cancelRun, confirmed, then
 * re-read from the server — never optimistically faked). The run
 * record the API returns carries no options field, so no options
 * summary is shown for a run.
 */

type Overview = { status: ProjectStatus; outputs: ProjectOutputs | null; runs: RunSummary[] };

// FS-4: ordinary users see a simple, truthful failure status —
// never internal failure detail (exception / stage / provider /
// worker text). The API already sanitizes its responses for
// non-admin principals; this is the presentation-side guard so no
// residual detail field is ever rendered verbatim for them.
// Admins (whose API responses carry the full detail) still see it.
const GENERIC_FAILURE_MESSAGE = "Video generation failed";

function failureDisplay(error: string | null | undefined, isAdmin: boolean): string | null {
  if (!error) return null;
  return isAdmin ? error : GENERIC_FAILURE_MESSAGE;
}

const TERMINAL_STATUSES = ["completed", "failed", "cancelled"];
// Backend semantics (services/run_ledger.request_cancel): any
// non-terminal run accepts a cancellation request — a queued run
// is cancelled immediately, a running or awaiting run is flagged
// and stops at its next stage boundary (an awaiting run's flag is
// recorded on the run). Terminal runs refuse (the endpoint reports
// cancel_requested: false).
const CANCELLABLE_STATUSES = ["queued", "running", "awaiting_approval"];

export default function ProjectPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [overview, setOverview] = useState<Overview | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  // FS-4: only admin principals may see full failure detail; the
  // role comes from /auth/me (the same source AdminShell uses).
  const [isAdmin, setIsAdmin] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void getCurrentUser()
      .then((user) => {
        if (!cancelled) setIsAdmin(user?.role === "admin" || user?.role === "super_admin");
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  // C-07 run details + cancellation.
  const [openRunId, setOpenRunId] = useState<string | null>(null);
  const [runDetails, setRunDetails] = useState<Record<string, RunSummary>>({});
  const [runDetailErrors, setRunDetailErrors] = useState<Record<string, unknown>>({});
  const [confirmCancelId, setConfirmCancelId] = useState<string | null>(null);
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const [cancelNote, setCancelNote] = useState("");

  const loadOverview = useCallback(async () => {
    const [statusResult, outputsResult, runsResult] = await Promise.all([
      getProjectStatus(projectId),
      getOutputs(projectId).catch(() => null),
      listRuns(projectId).catch(() => ({ runs: [] as RunSummary[] })),
    ]);
    setOverview({ status: statusResult, outputs: outputsResult, runs: runsResult.runs || [] });
    setLoadError(null);
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
    }, 0);
    return () => clearTimeout(timer);
  }, [loadOverview]);

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

  // The two-step cancel control, shared by the Status panel
  // (latest run) and the Runs panel (every run) — one state, one
  // handler, identical semantics in both places.
  function cancelControls(run: RunSummary): ReactNode {
    const cancellable = CANCELLABLE_STATUSES.includes(run.status) && !run.cancel_requested;
    if (!cancellable) return null;
    if (confirmCancelId !== run.run_id) {
      return (
        <button type="button" className="run-action run-action-danger" onClick={() => setConfirmCancelId(run.run_id)}>Cancel run</button>
      );
    }
    return (
      <span className="run-cancel-confirm">
        Cancel this run?{" "}
        <button type="button" className="run-action run-action-danger" disabled={cancellingId === run.run_id} onClick={() => void onCancelRun(run)}>
          {cancellingId === run.run_id ? "Cancelling…" : "Yes, cancel it"}
        </button>{" "}
        <button type="button" className="run-action" disabled={cancellingId === run.run_id} onClick={() => setConfirmCancelId(null)}>Keep running</button>
      </span>
    );
  }

  const latestRun = overview?.runs[0] ?? null;
  const film = overview?.outputs?.film;
  const nextAction = overview ? nextActionFor(overview.status.status, projectId) : null;

  return <main className="app-page"><AppNavigation /><ProjectTabs projectId={projectId} /><section className="projects-content"><Link href="/projects" className="editor-back"><ArrowLeft size={14} strokeWidth={2} aria-hidden /> Projects</Link>
    {!overview && loadError ? <>
      <p className="eyebrow">PROJECT OVERVIEW</p>
      <h1>Project unavailable</h1>
      <ErrorState error={loadError} onRetry={() => { void loadOverview().catch((error) => setLoadError(error)); }} />
    </> : !overview ? <LoadingState className="projects-state" label="Loading project…" /> : <>
      <div className="project-header">
        <p className="eyebrow">PROJECT OVERVIEW</p>
        <h1>{overview.status.title || `${overview.status.category} production`}</h1>
        <p className="overview-status-line">Status: <StatusBadge status={overview.status.status} />{overview.status.current_stage ? ` · Stage: ${overview.status.current_stage.replaceAll("_", " ")}` : ""}</p>
        <p><Link className="button" href={`/assistant?project=${projectId}`}><Sparkles size={14} strokeWidth={2} aria-hidden /> Ask the assistant</Link></p>
      </div>
      {failureDisplay(overview.status.error, isAdmin) && <p className="projects-error" role="alert">{failureDisplay(overview.status.error, isAdmin)}</p>}

      {nextAction && <div className="next-action"><p><strong>Next step.</strong> {nextAction.text}</p><Link className="button button-primary" href={nextAction.href}>{nextAction.label} <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div>}

      <div className="hub-grid">
        <section className="hub-panel" aria-label="Production status">
          <h2>Status</h2>
          <p className="overview-status-line"><StatusBadge status={overview.status.status} />{overview.status.current_stage ? ` · Stage: ${overview.status.current_stage.replaceAll("_", " ")}` : ""}</p>
          {overview.status.stages && <StageProgress stages={overview.status.stages} currentStage={overview.status.current_stage} />}
          {latestRun && <p className="hub-panel-note">Latest run: <StatusBadge status={latestRun.status} /> · started {latestRun.created_at ? new Date(latestRun.created_at).toLocaleString() : "—"}{latestRun.cancel_requested ? " · cancellation requested" : ""}</p>}
          {latestRun && <p>{cancelControls(latestRun)}</p>}
          {cancelNote && <p className="plan-message" role="status">{cancelNote}</p>}
        </section>

        <section className="hub-panel" aria-label="Delivery">
          <h2>Delivery</h2>
          {film?.exists ? <>
            <div className="media-frame">
              <AuthenticatedVideo src={film.media_url || ""} controls preload="metadata" />
            </div>
            <ul className="download-list">
              <li>Final film — <AuthenticatedDownloadLink path={film.media_url || ""} filename={film.filename}>download MP4</AuthenticatedDownloadLink></li>
              {overview.outputs?.captions.srt.exists && <li>Captions (SRT) — <AuthenticatedDownloadLink path={overview.outputs.captions.srt.media_url || ""} filename={overview.outputs.captions.srt.filename}>download</AuthenticatedDownloadLink></li>}
              {overview.outputs?.captions.vtt.exists && <li>Captions (VTT) — <AuthenticatedDownloadLink path={overview.outputs.captions.vtt.media_url || ""} filename={overview.outputs.captions.vtt.filename}>download</AuthenticatedDownloadLink></li>}
            </ul>
            {isAdmin ? <p className="hub-panel-note">Asset report, rights manifest and run manifest are on the <Link className="text-link" href={`/projects/${projectId}/certificate`}>Delivery tab</Link>.</p> : <p className="hub-panel-note">Asset report, rights manifest and run manifest are recorded with the production.</p>}
          </> : <p className="hub-panel-note">No finished film yet. When a run completes, the film and its captions appear here.</p>}
        </section>
      </div>

      {overview.runs.length > 0 && <section aria-label="Production history"><h2>Production history</h2>
        <ul className="overview-runs">{overview.runs.map((run) => {
          const detail = runDetails[run.run_id];
          const detailError = runDetailErrors[run.run_id];
          return <li key={run.run_id} className="overview-run">
            <div className="overview-run-line">
              <StatusBadge status={run.status} /> · started {run.created_at ? new Date(run.created_at).toLocaleString() : "—"}{run.finished_at ? ` · finished ${new Date(run.finished_at).toLocaleString()}` : ""}{run.cancel_requested ? " · cancellation requested" : ""}{failureDisplay(run.error, isAdmin) ? ` · ${failureDisplay(run.error, isAdmin)}` : ""}
              {" "}<button type="button" className="run-action" aria-expanded={openRunId === run.run_id} onClick={() => void onToggleRunDetail(run)}>{openRunId === run.run_id ? "Hide details" : "Details"}</button>
              {cancelControls(run)}
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
                    {failureDisplay(detail.error, isAdmin) && <div><dt>Error</dt><dd>{failureDisplay(detail.error, isAdmin)}</dd></div>}
                  </dl>
                )}
              </div>
            )}
          </li>;
        })}</ul></section>}
    </>}
  </section></main>;
}

// The next real action, computed from the project's actual
// status — never a generic CTA.
function nextActionFor(status: string, projectId: string): { text: string; label: string; href: string } | null {
  switch (status) {
    case "awaiting_approval":
      return { text: "The production plan is ready and production is paused on your decision.", label: "Review the plan", href: `/projects/${projectId}/plan` };
    case "completed":
      return { text: "The film is finished. Open it in the editor, or check delivery and provenance.", label: "Open the editor", href: `/projects/${projectId}/edit` };
    case "failed":
      // FS-4: simple failure status + next action on this page —
      // no internal failure detail, and no link into the
      // admin-only asset decisions surface.
      return { text: "Video generation failed. The run history below shows the run's status — start a new production from Create when you're ready, or contact support if it keeps failing.", label: "Stay on overview", href: `/projects/${projectId}` };
    case "created":
    case "queued":
    case "pipeline_queued":
    case "running":
      return { text: "Production is under way. Follow its stages in the status panel.", label: "View the plan", href: `/projects/${projectId}/plan` };
    default:
      return null;
  }
}
