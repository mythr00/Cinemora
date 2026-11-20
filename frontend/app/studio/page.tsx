"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { StatusBadge } from "@/components/status-badge";
import { EmptyState, ErrorState, LoadingState } from "@/components/states";
import { getProjectStatus, listProjects, type ProjectStatus, type ProjectSummary } from "@/lib/api";
import { StageProgress } from "@/components/stage-progress";
import { AuthenticatedVideo } from "@/components/authenticated-media";
import { ArrowUpRight } from "lucide-react";

type Project = ProjectSummary;

function label(value: string) { return value.replaceAll("_", " "); }

const ACTIVE_STATUSES = ["created", "queued", "pipeline_queued", "running", "awaiting_approval"];
const IN_PRODUCTION_STATUSES = ["created", "queued", "pipeline_queued", "running"];

// Redesign F1 (plan §3.2) — the Production desk. Approvals queue
// first; every active (non-terminal) production with its real
// StageProgress, polled at 3s like the project detail page
// (polling parity); recent & finished cards below; the summary
// strip counts the fetched list by real status and says so.
// Data: listProjects + getProjectStatus per active project —
// no new endpoints, and no quality chip: the list response
// carries no verdicts today (§3.4 defers it rather than faking
// it with per-project certificate fetches).
export default function StudioPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [stageByProject, setStageByProject] = useState<Record<string, ProjectStatus>>({});
  const projectsRef = useRef<Project[]>([]);

  const load = useCallback(async (signal?: AbortSignal, silent = false) => {
    if (!silent) setError(null);
    try {
      const data = await listProjects(signal);
      const loaded = data.projects || [];
      projectsRef.current = loaded;
      if (!signal?.aborted) setProjects(loaded);
      const active = loaded.filter((project) => ACTIVE_STATUSES.includes(project.status));
      if (active.length > 0) {
        const entries = await Promise.all(
          active.map(async (project) => {
            try {
              return [project.id, await getProjectStatus(project.id, signal)] as const;
            } catch {
              return null;
            }
          }),
        );
        if (!signal?.aborted) {
          setStageByProject((current) => {
            const next = { ...current };
            for (const entry of entries) if (entry) next[entry[0]] = entry[1];
            return next;
          });
        }
      }
    } catch (cause) {
      if (!signal?.aborted && !silent) setError(cause);
    } finally { if (!signal?.aborted && !silent) setBusy(false); }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => { void load(controller.signal); }, 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [load]);

  // 3s polling while any production is active — the same cadence
  // the project detail page uses, so the desk never shows a
  // staler state than the workspace does.
  useEffect(() => {
    const interval = setInterval(() => {
      if (projectsRef.current.some((project) => ACTIVE_STATUSES.includes(project.status))) {
        void load(undefined, true);
      }
    }, 3000);
    return () => clearInterval(interval);
  }, [load]);

  const activeProjects = projects.filter((project) => ACTIVE_STATUSES.includes(project.status));
  const inProductionCount = projects.filter((project) => IN_PRODUCTION_STATUSES.includes(project.status)).length;
  const awaitingApproval = projects.filter(
    (project) => project.status === "awaiting_approval" || project.latest_run?.status === "awaiting_approval",
  );
  const completeCount = projects.filter((project) => project.status === "completed").length;
  const failedCount = projects.filter((project) => project.status === "failed").length;
  const recentFinished = projects.filter((project) => !ACTIVE_STATUSES.includes(project.status)).slice(0, 4);

  function projectCard(project: Project) {
    return <article className="dashboard-project-card" key={project.id}>
      <Link className={`project-art ${project.video_url ? "has-video" : ""}`} href={`/projects/${project.id}`} aria-label={`Open ${project.category} project`}>
        {project.video_url ? <AuthenticatedVideo src={project.video_url} preload="metadata" muted playsInline /> : <span className="project-art-mark"><span className="reel-glyph" /></span>}
        <StatusBadge status={project.status} overlay />
      </Link>
      <div className="dashboard-project-info"><div><p>{project.category}</p><time dateTime={project.created_at}>{new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(new Date(project.created_at))}</time></div><h3>{project.title || project.script_preview || "Untitled production"}</h3><p className="project-stage">{project.current_stage ? `Stage · ${label(project.current_stage)}` : project.video_url ? "Export available" : "Production status available"}</p>{stageByProject[project.id] && <StageProgress stages={stageByProject[project.id].stages} currentStage={stageByProject[project.id].current_stage} />}<Link href={`/projects/${project.id}`}>Open project <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div>
    </article>;
  }

  return <main className="app-page studio-dashboard">
    <AppNavigation />
    <div className="dashboard-content">
      <section className="dashboard-welcome">
        <div><p className="eyebrow">YOUR PRODUCTION DESK</p><h1>Good stories deserve a considered cut.</h1><p>Start with a script, follow each production as it moves, and return to the work whenever you’re ready.</p></div>
        <Link className="button button-primary" href="/create">Start a production <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>
      </section>

      <section className="dashboard-metrics" aria-label="Production summary">
        <div><span>ALL PROJECTS</span><strong>{busy ? "—" : projects.length}</strong></div>
        <div><span>IN PRODUCTION</span><strong>{busy ? "—" : inProductionCount}</strong></div>
        <div><span>AWAITING APPROVAL</span><strong>{busy ? "—" : awaitingApproval.length}</strong></div>
        <div><span>COMPLETED</span><strong>{busy ? "—" : completeCount}</strong></div>
        <div><span>FAILED</span><strong>{busy ? "—" : failedCount}</strong></div>
        <p>Counts are computed from the live project list.</p>
      </section>

      {!busy && !error && awaitingApproval.length > 0 && (
        <section className="dashboard-approvals" aria-label="Awaiting your approval">
          <div className="section-heading"><div><p className="eyebrow">NEEDS YOUR DECISION</p><h2>Awaiting your approval.</h2></div></div>
          <ul className="approval-queue">
            {awaitingApproval.map((project) => (
              <li key={project.id}>
                <StatusBadge status="awaiting_approval" />
                <span className="approval-queue-title">{project.title || project.script_preview || "Untitled production"}</span>
                <span>Its production plan is ready for review — production starts only when you approve it.</span>
                <Link className="text-link" href={`/projects/${project.id}/plan`}>Review the plan <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>
              </li>
            ))}
          </ul>
        </section>
      )}

      {busy && <LoadingState className="dashboard-loading" label="Loading your project library…" />}
      {!!error && <ErrorState className="dashboard-error" error={error} onRetry={() => { setBusy(true); void load(); }} />}
      {!busy && !error && projects.length === 0 && <EmptyState className="dashboard-empty" eyebrow="A CLEAR FIRST FRAME" title="Your next film starts here." description="Create a project from a script. Its real production state and export will appear in this space." action={<Link className="button button-primary" href="/create">Create your first film <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>} />}

      {!busy && !error && activeProjects.length > 0 && (
        <section className="dashboard-projects" aria-label="Active productions">
          <div className="section-heading"><div><p className="eyebrow">IN PRODUCTION</p><h2>Active productions.</h2></div></div>
          <div className="dashboard-project-grid">{activeProjects.map(projectCard)}</div>
        </section>
      )}

      {!busy && !error && recentFinished.length > 0 && (
        <section className="dashboard-projects" aria-label="Recent and finished">
          <div className="section-heading"><div><p className="eyebrow">RECENT WORK</p><h2>Back to the edit.</h2></div><Link className="text-link" href="/projects">View all projects <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div>
          <div className="dashboard-project-grid">{recentFinished.map(projectCard)}</div>
        </section>
      )}

      <footer className="app-footer"><span>CINEMORA / STUDIO</span><span>Project information is loaded from your Cinemora API.</span></footer>
    </div>
  </main>;
}
