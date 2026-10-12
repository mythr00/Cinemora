"use client";

import Image from "next/image";
import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";

const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
const categories = [
  "Documentary",
  "History",
  "Science",
  "Biography",
  "True Crime",
  "Naval History",
  "War History",
  "Finance",
  "Custom",
];

const stageOrder = [
  ["voice_actor", "Narration"],
  ["script_analysis", "Story analysis"],
  ["asset_manager", "Visual research"],
  ["sound_designer", "Sound mix"],
  ["motion_designer", "Motion design"],
  ["timeline_builder", "Timeline"],
  ["renderer", "Render"],
  ["video_editor", "Final edit"],
] as const;

type MusicTrack = {
  id: string;
  title: string;
  mood: string;
  preview_url: string;
};

type ProjectStatus = {
  status: string;
  current_stage?: string | null;
  error?: string | null;
  video_url?: string | null;
  stages?: Record<string, { status?: string; error?: string | null }>;
};

export default function Home() {
  const [script, setScript] = useState("");
  const [category, setCategory] = useState("Documentary");
  const [loading, setLoading] = useState(false);
  const [projectId, setProjectId] = useState("");
  const [jobId, setJobId] = useState("");
  const [status, setStatus] = useState<ProjectStatus | null>(null);
  const [message, setMessage] = useState("");
  const [apiOnline, setApiOnline] = useState<boolean | null>(null);
  const [musicTracks, setMusicTracks] = useState<MusicTrack[]>([]);
  const [selectedMusic, setSelectedMusic] = useState("auto");
  const [musicLoadError, setMusicLoadError] = useState("");

  const words = useMemo(() => script.trim().split(/\s+/).filter(Boolean).length, [script]);
  const duration = words ? Math.max(1, Math.round(words / 145)) : 0;
  const isTerminal = status?.status === "completed" || status?.status === "failed";

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_URL}/health`, { signal: controller.signal })
      .then((response) => setApiOnline(response.ok))
      .catch(() => setApiOnline(false));
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_URL}/music`, { signal: controller.signal })
      .then(async (response) => {
        const data = await response.json() as { detail?: string; tracks?: MusicTrack[] };
        if (!response.ok) throw new Error(data.detail || "Could not load music tracks.");
        setMusicTracks(data.tracks || []);
      })
      .catch(() => {
        if (!controller.signal.aborted) setMusicLoadError("Music previews are unavailable. The generated instrumental will be used.");
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!projectId || isTerminal) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;

    async function pollStatus() {
      try {
        const response = await fetch(`${API_URL}/projects/${projectId}/status`, { cache: "no-store" });
        const data = (await response.json()) as ProjectStatus;
        if (!response.ok) throw new Error((data as ProjectStatus & { detail?: string }).detail || "Could not read project status.");
        if (stopped) return;
        setStatus(data);
        if (data.status === "completed" && data.video_url) {
          setMessage("Your documentary is ready to review.");
        } else if (data.status === "failed") {
          setMessage(data.error || "Production stopped before completion.");
        } else {
          setMessage(data.current_stage ? `${stageLabel(data.current_stage)} in progress` : "Waiting for a worker to start production");
        }
      } catch (error) {
        if (!stopped) setMessage(error instanceof Error ? error.message : "Could not read project status.");
      }
      if (!stopped) timer = setTimeout(pollStatus, 3000);
    }

    void pollStatus();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [projectId, isTerminal]);

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!script.trim()) {
      setMessage("Add a script before starting production.");
      return;
    }

    setLoading(true);
    setProjectId("");
    setJobId("");
    setStatus(null);
    setMessage("Creating your project…");

    try {
      const createResponse = await fetch(`${API_URL}/projects`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ script: script.trim(), category }),
      });
      const created = await createResponse.json();
      if (!createResponse.ok) throw new Error(created.detail || "Could not create the project.");

      setProjectId(created.id);
      setMessage("Project created. Adding it to the production queue…");
      const startResponse = await fetch(`${API_URL}/projects/${created.id}/analyze?music_track_id=${encodeURIComponent(selectedMusic)}`, { method: "POST" });
      const started = await startResponse.json();
      if (!startResponse.ok) throw new Error(started.detail || "The project was created, but production could not be started.");

      setJobId(started.job_id || "");
      setMessage("Project created and added to the production queue.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not start documentary production.");
    } finally {
      setLoading(false);
    }
  }

  const videoUrl = status?.video_url ? new URL(status.video_url, `${API_URL}/`).toString() : "";

  return (
    <main className="studio-shell">
      <aside className="sidebar">
        <a className="brand" href="#create" aria-label="Cinemora home">
          <Image className="brand-mark-image" src="/cinemora-mark.svg" alt="" width={34} height={34} priority />
          <span className="brand-copy"><strong>CINEMORA</strong><span>DOCUMENTARY STUDIO</span></span>
        </a>

        <div className="side-label">WORKSPACE</div>
        <nav className="side-nav" aria-label="Main navigation">
          <a className="nav-item active" href="#create"><Icon name="spark" /><span>Create</span><span className="nav-active-dot" /></a>
          <button className="nav-item upcoming" type="button" disabled><Icon name="grid" /><span>Projects</span><small>Next</small></button>
          <button className="nav-item upcoming" type="button" disabled><Icon name="image" /><span>Thumbnail lab</span><small>Planned</small></button>
          <button className="nav-item upcoming" type="button" disabled><Icon name="layers" /><span>Backgrounds</span><small>Planned</small></button>
        </nav>

        <div className="sidebar-spacer" />
        <div className="sidebar-note">
          <span className="note-icon"><Icon name="wand" /></span>
          <div><strong>Production tools</strong><p>Script, research, narration, and edit.</p></div>
        </div>
        <div className="connection"><span className={`connection-dot ${apiOnline === false ? "offline" : ""}`} /><span>{apiOnline === null ? "Connecting to studio" : apiOnline ? "Studio API connected" : "Studio API unavailable"}</span></div>
        <div className="sidebar-footer"><span className="avatar"><Image src="/cinemora-mark.svg" alt="" width={20} height={20} /></span><span><strong>Cinemora</strong><small>Production workspace</small></span></div>
      </aside>

      <section className="workspace" id="create">
        <header className="topbar">
          <div className="breadcrumbs"><span>Cinemora</span><span className="crumb-separator">/</span><strong>New documentary</strong></div>
          <div className="topbar-right"><span className="draft-status"><span /> New project</span><span className="edition-label">STUDIO EDITION</span></div>
        </header>

        <div className="content-wrap">
          <div className="page-heading">
          <div><div className="eyebrow">DOCUMENTARY STUDIO <span className="eyebrow-line" /></div><h1>New documentary</h1><p>Script, visual research, narration, and final edit.</p></div>
          </div>

          <form className="create-layout" onSubmit={createProject}>
            <div className="main-column">
              <section className="panel script-panel" aria-labelledby="script-title">
                <div className="panel-heading">
                  <div className="step-marker">01</div>
                  <div className="panel-title"><h2 id="script-title">Script</h2><p>Paste the narration you want produced.</p></div>
                  <span className="required-tag">{words.toLocaleString()} / 50,000</span>
                </div>
                <label className="sr-only" htmlFor="documentary-script">Documentary script</label>
                <textarea id="documentary-script" value={script} onChange={(event) => setScript(event.target.value)} maxLength={50000} placeholder="Paste script…" />
                <div className="editor-footer"><span className="editor-hint">{words.toLocaleString()} words</span><span className="count">{script.length.toLocaleString()} / 50,000 characters</span></div>
              </section>

              <section className="panel details-panel">
                <div className="panel-heading compact-heading">
                  <div className="step-marker">02</div>
                  <div className="panel-title"><h2>Project details</h2><p>Choose a category and check the estimated runtime.</p></div>
                </div>
                <div className="form-fields">
                  <label className="field-label" htmlFor="category">STORY CATEGORY
                    <span className="select-wrap"><select id="category" value={category} onChange={(event) => setCategory(event.target.value)}>{categories.map((item) => <option key={item}>{item}</option>)}</select><Icon name="chevron" /></span>
                  </label>
                  <div className="field-label length-field"><span>ESTIMATED RUNTIME</span><div className="runtime-value">{duration ? `~${duration} min` : "Add a script"}</div><small>Based on about 145 spoken words per minute.</small></div>
                </div>
                <div className="workflow-includes"><span className="include-label">INCLUDED IN YOUR WORKFLOW</span><div className="include-chips"><span><Icon name="search" /> Story research</span><span><Icon name="film" /> Matched visuals</span><span><Icon name="mic" /> Narration + captions</span></div></div>
              </section>

              <section className="panel music-panel" aria-labelledby="music-title">
                <div className="panel-heading compact-heading">
                  <div className="step-marker">03</div>
                  <div className="panel-title"><h2 id="music-title">Background music</h2><p>Choose a track; narration stays in front of the music.</p></div>
                </div>
                <div className="music-grid">
                  <div className={`music-card ${selectedMusic === "auto" ? "selected" : ""}`}>
                    <label className="music-card-choice">
                      <input type="radio" name="music-track" value="auto" checked={selectedMusic === "auto"} onChange={() => setSelectedMusic("auto")} />
                      <span><strong>Cinemora ambient</strong><small>Generated instrumental · automatic ducking</small></span>
                    </label>
                    <div className="music-auto-note">A restrained instrumental bed is created for this project.</div>
                  </div>
                  {musicTracks.map((track) => (
                    <div className={`music-card ${selectedMusic === track.id ? "selected" : ""}`} key={track.id}>
                      <label className="music-card-choice">
                        <input type="radio" name="music-track" value={track.id} checked={selectedMusic === track.id} onChange={() => setSelectedMusic(track.id)} />
                        <span><strong>{track.title}</strong><small>{track.mood}</small></span>
                      </label>
                      <audio controls preload="none" src={`${API_URL}${track.preview_url}`} aria-label={`Preview ${track.title}`} />
                    </div>
                  ))}
                </div>
                <p className="music-license-note">Use tracks only when your download license permits your intended video use. Keep the original license or receipt.</p>
                {musicLoadError && <p className="music-error" role="status">{musicLoadError}</p>}
              </section>

              <div className="future-tools" aria-label="Planned tools">
                <div className="future-heading"><span className="eyebrow">NOT AVAILABLE YET</span><span className="roadmap-line" /></div>
                <div className="planned-tools">
                  <div><strong>Scene backgrounds</strong><span>Selection is not connected to the renderer.</span></div>
                  <div><strong>Thumbnail generation</strong><span>No generation endpoint is configured.</span></div>
                </div>
              </div>
            </div>

            <aside className="right-column">
              <section className="summary-card">
                <div className="summary-top"><div><span className="eyebrow">PROJECT</span><h2>Summary</h2></div></div>
                <div className="summary-rows"><div><span>Category</span><strong>{category}</strong></div><div><span>Music</span><strong>{selectedMusic === "auto" ? "Cinemora ambient" : musicTracks.find((track) => track.id === selectedMusic)?.title || "Loading track"}</strong></div><div><span>Script length</span><strong>{words ? `${words.toLocaleString()} words` : "Not added"}</strong></div><div><span>Estimated runtime</span><strong>{duration ? `About ${duration} min` : "—"}</strong></div><div><span>Visual sourcing</span><strong>Research-led</strong></div></div>
                <div className="summary-footnote"><Icon name="info" /><span>Runtime is an estimate. Final length depends on narration and edit.</span></div>

                <button className="generate-button" type="submit" disabled={loading || !script.trim() || apiOnline === false}>
                  {loading ? <><span className="button-spinner" /> Starting…</> : <>Create and start <Icon name="arrow" /></>}
                </button>
                <p className="button-caption">You can follow each stage as your film comes together.</p>
              </section>

              <section className="status-card" aria-live="polite">
                <div className="status-heading"><span className={`status-indicator ${status?.status === "failed" ? "status-error" : status?.status === "completed" ? "status-done" : projectId ? "status-active" : ""}`} /><div><span className="eyebrow">PRODUCTION STATUS</span><h3>{statusTitle(status?.status, !!projectId, loading)}</h3></div></div>
                {projectId ? <>
                  <p className="status-message">{message || "Waiting for production status…"}</p>
                  <div className="stage-list">{stageOrder.map(([key, label]) => {
                    const stageStatus = status?.stages?.[key]?.status || (status?.current_stage === key ? "running" : "pending");
                    return <div className={`stage-row ${stageStatus}`} key={key}><span className="stage-check">{stageStatus === "completed" ? "✓" : stageStatus === "running" ? <span /> : stageStatus === "failed" ? "!" : ""}</span><span>{label}</span>{stageStatus === "running" && <small>NOW</small>}{stageStatus === "skipped" && <small>SKIPPED</small>}{stageStatus === "failed" && <small>FAILED</small>}</div>;
                  })}</div>
                  <div className="project-reference"><span>PROJECT</span><code>{projectId.slice(0, 8)}…</code>{jobId && <><i /><span>JOB</span><code>{jobId.slice(0, 8)}…</code></>}</div>
                </> : <div className="empty-status"><div className="status-skeleton"><span /><span /><span /></div><p>Your production stages will appear here after you start.</p></div>}
              </section>

              {videoUrl && <section className="video-card"><div className="video-heading"><span className="eyebrow">FINAL CUT</span><span className="ready-pill">READY</span></div><video controls preload="metadata" src={videoUrl} /><a href={videoUrl} target="_blank" rel="noreferrer">Open or download MP4 <Icon name="arrow-up-right" /></a></section>}
            </aside>
          </form>
          <footer className="page-footer"><span>CINEMORA</span><span>Production workspace</span></footer>
        </div>
      </section>
    </main>
  );
}

