"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { StatusBadge } from "@/components/status-badge";
import { EmptyState, ErrorState, LoadingState } from "@/components/states";
import {
  createProject as apiCreateProject,
  getCurrentUser,
  getProjectStatus,
  getVoiceCatalogue,
  listProjects,
  submitRun,
  type ApiConnectionStatus,
  type ProjectStatus,
  type ProjectSummary,
  type VoiceCatalogueEntry,
} from "@/lib/api";
import { StageProgress } from "@/components/stage-progress";
import { ApiConnectionBadge } from "@/components/connection-status";
import { AuthenticatedVideo } from "@/components/authenticated-media";
import { ArrowRight, ArrowUpRight, ChevronDown, Mic, Sparkles } from "lucide-react";

type Project = ProjectSummary;

function label(value: string) { return value.replaceAll("_", " "); }

const ACTIVE_STATUSES = ["created", "queued", "pipeline_queued", "running", "awaiting_approval"];
const IN_PRODUCTION_STATUSES = ["created", "queued", "pipeline_queued", "running"];

// The backend's real script cap (api_v1 MAX_SCRIPT_CHARS) — the
// same limit the production form enforces.
const MAX_SCRIPT_CHARS = 60000;

// The canonical narration voice (the installed Piper model), and
// the honest catalogue fallback — the same fact app/create
// falls back to when the catalogue endpoint cannot be reached:
// the one installed voice. It is labelled as a fallback whenever
// it is shown. FS-2: there is no genre selection on this desk —
// the documentary treatment (genre pack) is derived by the
// backend from the script, and is never a user choice.
const NARRATION_VOICE_ID = "en_US-lessac-medium";
const FALLBACK_VOICES: VoiceCatalogueEntry[] = [
  { id: NARRATION_VOICE_ID, is_default: false, alias_of: null, model: "en_US-lessac-medium.onnx", model_file: "en_US-lessac-medium.onnx", config_file: "en_US-lessac-medium.onnx.json", installed: true },
  { id: "default", is_default: true, alias_of: NARRATION_VOICE_ID, model: "en_US-lessac-medium.onnx", model_file: "en_US-lessac-medium.onnx", config_file: "en_US-lessac-medium.onnx.json", installed: true },
];

