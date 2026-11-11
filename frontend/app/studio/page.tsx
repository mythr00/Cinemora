"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";

const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
type Project = { id: string; category: string; status: string; current_stage?: string | null; created_at: string; script_preview: string; video_url?: string | null };

function label(value: string) { return value.replaceAll("_", " "); }

export default function StudioPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async (signal?: AbortSignal) => {
    setError("");
    try {
      const response = await fetch(`${API_URL}/projects`, { cache: "no-store", signal });
      const data = await response.json() as { projects?: Project[]; detail?: string };
      if (!response.ok) throw new Error(data.detail || "Your projects could not be loaded.");
      setProjects(data.projects || []);
    } catch (cause) {
      if (!signal?.aborted) setError(cause instanceof Error ? cause.message : "Your projects could not be loaded.");
    } finally { if (!signal?.aborted) setBusy(false); }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => { void load(controller.signal); }, 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [load]);

  const activeCount = projects.filter((project) => ["queued", "running", "pipeline_queued"].includes(project.status)).length;
  const completeCount = projects.filter((project) => project.status === "completed").length;
  const recent = projects.slice(0, 4);

  return <main className="app-page studio-dashboard">
    <AppNavigation active="/studio" />
    <div className="dashboard-content">
      <section className="dashboard-welcome">
        <div><p className="eyebrow">YOUR PRODUCTION DESK</p><h1>Good stories deserve<br /><em>a considered cut.</em></h1><p>Start with a script, follow each production as it moves, and return to the work whenever you’re ready.</p></div>
        <Link className="button button-primary" href="/create">Start a production <span aria-hidden="true">↗</span></Link>
      </section>

      <section className="dashboard-metrics" aria-label="Project overview">
        <div><span>ALL PROJECTS</span><strong>{busy ? "—" : projects.length}</strong></div>
        <div><span>IN PRODUCTION</span><strong>{busy ? "—" : activeCount}</strong></div>
        <div><span>FINISHED EXPORTS</span><strong>{busy ? "—" : completeCount}</strong></div>
        <p>Counts reflect the live project list.</p>
      </section>

      <section className="dashboard-projects">
        <div className="section-heading"><div><p className="eyebrow">RECENT WORK</p><h2>Back to the edit.</h2></div><Link className="text-link" href="/projects">View all projects <span aria-hidden="true">↗</span></Link></div>
        {busy && <div className="dashboard-loading" role="status">Loading your project library…</div>}
        {error && <div className="dashboard-error" role="alert"><span>{error}</span><button type="button" onClick={() => { setBusy(true); void load(); }}>Try again</button></div>}
        {!busy && !error && recent.length === 0 && <div className="dashboard-empty"><div className="empty-reel" aria-hidden="true"><span /><span /><span /></div><p className="eyebrow">A CLEAR FIRST FRAME</p><h3>Your next film starts here.</h3><p>Create a project from a script. Its real production state and export will appear in this space.</p><Link className="button button-primary" href="/create">Create your first film <span aria-hidden="true">↗</span></Link></div>}
        {!busy && !error && recent.length > 0 && <div className="dashboard-project-grid">{recent.map((project) => <article className="dashboard-project-card" key={project.id}>
          <Link className={`project-art ${project.video_url ? "has-video" : ""}`} href={`/projects/${project.id}`} aria-label={`Open ${project.category} project`}>
            {project.video_url ? <video src={`${API_URL}${project.video_url}`} preload="metadata" muted playsInline /> : <span className="project-art-mark"><span className="reel-glyph" /></span>}
            <span className={`project-state project-state-${project.status}`}>{label(project.status)}</span>
          </Link>
          <div className="dashboard-project-info"><div><p>{project.category}</p><time dateTime={project.created_at}>{new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(new Date(project.created_at))}</time></div><h3>{project.script_preview || "Untitled production"}</h3><p className="project-stage">{project.current_stage ? `Stage · ${label(project.current_stage)}` : project.video_url ? "Export available" : "Production status available"}</p><Link href={`/projects/${project.id}`}>Open project <span aria-hidden="true">↗</span></Link></div>
        </article>)}</div>}
      </section>

      <section className="dashboard-tools"><div><p className="eyebrow">AT YOUR DISPOSAL</p><h2>Small tools for<br />the larger story.</h2></div><div className="tool-links"><Link href="/channel-lab"><span className="tool-index">01</span><span><strong>Script</strong><small>Research a fresh topic and develop an original script.</small></span><span aria-hidden="true">↗</span></Link><Link href="/thumbnail"><span className="tool-index">02</span><span><strong>Thumbnail</strong><small>Generate a standalone documentary cover image.</small></span><span aria-hidden="true">↗</span></Link><Link href="/viral-clips"><span className="tool-index">03</span><span><strong>Short-clip extraction</strong><small>Work from an accessible YouTube link or your own video file.</small></span><span aria-hidden="true">↗</span></Link></div></section>
      <footer className="app-footer"><span>CINEMORA / STUDIO</span><span>Project information is loaded from your Cinemora API.</span></footer>
    </div>
  </main>;
}
