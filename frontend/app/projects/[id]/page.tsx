"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { ArrowLeft } from "lucide-react";
import {
  apiUrl,
  getOutputs,
  getProjectStatus,
  listRuns,
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
 * revision editor arrives as its own screen (register C-03).
 */

type Overview = { status: ProjectStatus; outputs: ProjectOutputs | null; runs: RunSummary[] };

const TERMINAL_STATUSES = ["completed", "failed", "cancelled"];

export default function ProjectPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [overview, setOverview] = useState<Overview | null>(null);
  const [loadError, setLoadError] = useState("");

  const loadOverview = useCallback(async () => {
    const [statusResult, outputsResult, runsResult] = await Promise.all([
      getProjectStatus(projectId),
      getOutputs(projectId).catch(() => null),
      listRuns(projectId).catch(() => ({ runs: [] as RunSummary[] })),
    ]);
    setOverview({ status: statusResult, outputs: outputsResult, runs: runsResult.runs || [] });
    setLoadError("");
  }, [projectId]);

  useEffect(() => {
    const timer = setTimeout(() => {
      void loadOverview().catch((error) => setLoadError(error instanceof Error ? error.message : "Could not load project."));
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

  return <main className="project-editor-page"><AppNavigation active="/projects" /><section className="projects-content"><Link href="/projects" className="editor-back"><ArrowLeft size={14} strokeWidth={2} aria-hidden /> Projects</Link>
    {!overview && loadError ? <>
      <p className="eyebrow">PROJECT OVERVIEW</p>
      <h1>Project unavailable</h1>
      <p className="projects-error">{loadError}</p>
    </> : !overview ? <p className="projects-state">Loading project…</p> : <>
      <p className="eyebrow">PROJECT OVERVIEW</p>
      <h1>{overview.status.title || `${overview.status.category} production`}</h1>
      <p>Status: <strong>{overview.status.status.replaceAll("_", " ")}</strong>{overview.status.current_stage ? ` · Stage: ${overview.status.current_stage.replaceAll("_", " ")}` : ""}</p>
      {overview.status.error && <p className="projects-error">{overview.status.error}</p>}
      <p><Link className="button button-primary" href={`/projects/${projectId}/plan`}>Production plan</Link>{" "}<Link className="button button-secondary" href={`/projects/${projectId}/certificate`}>Provenance certificate</Link></p>
      {overview.outputs && <section aria-label="Outputs"><h2>Outputs</h2><ul className="overview-outputs">
        {overview.outputs.film.exists && <li>Final film — <a href={apiUrl(overview.outputs.film.media_url || "")}>download MP4</a></li>}
        {overview.outputs.captions.srt.exists && <li>Captions (SRT) — <a href={apiUrl(overview.outputs.captions.srt.media_url || "")}>download</a></li>}
        {overview.outputs.captions.vtt.exists && <li>Captions (VTT) — <a href={apiUrl(overview.outputs.captions.vtt.media_url || "")}>download</a></li>}
        {overview.outputs.asset_report.exists && <li>Asset report — <a href={apiUrl(overview.outputs.asset_report.media_url || "")}>download JSON</a></li>}
        {overview.outputs.rights_manifest.exists && <li>Rights manifest — <a href={apiUrl(overview.outputs.rights_manifest.media_url || "")}>download JSON</a></li>}
        {overview.outputs.run_manifest.exists && <li>Run manifest — <a href={apiUrl(overview.outputs.run_manifest.media_url || "")}>download JSON</a></li>}
        {!overview.outputs.film.exists && !overview.outputs.asset_report.exists && <li>No outputs recorded yet.</li>}
      </ul></section>}
      {overview.runs.length > 0 && <section aria-label="Production history"><h2>Production history</h2><ul className="overview-runs">{overview.runs.map((run) => <li key={run.run_id}><strong>{run.status.replaceAll("_", " ")}</strong> · started {run.created_at ? new Date(run.created_at).toLocaleString() : "—"}{run.finished_at ? ` · finished ${new Date(run.finished_at).toLocaleString()}` : ""}{run.error ? ` · ${run.error}` : ""}</li>)}</ul></section>}
    </>}
  </section></main>;
}
