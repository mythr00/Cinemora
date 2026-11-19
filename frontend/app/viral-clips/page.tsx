"use client";

import { useState, type FormEvent } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { apiUrl, legacyRequest } from "@/lib/api";
import { ArrowUpRight, Download } from "lucide-react";

type Clip = { clip: number; start_seconds: number; end_seconds: number; duration_seconds?: number; potential_score: number; transcript: string; file: string };
function timestamp(seconds: number) { const m = Math.floor(seconds / 60); const s = Math.floor(seconds % 60); return `${m}:${String(s).padStart(2, "0")}`; }

export default function ViralClipsPage() {
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [clips, setClips] = useState<Clip[]>([]);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState(false);

  async function run(mode: "url" | "upload", event?: FormEvent) {
    event?.preventDefault();
    if (mode === "url" && !url.trim()) { setError(true); setMessage("Paste a YouTube video link first."); return; }
    if (mode === "upload" && !file) { setError(true); setMessage("Choose a video file first."); return; }
    setBusy(true); setError(false); setMessage("Analyzing the video and selecting short moments…"); setClips([]);
    try {
      type ClipsResult = { clips?: Clip[]; video_title?: string; clips_created?: number };
      let data: ClipsResult;
      if (mode === "url") data = await legacyRequest<ClipsResult>("/viral-clips", { method: "POST", body: JSON.stringify({ youtube_url: url.trim() }) });
      else { const form = new FormData(); form.append("file", file!); data = await legacyRequest<ClipsResult>("/viral-clips/upload", { method: "POST", body: form }); }
      setClips(data.clips || []); setTitle(data.video_title || file?.name || "Your video"); setMessage(`${data.clips_created || data.clips?.length || 0} clips are ready to review.`);
    } catch (cause) { setError(true); setMessage(cause instanceof Error ? cause.message : "Video processing failed."); }
    finally { setBusy(false); }
  }

  return <main className="app-page short-page">
    <AppNavigation active="/viral-clips" />
    <div className="tool-page-content">
      <section className="tool-page-heading"><p className="eyebrow">SHORT-FORM WORKFLOW</p><h1>Find the moment.<br /><em>Cut it with intent.</em></h1><p>Extract candidate highlights from a YouTube link or a video file you’re allowed to use. Review each moment before downloading.</p></section>
      <section className="short-input-grid" aria-label="Choose a video source">
        <form className="short-source-panel" onSubmit={(event) => void run("url", event)}><span className="source-step">01 / VIDEO SOURCE</span><h2>Use a YouTube video</h2><p>Paste an accessible link. Availability does not grant reuse rights.</p><label htmlFor="youtube-url">Video URL</label><input id="youtube-url" type="url" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://www.youtube.com/watch?v=…" required/><button className="button button-primary" type="submit" disabled={busy}>{busy ? "Analyzing…" : "Find short moments"}<ArrowUpRight size={14} strokeWidth={2} aria-hidden /></button></form>
        <form className="short-source-panel" onSubmit={(event) => void run("upload", event)}><span className="source-step">02 / YOUR FOOTAGE</span><h2>Upload a video</h2><p>Use an MP4, MOV, MKV, WebM, or AVI file from your device.</p><label htmlFor="clip-upload">Video file</label><input id="clip-upload" type="file" accept="video/mp4,video/quicktime,video/x-matroska,video/webm,video/x-msvideo" onChange={(event) => setFile(event.target.files?.[0] || null)} required/><small className="file-selection">{file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB` : "No file selected"}</small><button className="button button-secondary" type="submit" disabled={busy || !file}>{busy ? "Analyzing…" : "Analyze my video"}<ArrowUpRight size={14} strokeWidth={2} aria-hidden /></button></form>
      </section>
      {message && <p className={`tool-status ${error ? "is-error" : ""}`} role={error ? "alert" : "status"} aria-live="polite"><span className={busy ? "status-pulse" : ""} />{message}</p>}
      {clips.length > 0 && <section className="short-results"><div className="section-heading"><div><p className="eyebrow">REVIEW BEFORE EXPORT</p><h2>{title}</h2></div><span>{clips.length} clips</span></div><div className="short-clip-grid">{clips.map((clip) => { const videoUrl = apiUrl(clip.file); return <article className="short-clip-card" key={clip.clip}><div className="short-clip-heading"><span>CLIP {String(clip.clip).padStart(2, "0")}</span><span>{timestamp(clip.start_seconds)} – {timestamp(clip.end_seconds)}</span></div><video controls playsInline preload="metadata" src={videoUrl} aria-label={`Preview clip ${clip.clip}`} /><div className="short-clip-copy"><p>{clip.transcript || "No transcript was returned for this clip."}</p><span>Candidate score · {clip.potential_score}</span><a className="button button-secondary" href={videoUrl} download={`cinemora-clip-${clip.clip}.mp4`}>Download MP4 <Download size={14} strokeWidth={2} aria-hidden /></a></div></article>; })}</div></section>}
      <footer className="app-footer"><span>CINEMORA / SHORT CLIPS</span><span>Review rights and source permissions before publishing.</span></footer>
    </div>
  </main>;
}
