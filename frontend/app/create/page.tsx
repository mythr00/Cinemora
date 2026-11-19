"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { AppNavigation } from "@/components/app-navigation";
import {
  apiUrl,
  createProject as apiCreateProject,
  getHealth,
  getProjectStatus,
  submitRun,
  type ProjectStatus,
} from "@/lib/api";
import {
  ArrowRight,
  ArrowUpRight,
  Check,
  ChevronDown,
  AlertCircle,
  Film,
  Info,
  Mic,
  Search,
} from "lucide-react";

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

// The pipeline's six real stages (backend production_state
// PRODUCTION_STAGES). Earlier versions of this page also listed a
// "timeline_builder" and a "renderer" stage that do not exist.
const stageOrder = [
  ["voice_actor", "Narration"],
  ["script_analysis", "Story analysis"],
  ["asset_manager", "Visual research"],
  ["motion_designer", "Motion design"],
  ["sound_designer", "Sound mix"],
  ["video_editor", "Final edit"],
] as const;

// The one narration voice actually installed in the backend
// (Stage 3J catalogue): a built-in Piper voice. Brief Stage 3
// (C-05) removed the catalogue browser that pretended otherwise —
// /voices does not exist in the committed backend, and any other
// voice id would silently fall back to this one at production time.
const NARRATION_VOICE_ID = "en_US-lessac-medium";

// The backend's real script cap (api_v1 MAX_SCRIPT_CHARS). The form
// previously enforced a lower, wrong limit and labelled a character
// counter as "words" (audit D-14).
const MAX_SCRIPT_CHARS = 60000;

