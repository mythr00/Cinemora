"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { PROJECT_STATUS_OPTIONS, StatusBadge } from "@/components/status-badge";
import { EmptyState, ErrorState, LoadingState } from "@/components/states";
import { listProjects, type ProjectSummary } from "@/lib/api";
import { AuthenticatedDownloadLink, AuthenticatedVideo } from "@/components/authenticated-media";
import { ArrowUpRight } from "lucide-react";

type Project = ProjectSummary;

export default function ProjectsPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);
  // IA-12: status filter over the already-fetched list, using
  // the shared status vocabulary (status-badge.tsx).
  const [statusFilter, setStatusFilter] = useState("all");
  const visibleProjects = statusFilter === "all" ? projects : projects.filter((project) => project.status === statusFilter);
  const load = useCallback(async (signal?: AbortSignal) => {
    setError(null);
    try {
      const data = await listProjects(signal);
      setProjects(data.projects || []);
    } catch (cause) {
      if (!signal?.aborted) setError(cause);
    } finally { if (!signal?.aborted) setBusy(false); }
  }, []);
  useEffect(() => { const controller = new AbortController(); const timer = setTimeout(() => { void load(controller.signal); }, 0); return () => { clearTimeout(timer); controller.abort(); }; }, [load]);

  return <main className="app-page projects-page">
    <AppNavigation />
    <section className="projects-content">
      <div className="projects-heading"><div><p className="eyebrow">PROJECT LIBRARY</p><h1>Every cut, in its place.</h1><p className="projects-intro">Your productions, their real status, and finished exports.</p></div><Link className="button button-primary" href="/create">New production <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div>
      <div className="library-toolbar"><span>{busy ? "Loading projects…" : `${visibleProjects.length} ${visibleProjects.length === 1 ? "project" : "projects"}${statusFilter !== "all" ? ` of ${projects.length}` : ""}`}</span><label className="library-filter"><span>Status</span><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} aria-label="Filter projects by status"><option value="all">All statuses</option>{PROJECT_STATUS_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label><span>Newest first</span></div>
      {busy && <LoadingState className="library-state" label="Loading your project library…" />}
      {!!error && <ErrorState className="library-error" error={error} onRetry={() => { setBusy(true); void load(); }} />}
      {!busy && !error && projects.length === 0 && <EmptyState className="library-empty" eyebrow="THE FIRST PAGE IS BLANK" title="Make room for a story." titleLevel="h2" description="Start a production with your script. Once created, it will show its actual status and export here." action={<Link className="button button-primary" href="/create">Create a project <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>} />}
      {!busy && !error && projects.length > 0 && visibleProjects.length === 0 && <p className="library-state">No projects have this status.</p>}
      {!busy && !error && visibleProjects.length > 0 && <div className="library-grid">{visibleProjects.map((project) => <article className="library-card" key={project.id}>
        <Link className={`library-preview ${project.video_url ? "has-video" : ""}`} href={`/projects/${project.id}`} aria-label={`Open project: ${project.script_preview || project.category}`}>
          {project.video_url ? <AuthenticatedVideo src={project.video_url} preload="metadata" muted playsInline /> : <span className="preview-placeholder"><span className="reel-glyph" /><small>NO EXPORT YET</small></span>}
          <StatusBadge status={project.status} overlay />
        </Link>
        <div className="library-card-content"><div className="library-meta"><span>{project.category}</span><time dateTime={project.created_at}>{new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(new Date(project.created_at))}</time></div><h2>{project.title || project.script_preview || "Untitled production"}</h2><p>{project.current_stage ? `Stage · ${project.current_stage.replaceAll("_", " ")}` : project.video_url ? "Finished export available" : "Project details and production status"}</p><div className="library-actions">{project.video_url && <AuthenticatedDownloadLink className="button button-secondary" path={project.video_url} filename="documentary_final_output.mp4">Download film</AuthenticatedDownloadLink>}<Link className="button button-text" href={`/projects/${project.id}`}>Open project <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div></div>
      </article>)}</div>}
    </section>
    <footer className="app-footer"><span>CINEMORA / PROJECT LIBRARY</span><span>Project records are supplied by the connected API.</span></footer>
  </main>;
}
