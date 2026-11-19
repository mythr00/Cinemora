"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { apiUrl, listProjects, type ProjectSummary } from "@/lib/api";

type Project = ProjectSummary;

export default function ProjectsPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const load = useCallback(async (signal?: AbortSignal) => {
    setError("");
    try {
      const data = await listProjects(signal);
      setProjects(data.projects || []);
    } catch (cause) {
      if (!signal?.aborted) setError(cause instanceof Error ? cause.message : "Projects could not be loaded.");
    } finally { if (!signal?.aborted) setBusy(false); }
  }, []);
  useEffect(() => { const controller = new AbortController(); const timer = setTimeout(() => { void load(controller.signal); }, 0); return () => { clearTimeout(timer); controller.abort(); }; }, [load]);

  return <main className="app-page projects-page">
    <AppNavigation active="/projects" />
    <section className="projects-content">
      <div className="projects-heading"><div><p className="eyebrow">PROJECT LIBRARY</p><h1>Every cut, in its place.</h1><p className="projects-intro">Your productions, their real status, and finished exports.</p></div><Link className="button button-primary" href="/create">New production <span aria-hidden="true">↗</span></Link></div>
      <div className="library-toolbar"><span>{busy ? "Loading projects…" : `${projects.length} ${projects.length === 1 ? "project" : "projects"}`}</span><span>Newest first</span></div>
      {busy && <div className="library-state" role="status">Loading your project library…</div>}
      {error && <div className="library-error" role="alert"><p>{error}</p><button type="button" onClick={() => { setBusy(true); void load(); }}>Try again</button></div>}
      {!busy && !error && projects.length === 0 && <div className="library-empty"><div className="empty-reel" aria-hidden="true"><span /><span /><span /></div><p className="eyebrow">THE FIRST PAGE IS BLANK</p><h2>Make room for a story.</h2><p>Start a production with your script. Once created, it will show its actual status and export here.</p><Link className="button button-primary" href="/create">Create a project <span aria-hidden="true">↗</span></Link></div>}
      {!busy && !error && projects.length > 0 && <div className="library-grid">{projects.map((project) => <article className="library-card" key={project.id}>
        <Link className={`library-preview ${project.video_url ? "has-video" : ""}`} href={`/projects/${project.id}`} aria-label={`Open project: ${project.script_preview || project.category}`}>
          {project.video_url ? <video src={apiUrl(project.video_url)} preload="metadata" muted playsInline /> : <span className="preview-placeholder"><span className="reel-glyph" /><small>NO EXPORT YET</small></span>}
          <span className={`project-state project-state-${project.status}`}>{project.status.replaceAll("_", " ")}</span>
        </Link>
        <div className="library-card-content"><div className="library-meta"><span>{project.category}</span><time dateTime={project.created_at}>{new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(new Date(project.created_at))}</time></div><h2>{project.title || project.script_preview || "Untitled production"}</h2><p>{project.current_stage ? `Stage · ${project.current_stage.replaceAll("_", " ")}` : project.video_url ? "Finished export available" : "Project details and production status"}</p><div className="library-actions">{project.video_url && <a className="button button-secondary" href={apiUrl(project.video_url)}>Download film</a>}<Link className="button button-text" href={`/projects/${project.id}`}>Open project <span aria-hidden="true">↗</span></Link></div></div>
      </article>)}</div>}
    </section>
    <footer className="app-footer"><span>CINEMORA / PROJECT LIBRARY</span><span>Project records are supplied by the connected API.</span></footer>
  </main>;
}
