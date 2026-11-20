"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { StatusBadge } from "@/components/status-badge";
import { EmptyState, ErrorState, LoadingState } from "@/components/states";
import { listProjects, type ProjectSummary } from "@/lib/api";
import { AuthenticatedVideo } from "@/components/authenticated-media";
import { ArrowUpRight } from "lucide-react";

type Project = ProjectSummary;

function label(value: string) { return value.replaceAll("_", " "); }

export default function StudioPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setError(null);
    try {
      const data = await listProjects(signal);
      setProjects(data.projects || []);
    } catch (cause) {
      if (!signal?.aborted) setError(cause);
    } finally { if (!signal?.aborted) setBusy(false); }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => { void load(controller.signal); }, 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [load]);

  const activeCount = projects.filter((project) => ["queued", "running", "pipeline_queued", "awaiting_approval"].includes(project.status)).length;
  const completeCount = projects.filter((project) => project.status === "completed").length;
  const recent = projects.slice(0, 4);
  // Brief Stage 5 (C-07): the approval queue. A plan-gated
  // production parks in awaiting_approval until the operator
  // approves it — previously it was silently folded into "IN
  // PRODUCTION" and nothing said it was waiting on the user.
  const awaitingApproval = projects.filter(
    (project) => project.status === "awaiting_approval" || project.latest_run?.status === "awaiting_approval",
  );

  return <main className="app-page studio-dashboard">
    <AppNavigation />
    <div className="dashboard-content">
      <section className="dashboard-welcome">
        <div><p className="eyebrow">YOUR PRODUCTION DESK</p><h1>Good stories deserve<br /><em>a considered cut.</em></h1><p>Start with a script, follow each production as it moves, and return to the work whenever you’re ready.</p></div>
        <Link className="button button-primary" href="/create">Start a production <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>
      </section>

      <section className="dashboard-metrics" aria-label="Project overview">
        <div><span>ALL PROJECTS</span><strong>{busy ? "—" : projects.length}</strong></div>
        <div><span>IN PRODUCTION</span><strong>{busy ? "—" : activeCount}</strong></div>
        <div><span>FINISHED EXPORTS</span><strong>{busy ? "—" : completeCount}</strong></div>
        <p>Counts reflect the live project list.</p>
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

      <section className="dashboard-projects">
        <div className="section-heading"><div><p className="eyebrow">RECENT WORK</p><h2>Back to the edit.</h2></div><Link className="text-link" href="/projects">View all projects <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div>
        {busy && <LoadingState className="dashboard-loading" label="Loading your project library…" />}
        {!!error && <ErrorState className="dashboard-error" error={error} onRetry={() => { setBusy(true); void load(); }} />}
        {!busy && !error && recent.length === 0 && <EmptyState className="dashboard-empty" eyebrow="A CLEAR FIRST FRAME" title="Your next film starts here." description="Create a project from a script. Its real production state and export will appear in this space." action={<Link className="button button-primary" href="/create">Create your first film <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>} />}
        {!busy && !error && recent.length > 0 && <div className="dashboard-project-grid">{recent.map((project) => <article className="dashboard-project-card" key={project.id}>
          <Link className={`project-art ${project.video_url ? "has-video" : ""}`} href={`/projects/${project.id}`} aria-label={`Open ${project.category} project`}>
            {project.video_url ? <AuthenticatedVideo src={project.video_url} preload="metadata" muted playsInline /> : <span className="project-art-mark"><span className="reel-glyph" /></span>}
            <StatusBadge status={project.status} overlay />
          </Link>
          <div className="dashboard-project-info"><div><p>{project.category}</p><time dateTime={project.created_at}>{new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(new Date(project.created_at))}</time></div><h3>{project.title || project.script_preview || "Untitled production"}</h3><p className="project-stage">{project.current_stage ? `Stage · ${label(project.current_stage)}` : project.video_url ? "Export available" : "Production status available"}</p><Link href={`/projects/${project.id}`}>Open project <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div>
        </article>)}</div>}
      </section>

      <footer className="app-footer"><span>CINEMORA / STUDIO</span><span>Project information is loaded from your Cinemora API.</span></footer>
    </div>
  </main>;
}
