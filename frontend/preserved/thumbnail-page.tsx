/**
 * PRESERVED — quarantined in Brief Stage 3 (C-02), 2026-11-20.
 *
 * This is the original Thumbnail studio (thumbnail generation) implementation,
 * kept verbatim for reference. It is NOT routed, imported, or
 * rendered anywhere in the active application: every backend
 * endpoint it calls is absent from the committed backend
 * (all 23 legacy endpoints re-verified 404 under TestClient
 * at HEAD bfc5a06). The active route shows an honest
 * unavailable state instead. Do not resurrect this file
 * without a working backend for it, and do not delete it —
 * removal decisions belong to the founder (register C-14).
 */
"use client";

import { useEffect, useState, type FormEvent } from "react";
import Image from "next/image";
import { AppNavigation } from "@/components/app-navigation";
import { apiUrl, legacyRequest, listProjects, type ProjectSummary } from "@/lib/api";

type Project = Pick<ProjectSummary, "id" | "category" | "script_preview">;
type Thumbnail = { id: string; title: string; width: number; height: number; url: string; project_id?: string | null; prompt?: string; created_at?: string };

export default function ThumbnailPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [variations, setVariations] = useState(2);
  const [results, setResults] = useState<Thumbnail[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    void listProjects(controller.signal)
      .then((data) => { setProjects(data.projects || []); })
      .catch((error: unknown) => { if (!controller.signal.aborted) setMessage(error instanceof Error ? error.message : "Projects could not be loaded."); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    void legacyRequest<{ thumbnails?: Thumbnail[] }>(`/thumbnails${query}`, { signal: controller.signal })
      .then((data) => { setResults(data.thumbnails || []); })
      .catch((error: unknown) => { if (!controller.signal.aborted) setMessage(error instanceof Error ? error.message : "Saved thumbnails could not be loaded."); });
    return () => controller.abort();
  }, [projectId]);

  async function generate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setResults([]); setMessage("Generating and validating thumbnail images…");
    try {
      const data = await legacyRequest<{ thumbnails?: Thumbnail[] }>("/thumbnails/generate", {
        method: "POST",
        body: JSON.stringify({ title, prompt, project_id: projectId || null, variations }),
      });
      if (!data.thumbnails?.length) throw new Error("The image provider returned no validated thumbnail files.");
      setResults((previous) => [...data.thumbnails!, ...previous]); setMessage(`${data.thumbnails.length} thumbnail${data.thumbnails.length === 1 ? "" : "s"} generated and saved.`);
    } catch (error) { setMessage(error instanceof Error ? error.message : "Thumbnail generation failed."); }
    finally { setBusy(false); }
  }

  return <main className="app-page thumbnail-page">
    <AppNavigation active="/thumbnail" />
    <section className="thumbnail-content">
      <p className="eyebrow">IMAGE WORKSPACE</p><h1>Thumbnail</h1>
      <p className="thumbnail-intro">Create and save a documentary cover image whenever you need one. Video rendering is not required. Project context is optional.</p>
      <form className="thumbnail-form" onSubmit={(event) => void generate(event)}>
        <label>Headline or title<input value={title} onChange={(event) => setTitle(event.target.value)} maxLength={140} required placeholder="A concise, compelling headline" /></label>
        <label>Visual direction<textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} maxLength={1800} required placeholder="Describe the scene, subject, mood, lighting, and composition." /></label>
        <div className="thumbnail-form-row">
          <label>Project context <select value={projectId} onChange={(event) => setProjectId(event.target.value)}><option value="">No project context</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.script_preview || project.category} · {project.id.slice(0, 8)}</option>)}</select><small>Uses the saved script for subject accuracy.</small></label>
          <label>Variations <select value={variations} onChange={(event) => setVariations(Number(event.target.value))}><option value={1}>1 image</option><option value={2}>2 images</option><option value={3}>3 images</option><option value={4}>4 images</option></select></label>
        </div>
        <button className="button button-primary" disabled={busy || !title.trim() || !prompt.trim()}>{busy ? "Generating…" : results.length ? "Generate new variations" : "Generate thumbnail"}</button>
      </form>
      <p className="thumbnail-status" role="status" aria-live="polite">{message}</p>
      {results.length > 0 && <section className="thumbnail-results" aria-label="Generated thumbnails">{results.map((image) => <article className="thumbnail-result" key={image.id}>
        <Image src={apiUrl(image.url)} alt={`Generated thumbnail: ${image.title}`} width={1280} height={720} unoptimized />
        <div><strong>{image.title}</strong><span>{image.width} × {image.height}{image.project_id ? ` · ${image.project_id.slice(0, 8)}` : " · standalone"}</span><a className="button button-secondary" href={apiUrl(image.url)} download={`cinemora-thumbnail-${image.id}.jpg`}>Download image</a></div>
      </article>)}</section>}
      <p className="thumbnail-note">Generated images are stored as real files. Review factual details and image rights before publishing.</p>
    </section>
  </main>;
}