export default function Home() {
  const [script, setScript] = useState("");
  const [title, setTitle] = useState("");
  const [category, setCategory] = useState("Documentary");
  const [requirePlanApproval, setRequirePlanApproval] = useState(false);
  const [loading, setLoading] = useState(false);
  const [projectId, setProjectId] = useState("");
  const [jobId, setJobId] = useState("");
  const [status, setStatus] = useState<ProjectStatus | null>(null);
  const [message, setMessage] = useState("");
  const [apiOnline, setApiOnline] = useState<boolean | null>(null);
  const [includeCaptions, setIncludeCaptions] = useState(true);

  const words = useMemo(() => script.trim().split(/\s+/).filter(Boolean).length, [script]);
  const duration = words ? Math.max(1, Math.round(words / 145)) : 0;
  const isTerminal = status?.status === "completed" || status?.status === "failed";

  useEffect(() => {
    const controller = new AbortController();
    const draftTimer = setTimeout(() => {
      const channelDraft = sessionStorage.getItem("cinemora-script-draft");
      if (channelDraft) {
        setScript(channelDraft.slice(0, MAX_SCRIPT_CHARS));
        sessionStorage.removeItem("cinemora-script-draft");
        setMessage("Script draft loaded. Review it before starting production.");
      }
    }, 0);
    // Reachability only: /api/v1/health is unauthenticated by
    // design, so this says the service answers — not that the
    // configured token is accepted (that check is register C-06).
    // The topbar badge is labelled accordingly ("reachable").
    getHealth(controller.signal)
      .then(() => setApiOnline(true))
      .catch(() => setApiOnline(false));
    return () => { controller.abort(); clearTimeout(draftTimer); };
  }, []);

  useEffect(() => {
    if (!projectId || isTerminal) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;

    async function pollStatus() {
      try {
        const data = await getProjectStatus(projectId);
        if (stopped) return;
        setStatus(data);
        if (data.status === "completed" && data.video_url) {
          setMessage("Your documentary is ready to review.");
        } else if (data.status === "failed") {
          setMessage(data.error || "Production stopped before completion.");
        } else if (data.status === "awaiting_approval") {
          setMessage("The production plan is ready. Review it and approve to start the full production.");
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

    if (script.length > MAX_SCRIPT_CHARS) {
      setMessage(`Scripts are limited to ${MAX_SCRIPT_CHARS.toLocaleString()} characters. Shorten the script and try again.`);
      return;
    }

    setLoading(true);
    setProjectId("");
    setJobId("");
    setStatus(null);
    setMessage("Creating your project…");

    try {
      // API v1 (MA-40): the project is created with the options the
      // pipeline actually consumes, persisted on the project row —
      // the old flow posted a render_options body to endpoints that
      // silently discarded it, plus query parameters the analyze
      // handler never read.
      // Brief Stage 3 (C-05): the submitted options are now EXACTLY
      // the consumed keys of ProjectOptionsIn — genre (genre-pack
      // resolution), voice_id (narration), captions (burn/off) and
      // require_plan_approval (the plan gate). The music, sfx,
      // graphics, language and background keys the form used to add
      // were recorded in the snapshot but consumed by nothing
      // (verified against jobs.py: recognised keys are music_file,
      // voice_id, captions), so their controls are gone. Music
      // reaches a production only via the operator-level music_file
      // option, which has no control on this form.
      const created = await apiCreateProject({
        title: title.trim() || undefined,
        script: script.trim(),
        category,
        options: {
          genre: category,
          voice_id: NARRATION_VOICE_ID,
          captions: includeCaptions ? "burn" : "off",
          require_plan_approval: requirePlanApproval,
        },
      });

      setProjectId(created.id);
      setMessage("Project created. Adding it to the production queue…");
      const started = await submitRun(created.id);

      setJobId(started.job_id || started.run?.run_id || "");
      setMessage(
        started.phase === "analysis"
          ? "Project created. Its production plan is being prepared for your review."
          : started.deduplicated
            ? "An identical production is already in progress for this script."
            : "Project created and added to the production queue.",
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Could not start documentary production.");
    } finally {
      setLoading(false);
    }
  }

  const videoUrl = status?.video_url ? apiUrl(status.video_url) : "";

  // Brief Stage 4 (C-11): the page renders inside the shared
  // studio navigation like every other studio page. Its private
  // sidebar (a third navigation system: "Create / Overview /
  // Projects" labels, a brand link to the landing page) is
  // retired; the API reachability badge it carried moves to the
  // topbar, unchanged — same probe, same honest label (the
  // authenticated connection check is register C-06).
  return (
    <main className="app-page create-page">
      <AppNavigation />

      <section className="workspace" id="create">
        <header className="topbar">
          <div className="breadcrumbs"><span>Cinemora</span><span className="crumb-separator">/</span><strong>New documentary</strong></div>
          <div className="topbar-right"><span className="connection"><span className={`connection-dot ${apiOnline === false ? "offline" : ""}`} /><span>{apiOnline === null ? "Contacting the studio API" : apiOnline ? "Studio API reachable" : "Studio API unavailable"}</span></span><span className="draft-status"><span /> New project</span><span className="edition-label">STUDIO EDITION</span></div>
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
                  <span className="required-tag">{script.length.toLocaleString()} / {MAX_SCRIPT_CHARS.toLocaleString()} characters</span>
                </div>
                <label className="sr-only" htmlFor="documentary-script">Documentary script</label>
                <textarea id="documentary-script" value={script} onChange={(event) => setScript(event.target.value)} maxLength={MAX_SCRIPT_CHARS} placeholder="Paste script…" />
                <div className="editor-footer"><span className="editor-hint">{words.toLocaleString()} words</span><span className="count">{script.length.toLocaleString()} / {MAX_SCRIPT_CHARS.toLocaleString()} characters</span></div>
                {script.length >= MAX_SCRIPT_CHARS && <p className="voice-note" role="status">Your script is at the {MAX_SCRIPT_CHARS.toLocaleString()}-character limit — the production API accepts at most {MAX_SCRIPT_CHARS.toLocaleString()} characters, and nothing beyond the limit is kept.</p>}
              </section>

              <section className="panel details-panel">
                <div className="panel-heading compact-heading">
                  <div className="step-marker">02</div>
                  <div className="panel-title"><h2>Project details</h2><p>Choose a category and check the estimated runtime.</p></div>
                </div>
                <div className="form-fields">
                  <label className="field-label" htmlFor="project-title">TITLE
                    <input id="project-title" className="thumbnail-input" value={title} onChange={(event) => setTitle(event.target.value)} maxLength={300} placeholder="e.g. The Yangtze Incident" />
                  </label>
                  <label className="field-label" htmlFor="category">STORY CATEGORY
                    <span className="select-wrap"><select id="category" value={category} onChange={(event) => setCategory(event.target.value)}>{categories.map((item) => <option key={item}>{item}</option>)}</select><ChevronDown size={18} strokeWidth={1.7} aria-hidden /></span>
                  </label>
                  <div className="field-label length-field"><span>ESTIMATED RUNTIME</span><div className="runtime-value">{duration ? `~${duration} min` : "Add a script"}</div><small>Based on about 145 spoken words per minute.</small></div>
                </div>
                <div className="workflow-includes"><span className="include-label">INCLUDED IN YOUR WORKFLOW</span><div className="include-chips"><span><Search size={18} strokeWidth={1.7} aria-hidden /> Story research</span><span><Film size={18} strokeWidth={1.7} aria-hidden /> Matched visuals</span><span><Mic size={18} strokeWidth={1.7} aria-hidden /> Narration + captions</span></div></div>
              </section>

              <section className="panel voice-panel" aria-labelledby="voice-title">
                <div className="panel-heading compact-heading"><div className="step-marker">03</div><div className="panel-title"><h2 id="voice-title">Narration voice</h2><p>Every production in this build is narrated with the studio's one installed voice.</p></div></div>
                <div className="voice-catalog-list"><div className="voice-catalog-row"><div><strong>{NARRATION_VOICE_ID}</strong><small>English (US) · Built-in Piper voice · Model installed</small></div><span>In use</span></div></div>
                <p className="voice-note">Narration language is English (US) — the language of the installed voice. There is no voice or language picker because there is nothing else installed to pick: the form sends exactly this voice, so the voice named here is the voice your film will use.</p>
              </section>

              <section className="panel motion-option-panel"><div className="panel-heading compact-heading"><div className="step-marker">04</div><div className="panel-title"><h2>Generation options</h2><p>Only options the production pipeline actually applies — nothing here is decorative.</p></div></div><label className="motion-toggle"><input type="checkbox" checked={includeCaptions} onChange={(event) => setIncludeCaptions(event.target.checked)} /><span><strong>Captions</strong><small>Burn timed captions into the finished video. Turn off to render without burned-in captions.</small></span></label><label className="motion-toggle"><input type="checkbox" checked={requirePlanApproval} onChange={(event) => setRequirePlanApproval(event.target.checked)} /><span><strong>Review plan before rendering</strong><small>Production pauses after story analysis. You review the per-sentence plan — proposed visuals, evidence and rights — and approve it before any footage is retrieved or rendered.</small></span></label><p className="music-license-note"><strong>Background music</strong> is not part of productions in this build: there is no music picker because the pipeline applies music only from an operator-supplied file, which this form does not collect. Films render with narration and captions only.</p><div className="music-license-note" aria-label="Footage license notice"><strong>Advanced · Rights and sourcing</strong><p>Footage is screened under the configured rights policy. A downloaded asset is not automatically cleared for publication; review its source and license before publishing.</p></div></section>

            </div>

            <aside className="right-column">
              <section className="summary-card">
                <div className="summary-top"><div><span className="eyebrow">PROJECT</span><h2>Summary</h2></div></div>
                <div className="summary-rows"><div><span>Category</span><strong>{category}</strong></div><div><span>Narration voice</span><strong>{NARRATION_VOICE_ID} · English (US)</strong></div><div><span>Captions</span><strong>{includeCaptions ? "Burned in" : "Off"}</strong></div><div><span>Plan review</span><strong>{requirePlanApproval ? "Required before rendering" : "Not required"}</strong></div><div><span>Script length</span><strong>{words ? `${words.toLocaleString()} words · ${script.length.toLocaleString()} characters` : "Not added"}</strong></div><div><span>Estimated runtime</span><strong>{duration ? `About ${duration} min` : "—"}</strong></div><div><span>Visual sourcing</span><strong>Research-led</strong></div></div>
                <div className="summary-footnote"><Info size={18} strokeWidth={1.7} aria-hidden /><span>Runtime is an estimate. Final length depends on narration and edit.</span></div>

                <button className="generate-button" type="submit" disabled={loading || !script.trim() || apiOnline === false}>
                  {loading ? <><span className="button-spinner" /> Starting…</> : <>Create and start <ArrowRight size={18} strokeWidth={1.7} aria-hidden /></>}
                </button>
                <p className="button-caption">You can follow each stage as your film comes together.</p>
              </section>

              <section className="status-card" aria-live="polite">
                <div className="status-heading"><span className={`status-indicator ${status?.status === "failed" ? "status-error" : status?.status === "completed" ? "status-done" : projectId ? "status-active" : ""}`} /><div><span className="eyebrow">PRODUCTION STATUS</span><h3>{statusTitle(status?.status, !!projectId, loading)}</h3></div></div>
                {projectId ? <>
                  <p className="status-message">{message || "Waiting for production status…"}</p>
                  {status?.status === "awaiting_approval" && <p className="status-message"><Link className="text-link" href={`/projects/${projectId}/plan`}>Review the production plan</Link></p>}
                  <div className="stage-list">{stageOrder.map(([key, label]) => {
                    const stageStatus = status?.stages?.[key]?.status || (status?.current_stage === key ? "running" : "pending");
                    return <div className={`stage-row ${stageStatus}`} key={key}><span className="stage-check">{stageStatus === "completed" ? <Check size={9} strokeWidth={3} aria-hidden /> : stageStatus === "running" ? <span /> : stageStatus === "failed" ? <AlertCircle size={9} strokeWidth={2.5} aria-hidden /> : ""}</span><span>{label}</span>{stageStatus === "running" && <small>NOW</small>}{stageStatus === "skipped" && <small>SKIPPED</small>}{stageStatus === "failed" && <small>FAILED</small>}</div>;
                  })}</div>
                  <div className="project-reference"><span>PROJECT</span><code>{projectId.slice(0, 8)}…</code>{jobId && <><i /><span>JOB</span><code>{jobId.slice(0, 8)}…</code></>}</div>
                </> : <div className="empty-status"><div className="status-skeleton"><span /><span /><span /></div><p>Your production stages will appear here after you start.</p></div>}
              </section>

              {videoUrl && <section className="video-card"><div className="video-heading"><span className="eyebrow">FINAL CUT</span><span className="ready-pill">READY</span></div><video controls preload="metadata" src={videoUrl} /><a href={videoUrl} target="_blank" rel="noreferrer">Open or download MP4 <ArrowUpRight size={18} strokeWidth={1.7} aria-hidden /></a></section>}
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
  if (status === "awaiting_approval") return "Plan ready for your review";
  if (status === "running") return "Your film is in production";
  if (status === "queued" || status === "created" || status === "pipeline_queued") return "Waiting in the queue";
  return hasProject ? "Checking production…" : "Ready when you are";
}
