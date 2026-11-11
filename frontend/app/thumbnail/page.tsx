"use client";

import { useEffect, useState, type FormEvent } from "react";
import Image from "next/image";
import { AppNavigation } from "@/components/app-navigation";

const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
type Project = { id: string; category: string; script_preview: string };
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
    void fetch(`${API_URL}/projects`, { cache: "no-store", signal: controller.signal })
      .then(async (response) => {
        const data = await response.json() as { projects?: Project[]; detail?: string };
        if (!response.ok) throw new Error(data.detail || "Projects could not be loaded.");
        setProjects(data.projects || []);
      })
      .catch((error: unknown) => { if (!controller.signal.aborted) setMessage(error instanceof Error ? error.message : "Projects could not be loaded."); });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    void fetch(`${API_URL}/thumbnails${query}`, { cache: "no-store", signal: controller.signal })
      .then(async (response) => {
        const data = await response.json() as { thumbnails?: Thumbnail[]; detail?: string };
        if (!response.ok) throw new Error(data.detail || "Saved thumbnails could not be loaded.");
        setResults(data.thumbnails || []);
      })
      .catch((error: unknown) => { if (!controller.signal.aborted) setMessage(error instanceof Error ? error.message : "Saved thumbnails could not be loaded."); });
    return () => controller.abort();
  }, [projectId]);

  async function generate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setResults([]); setMessage("Generating and validating thumbnail images…");
    try {
      const response = await fetch(`${API_URL}/thumbnails/generate`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, prompt, project_id: projectId || null, variations }),
      });
      const data = await response.json() as { thumbnails?: Thumbnail[]; detail?: string };
      if (!response.ok) throw new Error(data.detail || "Thumbnail generation failed.");
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
        <Image src={`${API_URL}${image.url}`} alt={`Generated thumbnail: ${image.title}`} width={1280} height={720} unoptimized />
        <div><strong>{image.title}</strong><span>{image.width} × {image.height}{image.project_id ? ` · ${image.project_id.slice(0, 8)}` : " · standalone"}</span><a className="button button-secondary" href={`${API_URL}${image.url}?download=true`} download={`cinemora-thumbnail-${image.id}.jpg`}>Download image</a></div>
      </article>)}</section>}
      <p className="thumbnail-note">Generated images are stored as real files. Review factual details and image rights before publishing.</p>
    </section>
  </main>;
}