// GoMotion redesign (2026-11-24): the desk is create-first. The
// page opens on the step-numbered creation flow — 1 Input
// (write a script, or the voiceover path), 2 Voice & story
// (title, narration voice from the real catalogue),
// 3 Start — and the desk's live data (summary counts, approvals
// queue, active and recent productions) follows as cards.
// Submitting runs the production form's own path: createProject
// with the consumed option keys (voice_id, captions,
// require_plan_approval), then submitRun — no new endpoints, and
// the full configuration (presets, theme, music) stays one link
// away on /create, named as such.
export default function StudioPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [stageByProject, setStageByProject] = useState<Record<string, ProjectStatus>>({});
  const projectsRef = useRef<Project[]>([]);

  // --- Create flow state (mirrors app/create's flat path) ---
  const [mode, setMode] = useState<"write" | "voiceover">("write");
  const [script, setScript] = useState("");
  const [title, setTitle] = useState("");
  const [voices, setVoices] = useState<VoiceCatalogueEntry[]>(FALLBACK_VOICES);
  const [voiceId, setVoiceId] = useState("");
  const [catalogueState, setCatalogueState] = useState<"loading" | "api" | "fallback">("loading");
  const [requirePlanApproval, setRequirePlanApproval] = useState(false);
  // FS-C (FS-5): the plan-approval queue links into the admin-only
  // Visual Plan surface, and approval itself is an admin action —
  // the queue renders only for admin/super_admin roles (fail-closed
  // until the role is known). Ordinary users' submissions no longer
  // park at the gate, so they never generate queue entries.
  const [isAdmin, setIsAdmin] = useState(false);
  const [connection, setConnection] = useState<ApiConnectionStatus | null>(null);
  const [creating, setCreating] = useState(false);
  const [createMessage, setCreateMessage] = useState("");
  const [created, setCreated] = useState<{ id: string; message: string } | null>(null);

  const words = useMemo(() => script.trim().split(/\s+/).filter(Boolean).length, [script]);
  const duration = words ? Math.max(1, Math.round(words / 145)) : 0;
  const voiceOptions = voices.filter((voice) => !voice.alias_of);
  const canonicalVoice = voiceOptions[0];
  const effectiveVoiceId = voiceId || canonicalVoice?.id || NARRATION_VOICE_ID;
  // The same submit gate as the production form: a production
  // cannot be created through a rejected session or an
  // unreachable API, and the badge in step 3 says which it is.
  const apiBlocked = connection !== null && connection.state !== "connected";

  useEffect(() => {
    const controller = new AbortController();
    getCurrentUser(controller.signal)
      .then((user) =>
        setIsAdmin(
          user !== null &&
            (user.role === "admin" || user.role === "super_admin"),
        ),
      )
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const voiceData = await getVoiceCatalogue(controller.signal);
          if (controller.signal.aborted) return;
          if (voiceData.voices?.length) {
            setVoices(voiceData.voices);
            const canonical = voiceData.voices.find((voice) => !voice.alias_of) || voiceData.voices[0];
            if (canonical) setVoiceId(canonical.id);
          }
          setCatalogueState("api");
        } catch {
          if (!controller.signal.aborted) setCatalogueState("fallback");
        }
      })();
    }, 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, []);

  async function startProduction(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (mode !== "write") {
      setCreateMessage("Voiceover productions start from a project's Configuration tab — choose a project in step 1.");
      return;
    }
    if (!script.trim()) {
      setCreateMessage("Add a script before starting production.");
      return;
    }
    if (script.length > MAX_SCRIPT_CHARS) {
      setCreateMessage(`Scripts are limited to ${MAX_SCRIPT_CHARS.toLocaleString()} characters. Shorten the script and try again.`);
      return;
    }
    setCreating(true);
    setCreated(null);
    setCreateMessage("Creating your project…");
    try {
      const createdProject = await apiCreateProject({
        title: title.trim() || undefined,
        script: script.trim(),
        options: {
          voice_id: effectiveVoiceId,
          captions: "burn",
          require_plan_approval: isAdmin ? requirePlanApproval : false,
        },
      });
      const started = await submitRun(createdProject.id);
      setCreated({
        id: createdProject.id,
        message:
          started.phase === "analysis"
            ? "Project created. Its production plan is being prepared for your review."
            : started.deduplicated
              ? "An identical production is already in progress for this script."
              : "Project created and added to the production queue.",
      });
      setCreateMessage("");
      void load(undefined, true);
    } catch (cause) {
      setCreateMessage(cause instanceof Error ? cause.message : "Could not start documentary production.");
    } finally {
      setCreating(false);
    }
  }

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
      <header className="dash-head"><h1>Create a production</h1></header>

      <form className="dash-create" onSubmit={startProduction}>
        <section className="gm-card" aria-labelledby="dash-input-title">
          <div className="gm-step-head">
            <span className="gm-step-num">1</span>
            <h2 id="dash-input-title">Input</h2>
            {mode === "write" && <span className="required-tag">{script.length.toLocaleString()} / {MAX_SCRIPT_CHARS.toLocaleString()} characters</span>}
          </div>
          <div className="gm-segmented" role="group" aria-label="Input source">
            <button type="button" aria-pressed={mode === "write"} onClick={() => setMode("write")}>Write a script</button>
            <button type="button" aria-pressed={mode === "voiceover"} onClick={() => setMode("voiceover")}>Upload a voiceover</button>
          </div>
          {mode === "write" ? <>
            <label className="sr-only" htmlFor="dash-script">Documentary script</label>
            <textarea
              id="dash-script"
              className="gm-textarea"
              value={script}
              onChange={(event) => setScript(event.target.value)}
              maxLength={MAX_SCRIPT_CHARS}
              placeholder="Paste or write your script here. We narrate it, add the visuals, subtitles and music."
            />
            <div className="gm-count"><span>{words.toLocaleString()} words · {script.length.toLocaleString()} / {MAX_SCRIPT_CHARS.toLocaleString()}</span></div>
            <Link className="gm-ai-card" href="/scripts">
              <Sparkles size={18} strokeWidth={1.8} aria-hidden />
              <span>No script yet? <strong>Write one with AI</strong></span>
              <ArrowRight className="gm-ai-arrow" size={18} strokeWidth={1.8} aria-hidden />
            </Link>
          </> : <div className="gm-upload-panel">
            <Mic size={22} strokeWidth={1.7} aria-hidden />
            <p>A recorded voiceover becomes a script inside a project: open a project, and its Configuration tab transcribes the recording into a script you review.</p>
            <Link className="button button-primary" href="/projects">Choose a project <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>
          </div>}
        </section>

        <section className="gm-card" aria-labelledby="dash-story-title">
          <div className="gm-step-head">
            <span className="gm-step-num">2</span>
            <h2 id="dash-story-title">Voice &amp; story</h2>
          </div>
          <div className="form-fields gm-fields">
            <label className="field-label" htmlFor="dash-title">TITLE
              <input id="dash-title" className="thumbnail-input" value={title} onChange={(event) => setTitle(event.target.value)} maxLength={300} placeholder="e.g. The Yangtze Incident" />
            </label>
            <label className="field-label" htmlFor="dash-voice">NARRATION VOICE
              <span className="select-wrap"><select id="dash-voice" value={effectiveVoiceId} onChange={(event) => setVoiceId(event.target.value)}>{voiceOptions.map((voice) => <option key={voice.id} value={voice.id}>{voice.id} · English (US)</option>)}</select><ChevronDown size={18} strokeWidth={1.7} aria-hidden /></span>
            </label>
            <div className="field-label length-field"><span>ESTIMATED RUNTIME</span><div className="runtime-value">{duration ? `~${duration} min` : "Add a script"}</div></div>
          </div>
          {catalogueState === "fallback" && <p className="voice-note catalogue-note" role="status">The voice catalogue could not be reached, so this form shows the fallback fact — the one installed voice. Nothing else has been added.</p>}
          {catalogueState === "loading" && <p className="voice-note catalogue-note" role="status">Loading the voice catalogue from the studio API…</p>}
          {isAdmin && <label className="motion-toggle"><input type="checkbox" checked={requirePlanApproval} onChange={(event) => setRequirePlanApproval(event.target.checked)} /><span><strong>Review plan before rendering</strong><small>Production pauses after story analysis — you approve the per-sentence plan before any footage is retrieved or rendered.</small></span></label>}
        </section>

        <section className="gm-card" aria-labelledby="dash-start-title">
          <div className="gm-step-head">
            <span className="gm-step-num">3</span>
            <h2 id="dash-start-title">Start</h2>
          </div>
          <p className="gm-summary-line">voice <strong>{effectiveVoiceId}</strong> · {words ? `${words.toLocaleString()} words, about ${duration} min` : "no script yet"} · captions burned in{isAdmin && requirePlanApproval ? " · plan review on" : ""}</p>
          <p className="status-message"><ApiConnectionBadge onStatus={setConnection} /></p>
          <button className="generate-button" type="submit" disabled={creating || !script.trim() || apiBlocked}>
            {creating ? <><span className="button-spinner" /> Starting…</> : <>Create and start <ArrowRight size={18} strokeWidth={1.7} aria-hidden /></>}
          </button>
          {createMessage && <p className="status-message" role="status">{createMessage}</p>}
          <p className="button-caption">Presets, theme, music and caption options live in the <Link className="text-link" href="/create">full production form</Link>.</p>
        </section>
      </form>

      {created && <section className="gm-card dash-created dash-section" aria-live="polite">
        <p>{created.message}</p>
        <Link className="button button-primary" href={`/projects/${created.id}`}>Open project <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>
      </section>}

      <section className="dashboard-metrics" aria-label="Production summary">
        <div><span>ALL PROJECTS</span><strong>{busy ? "—" : projects.length}</strong></div>
        <div><span>IN PRODUCTION</span><strong>{busy ? "—" : inProductionCount}</strong></div>
        <div><span>AWAITING APPROVAL</span><strong>{busy ? "—" : awaitingApproval.length}</strong></div>
        <div><span>COMPLETED</span><strong>{busy ? "—" : completeCount}</strong></div>
        <div><span>FAILED</span><strong>{busy ? "—" : failedCount}</strong></div>
        <p>Counts are computed from the live project list.</p>
      </section>

      {!busy && !error && isAdmin && awaitingApproval.length > 0 && (
        <section className="dashboard-approvals" aria-label="Awaiting your approval">
          <div className="section-heading"><div><p className="eyebrow">NEEDS YOUR DECISION</p><h2>Awaiting your approval</h2></div></div>
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
      {!busy && !error && projects.length === 0 && <EmptyState className="dashboard-empty" eyebrow="A CLEAR FIRST FRAME" title="Nothing here yet." description="Projects you create appear on this desk with their real production state and export." />}

      {!busy && !error && activeProjects.length > 0 && (
        <section className="dashboard-projects" aria-label="Active productions">
          <div className="section-heading"><div><p className="eyebrow">IN PRODUCTION</p><h2>Active productions</h2></div></div>
          <div className="dashboard-project-grid">{activeProjects.map(projectCard)}</div>
        </section>
      )}

      {!busy && !error && recentFinished.length > 0 && (
        <section className="dashboard-projects" aria-label="Recent and finished">
          <div className="section-heading"><div><p className="eyebrow">RECENT WORK</p><h2>Back to the edit</h2></div><Link className="text-link" href="/projects">View all projects <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></div>
          <div className="dashboard-project-grid">{recentFinished.map(projectCard)}</div>
        </section>
      )}

      <footer className="app-footer"><span>CINEMORA / STUDIO</span><span>Project information is loaded from your Cinemora API.</span></footer>
    </div>
  </main>;
}
