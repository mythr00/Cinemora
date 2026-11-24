"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { AppNavigation } from "@/components/app-navigation";
import {
  createProject as apiCreateProject,
  getConfigCatalogue,
  getConfigDefaults,
  getGenreCatalogue,
  getProjectStatus,
  getVoiceCatalogue,
  submitRun,
  type ApiConnectionStatus,
  type ConfigCatalogue,
  type GenreCatalogueEntry,
  type ProductionConfig,
  type ProjectStatus,
  type VoiceCatalogueEntry,
} from "@/lib/api";
import {
  ProductionConfigForm,
  emptyProductionConfig,
} from "@/components/production-config-form";
import { ApiConnectionBadge } from "@/components/connection-status";
import { StageProgress } from "@/components/stage-progress";
import { AuthenticatedDownloadLink, AuthenticatedVideo } from "@/components/authenticated-media";
import {
  ArrowRight,
  ArrowUpRight,
  ChevronDown,
  Film,
  Info,
  Mic,
  Search,
} from "lucide-react";

// IA-06: the genre selector and the voice panel are rendered
// from the backend catalogues (GET /api/v1/catalogues/genres and
// /voices — the real genre packs and the installed voice
// catalogue). The constants below are ONLY the honest fallback
// used when those endpoints cannot be reached: they state the
// same previously-static truthful facts (the three real packs,
// the one installed voice and its default alias), and the UI
// labels them as a fallback whenever they are shown — no option
// is ever silently invented.
const FALLBACK_GENRES: GenreCatalogueEntry[] = [
  {
    id: "general",
    name: "General documentary",
    label: "General documentary",
    description:
      "Default pack when no genre is specified or detected. No genre assumptions: the historical per-template engine styles apply, with a modern chart-first motif order.",
    style_family: null,
    graphics_motifs: [],
    format_preferences: {},
    entity_handling: [],
    research_query_templates: [],
    pacing: {},
  },
  {
    id: "history",
    name: "History",
    label: "History",
    description:
      "Documentaries about past events, people and places. Research leans on records and archives; graphics lean on maps, documents and chronology.",
    style_family: "history",
    graphics_motifs: [],
    format_preferences: {},
    entity_handling: [],
    research_query_templates: [],
    pacing: {},
  },
  {
    id: "true_crime",
    name: "True crime",
    label: "True crime",
    description:
      "Documentaries about real criminal cases. Evidence discipline is absolute: nothing about a case is invented, and victims are treated with care.",
    style_family: "true_crime",
    graphics_motifs: [],
    format_preferences: {},
    entity_handling: [],
    research_query_templates: [],
    pacing: {},
  },
];