function stageLabel(stage: string) {
  return stageOrder.find(([key]) => key === stage)?.[1] || stage.replaceAll("_", " ");
}

function statusTitle(status: string | undefined, hasProject: boolean, loading: boolean) {
  if (loading) return "Creating your project";
  if (status === "completed") return "Documentary complete";
  if (status === "failed") return "Production needs attention";
  if (status === "running") return "Your film is in production";
  if (status === "queued" || status === "created" || status === "pipeline_queued") return "Waiting in the queue";
  return hasProject ? "Checking production…" : "Ready when you are";
}

function Icon({ name }: { name: string }) {
  const common = { width: 18, height: 18, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.7, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, "aria-hidden": true as const };
  const paths: Record<string, ReactNode> = {
    spark: <><path d="m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3Z" /><path d="m19 15 .9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15Z" /></>,
    grid: <><rect x="3.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="3.5" y="13.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="13.5" width="7" height="7" rx="1.5" /></>,
    image: <><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="8.5" cy="9" r="1.5" /><path d="m21 15-5-5L5 20" /></>,
    layers: <><path d="m12 3 9 5-9 5-9-5 9-5Z" /><path d="m3 12 9 5 9-5M3 16l9 5 9-5" /></>,
    wand: <><path d="m15 4 5 5M4 20l12.5-12.5M7 4v4M5 6h4M18 14v4M16 16h4" /><path d="m13 6 5 5" /></>,
    more: <><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></>,
    chevron: <path d="m7 10 5 5 5-5" />,
    search: <><circle cx="10.8" cy="10.8" r="6.8" /><path d="m16 16 4.5 4.5" /></>,
    film: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4" /></>,
    mic: <><rect x="9" y="3" width="6" height="12" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3M9 21h6" /></>,
    info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v5M12 8h.01" /></>,
    arrow: <><path d="M5 12h14M13 6l6 6-6 6" /></>,
    "arrow-up-right": <><path d="M7 17 17 7M8 7h9v9" /></>,
  };
  return <svg {...common}>{paths[name] || paths.spark}</svg>;
}