const FALLBACK_VOICES: VoiceCatalogueEntry[] = [
  {
    id: "en_US-lessac-medium",
    is_default: false,
    alias_of: null,
    model: "en_US-lessac-medium.onnx",
    model_file: "en_US-lessac-medium.onnx",
    config_file: "en_US-lessac-medium.onnx.json",
    installed: true,
  },
  {
    id: "default",
    is_default: true,
    alias_of: "en_US-lessac-medium",
    model: "en_US-lessac-medium.onnx",
    model_file: "en_US-lessac-medium.onnx",
    config_file: "en_US-lessac-medium.onnx.json",
    installed: true,
  },
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
// (Stage 3J catalogue): a built-in Piper voice. IA-06: the voice
// panel below is rendered from GET /api/v1/catalogues/voices;
// this constant is only the fallback canonical id used when the
// catalogue cannot be reached (see FALLBACK_VOICES).
const NARRATION_VOICE_ID = "en_US-lessac-medium";

// The backend's real script cap (api_v1 MAX_SCRIPT_CHARS). The form
// previously enforced a lower, wrong limit and labelled a character
// counter as "words" (audit D-14).
const MAX_SCRIPT_CHARS = 60000;

// Redesign F1 (plan §3.3): the form is a stepped production
// brief — one page, sectioned, no wizard hiding state:
// 1 Script · 2 Story (genre + voice from the real catalogues) ·
// 3 Visual approach (the plan-approval toggle) ·
// 4 Configuration (Phase 9: the pre-production configuration —
// presets, motion graphics, theme, music, voice, captions —
// seeded from the user's saved defaults and submitted as the
// project's production_config, which the backend cleans,
// validates and projects onto the consumed option keys) ·
// 5 Review & start (exactly what will be submitted + submit).
// Post-submit status with StageProgress + 3s polling is
// preserved verbatim.
export default function Home() {
  const [script, setScript] = useState("");
  const [title, setTitle] = useState("");
  const [genreId, setGenreId] = useState("general");
  const [genres, setGenres] = useState<GenreCatalogueEntry[]>(FALLBACK_GENRES);
  const [voices, setVoices] = useState<VoiceCatalogueEntry[]>(FALLBACK_VOICES);
  const [catalogueState, setCatalogueState] = useState<"loading" | "api" | "fallback">("loading");
  const [requirePlanApproval, setRequirePlanApproval] = useState(false);
  const [loading, setLoading] = useState(false);
  const [projectId, setProjectId] = useState("");
  const [jobId, setJobId] = useState("");
  const [status, setStatus] = useState<ProjectStatus | null>(null);
  const [message, setMessage] = useState("");
  const [connection, setConnection] = useState<ApiConnectionStatus | null>(null);
  // Phase 9: the pre-production configuration document. Seeded
  // from the user's saved defaults when they exist; submitted
  // as options.production_config.
  const [configCatalogue, setConfigCatalogue] = useState<ConfigCatalogue | null>(null);
  const [productionConfig, setProductionConfig] = useState<ProductionConfig>(emptyProductionConfig());
  const [configSeeded, setConfigSeeded] = useState(false);

  const words = useMemo(() => script.trim().split(/\s+/).filter(Boolean).length, [script]);
  const duration = words ? Math.max(1, Math.round(words / 145)) : 0;
  const isTerminal = status?.status === "completed" || status?.status === "failed";

  // IA-06: load the genre and voice catalogues from the API.
  // (IA-10 / F-04: the sessionStorage draft read that used to sit
  // here is removed — nothing in the active build ever writes
  // that key; its only setter lives in preserved/ code.)
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const [genreData, voiceData] = await Promise.all([
            getGenreCatalogue(controller.signal),
            getVoiceCatalogue(controller.signal),
          ]);
          if (controller.signal.aborted) return;
          if (genreData.genres?.length) setGenres(genreData.genres);
          if (voiceData.voices?.length) setVoices(voiceData.voices);
          setCatalogueState("api");
          // Phase 9: the configuration catalogue + the user's
          // saved defaults seed step 4. A failure here only
          // disables the configuration step (labelled as such);
          // it never blocks project creation.
          try {
            const [configData, defaultsData] = await Promise.all([
              getConfigCatalogue(controller.signal),
              getConfigDefaults(controller.signal),
            ]);
            if (controller.signal.aborted) return;
            setConfigCatalogue(configData);
            // Seed from the saved defaults (or the empty
            // document) and name the canonical voice explicitly
            // unless a choice already exists, so the voice the
            // Story step marks "In use" is the voice the
            // configuration submits.
            const seed = defaultsData.defaults
              ? structuredClone(defaultsData.defaults)
              : emptyProductionConfig();
            if (!seed.narration.voice_id) {
              const fetchedCanonical =
                voiceData.voices?.find((voice) => !voice.alias_of) ||
                voiceData.voices?.[0];
              seed.narration = {
                ...seed.narration,
                voice_id: fetchedCanonical?.id || NARRATION_VOICE_ID,
              };
            }
            setProductionConfig(seed);
            setConfigSeeded(true);
          } catch {
            if (!controller.signal.aborted) setConfigSeeded(true);
          }
        } catch {
          if (!controller.signal.aborted) {
            setCatalogueState("fallback");
            setConfigSeeded(true);
          }
        }
      })();
    }, 0);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, []);

  const selectedGenre = genres.find((genre) => genre.id === genreId) || genres[0];
  const canonicalVoice = voices.find((voice) => !voice.alias_of) || voices[0];
  const narrationVoiceId = canonicalVoice?.id || NARRATION_VOICE_ID;



  // Brief Stage 5 (C-06): the connection badge is the authenticated
  // connection check (ApiConnectionBadge performs one authenticated
  // v1 call and classifies the outcome). Submitting is blocked
  // whenever the classified state is not "connected" — a production
  // cannot be created through a rejected token, an unconfigured
  // API, or an unreachable one, and the badge says which it is.
  // Redesign F1: the badge lives in Step 5 (the shell top bar
  // carries its own indicator for display; this instance feeds
  // the submit gate via onStatus, exactly as before).
  const apiBlocked = connection !== null && connection.state !== "connected";

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
      // Phase 9: the configuration document rides along when
      // its catalogue loaded — the backend cleans it, stores it
      // on the project and projects it onto the flat keys the
      // pipeline consumes (voice, captions, music, graphics,
      // theme), overriding the flat fallbacks below. Without a
      // configuration the flat keys behave exactly as before.
      const created = await apiCreateProject({
        title: title.trim() || undefined,
        script: script.trim(),
        category: selectedGenre?.label || selectedGenre?.name || genreId,
        options: {
          genre: selectedGenre?.id || genreId,
          voice_id: narrationVoiceId,
          captions: configCatalogue
            ? productionConfig.captions.mode
            : "burn",
          require_plan_approval: requirePlanApproval,
          ...(configCatalogue
            ? { production_config: productionConfig }
            : {}),
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

  // IA-01 (audit F-02): keep the raw media path — playback and
  // download go through the authenticated media components, which
  // send the API token as a header; a bare apiUrl() src/href here
  // received the media endpoint's 401 instead of the film.
  const videoPath = status?.video_url || "";

  // Brief Stage 4 (C-11): the page renders inside the shared
  // studio navigation like every other studio page. Its private
  // sidebar (a third navigation system) and private topbar are
  // retired; the shell (redesign §2.6) provides the chrome.
  return (
    <main className="app-page create-page">
      <AppNavigation />
      <div className="content-wrap">
        <div className="page-heading">
          <p className="eyebrow">NEW PRODUCTION</p>
          <h1>New documentary</h1>
        </div>

        <form className="create-steps" onSubmit={createProject}>
          <section className="create-step" aria-labelledby="script-title">
            <div className="create-step-head">
              <div className="step-marker">1</div>
              <div><h2 id="script-title">Script</h2><p>Paste the narration you want produced.</p></div>
              <span className="required-tag">{script.length.toLocaleString()} / {MAX_SCRIPT_CHARS.toLocaleString()} characters</span>
            </div>
            <div className="script-panel">
              <label className="sr-only" htmlFor="documentary-script">Documentary script</label>
              <textarea id="documentary-script" value={script} onChange={(event) => setScript(event.target.value)} maxLength={MAX_SCRIPT_CHARS} placeholder="Paste script…" />
              <div className="script-meta"><span className="editor-hint">{words.toLocaleString()} words</span><span className="count">{script.length.toLocaleString()} / {MAX_SCRIPT_CHARS.toLocaleString()} characters</span></div>
              {script.length >= MAX_SCRIPT_CHARS && <p className="voice-note" role="status">Your script is at the {MAX_SCRIPT_CHARS.toLocaleString()}-character limit — the production API accepts at most {MAX_SCRIPT_CHARS.toLocaleString()} characters, and nothing beyond the limit is kept.</p>}
            </div>
          </section>

          <section className="create-step" aria-labelledby="story-title">
            <div className="create-step-head">
              <div className="step-marker">2</div>
              <div><h2 id="story-title">Story</h2><p>Title, genre pack and narration voice.</p></div>
            </div>
            <div className="form-fields">
              <label className="field-label" htmlFor="project-title">TITLE
                <input id="project-title" className="thumbnail-input" value={title} onChange={(event) => setTitle(event.target.value)} maxLength={300} placeholder="e.g. The Yangtze Incident" />
              </label>
              <label className="field-label" htmlFor="category">GENRE PACK
                <span className="select-wrap"><select id="category" value={selectedGenre?.id || genreId} onChange={(event) => setGenreId(event.target.value)}>{genres.map((genre) => <option key={genre.id} value={genre.id}>{genre.label || genre.name || genre.id}</option>)}</select><ChevronDown size={18} strokeWidth={1.7} aria-hidden /></span>
                {selectedGenre?.description && <span className="genre-description">{selectedGenre.description}</span>}
              </label>
              <div className="field-label length-field"><span>ESTIMATED RUNTIME</span><div className="runtime-value">{duration ? `~${duration} min` : "Add a script"}</div><small>Based on about 145 spoken words per minute.</small></div>
            </div>
            <div className="voice-catalog-list">{voices.map((voice) => <div className="voice-catalog-row" key={voice.id}><div><strong>{voice.id}</strong><small>{voice.alias_of ? `Default alias of ${voice.alias_of} · Same installed Piper model` : "English (US) · Built-in Piper voice"} · {voice.installed ? "Model installed" : "Model not installed"}</small></div>{!voice.alias_of && <span>In use</span>}{voice.is_default && <span>Default</span>}</div>)}</div>
            {catalogueState === "fallback" && <p className="voice-note catalogue-note" role="status">The genre and voice catalogues could not be reached, so this form is showing the fallback facts — the three real genre packs and the one installed voice listed above — labelled as a fallback. Nothing else has been added.</p>}
            {catalogueState === "loading" && <p className="voice-note catalogue-note" role="status">Loading the genre and voice catalogues from the studio API…</p>}
            <p className="voice-note">Narration language is English (US) — the language of the installed voices.</p>
            <div className="workflow-includes"><span className="include-label">INCLUDED IN YOUR WORKFLOW</span><div className="include-chips"><span><Search size={18} strokeWidth={1.7} aria-hidden /> Story research</span><span><Film size={18} strokeWidth={1.7} aria-hidden /> Matched visuals</span><span><Mic size={18} strokeWidth={1.7} aria-hidden /> Narration + captions</span></div></div>
          </section>

          <section className="create-step" aria-labelledby="visual-title">
            <div className="create-step-head">
              <div className="step-marker">3</div>
              <div><h2 id="visual-title">Visual approach</h2><p>Options the pipeline actually applies.</p></div>
            </div>
            <label className="motion-toggle"><input type="checkbox" checked={requirePlanApproval} onChange={(event) => setRequirePlanApproval(event.target.checked)} /><span><strong>Review plan before rendering</strong><small>Production pauses after story analysis — you approve the per-sentence plan before any footage is retrieved or rendered.</small></span></label>
          </section>

          <section className="create-step" aria-labelledby="config-title">
            <div className="create-step-head">
              <div className="step-marker">4</div>
              <div><h2 id="config-title">Configuration</h2><p>Presets, graphics, theme, music, voice and captions.</p></div>
            </div>
            {configCatalogue ? (
              <ProductionConfigForm
                catalogue={configCatalogue}
                value={productionConfig}
                onChange={setProductionConfig}
                mode="create"
              />
            ) : (
              <p className="voice-note" role="status">
                {configSeeded
                  ? "The configuration catalogue could not be reached, so this project will be created with the pipeline\u2019s standard settings. The full configuration studio is available afterwards in the project\u2019s Configuration tab."
                  : "Loading the configuration catalogue…"}
              </p>
            )}
            <div className="music-license-note" aria-label="Footage license notice"><strong>Advanced · Rights and sourcing</strong><p>Footage is screened under the configured rights policy — a downloaded asset is not automatically cleared for publication.</p></div>
          </section>

          <section className="create-step" aria-labelledby="review-title">
            <div className="create-step-head">
              <div className="step-marker">5</div>
              <div><h2 id="review-title">Review &amp; start</h2><p>Exactly what will be submitted — nothing else is sent.</p></div>
            </div>
            <div className="summary-card">
              <div className="summary-rows"><div><span>Genre pack</span><strong>{selectedGenre?.label || selectedGenre?.name || genreId}</strong></div><div><span>Narration voice</span><strong>{(configCatalogue ? productionConfig.narration.voice_id : null) || narrationVoiceId} · English (US)</strong></div><div><span>Captions</span><strong>{configCatalogue ? (productionConfig.captions.mode === "burn" ? "Burned in" : productionConfig.captions.mode === "sidecar" ? "Sidecar files" : "Off") : "Burned in"}</strong></div>{configCatalogue && <><div><span>Preset</span><strong>{productionConfig.preset === "custom" ? "Custom" : productionConfig.preset[0].toUpperCase() + productionConfig.preset.slice(1)}</strong></div><div><span>Motion graphics</span><strong>{productionConfig.motion_graphics.enabled ? productionConfig.motion_graphics.intensity[0].toUpperCase() + productionConfig.motion_graphics.intensity.slice(1) : "Off"}</strong></div><div><span>Visual theme</span><strong>{productionConfig.theme.id ? (productionConfig.theme.id === "custom" ? "Custom" : productionConfig.theme.id[0].toUpperCase() + productionConfig.theme.id.slice(1)) : "Genre pack default"}</strong></div><div><span>Music</span><strong>{!productionConfig.music.enabled ? "Off" : productionConfig.music.source === "upload" ? "Your upload (add it in the project's Configuration tab)" : productionConfig.music.source === "library" ? "Studio library" : "On — no track selected yet"}</strong></div></>}<div><span>Plan review</span><strong>{requirePlanApproval ? "Required before rendering" : "Not required"}</strong></div><div><span>Script length</span><strong>{words ? `${words.toLocaleString()} words · ${script.length.toLocaleString()} characters` : "Not added"}</strong></div><div><span>Estimated runtime</span><strong>{duration ? `About ${duration} min` : "—"}</strong></div><div><span>Visual sourcing</span><strong>Research-led</strong></div></div>
              <div className="summary-footnote"><Info size={18} strokeWidth={1.7} aria-hidden /><span>Runtime is an estimate. Final length depends on narration and edit.</span></div>
              <p className="status-message"><ApiConnectionBadge onStatus={setConnection} /></p>
              <button className="generate-button" type="submit" disabled={loading || !script.trim() || apiBlocked}>
                {loading ? <><span className="button-spinner" /> Starting…</> : <>Create and start <ArrowRight size={18} strokeWidth={1.7} aria-hidden /></>}
              </button>
              <p className="button-caption">You can follow each stage as your film comes together.</p>
            </div>
          </section>
        </form>

        <section className="status-card" aria-live="polite">
          <div className="status-heading"><span className="status-indicator" /><div><span className="eyebrow">PRODUCTION STATUS</span><h3>{statusTitle(status?.status, !!projectId, loading)}</h3></div></div>
          {projectId ? <>
            <p className="status-message">{message || "Waiting for production status…"}</p>
            {status?.status === "awaiting_approval" && <p className="status-message"><Link className="text-link" href={`/projects/${projectId}/plan`}>Review the production plan</Link></p>}
            {status && <StageProgress stages={status.stages} currentStage={status.current_stage} />}
            {projectId && <p className="status-message"><Link className="text-link" href={`/projects/${projectId}`}>Open the project workspace <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link></p>}
            <div className="project-reference"><span>PROJECT</span><code>{projectId.slice(0, 8)}…</code>{jobId && <><i /><span>JOB</span><code>{jobId.slice(0, 8)}…</code></>}</div>
          </> : <div className="empty-status"><p>Your production stages will appear here after you start.</p></div>}
        </section>

        {videoPath && <section className="video-card"><div className="video-heading"><span className="eyebrow">FINAL CUT</span><span className="ready-pill">READY</span></div><AuthenticatedVideo controls preload="metadata" src={videoPath} /><AuthenticatedDownloadLink path={videoPath} filename="documentary_final_output.mp4">Download MP4 <ArrowUpRight size={18} strokeWidth={1.7} aria-hidden /></AuthenticatedDownloadLink></section>}

        <footer className="page-footer"><span>CINEMORA</span><span>Production workspace</span></footer>
      </div>
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
