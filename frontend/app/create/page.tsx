"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { AppNavigation } from "@/components/app-navigation";
import {
  createProject as apiCreateProject,
  getBillingSummary,
  getConfigCatalogue,
  getConfigDefaults,
  getCurrentUser,
  getProjectStatus,
  getLanguageCatalogue,
  getVoiceCatalogue,
  submitRun,
  type ApiConnectionStatus,
  type BillingSummary,
  type ConfigCatalogue,
  type ProductionConfig,
  type ProjectStatus,
  type LanguageCatalogueEntry,
  type VoiceCatalogueEntry,
} from "@/lib/api";
import {
  ProductionConfigForm,
  emptyProductionConfig,
} from "@/components/production-config-form";
import { ApiConnectionBadge } from "@/components/connection-status";
import { clearDraft, loadDraft, saveDraft } from "@/lib/draft-storage";
import { StageProgress } from "@/components/stage-progress";
import { AuthenticatedDownloadLink, AuthenticatedVideo } from "@/components/authenticated-media";
import {
  ArrowRight,
  ArrowUpRight,
  Film,
  Info,
  Mic,
  Search,
} from "lucide-react";

// IA-06: the voice panel is rendered from the backend catalogue
// (GET /api/v1/catalogues/voices — the installed voice
// catalogue). The constant below is ONLY the honest fallback
// used when that endpoint cannot be reached: it states the same
// previously-static truthful facts (the primary installed
// Kokoro voice and its default alias), and the UI labels it as
// a fallback whenever it is shown — no option is ever silently
// invented.
// FS-2: there is no genre selection on this form — the
// documentary treatment (genre pack) is derived by the backend
// from the script, and is never a user choice.
const FALLBACK_VOICES: VoiceCatalogueEntry[] = [
  {
    id: "af_heart",
    is_default: false,
    alias_of: null,
    model: "kokoro-v1.0.onnx",
    model_file: "kokoro-v1.0.onnx",
    config_file: "voices-v1.0.bin",
    installed: true,
    language: "en",
    region: "English (US)",
    gender: "female",
  },
  {
    id: "default",
    is_default: true,
    alias_of: "af_heart",
    model: "kokoro-v1.0.onnx",
    model_file: "kokoro-v1.0.onnx",
    config_file: "voices-v1.0.bin",
    installed: true,
    language: "en",
    region: "English (US)",
    gender: "female",
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

// The primary narration voice actually installed in the backend
// (Stage 3J catalogue, Kokoro-82M since 2026-11-26): af_heart.
// IA-06: the voice panel below is rendered from
// GET /api/v1/catalogues/voices; this constant is only the
// fallback canonical id used when the catalogue cannot be
// reached (see FALLBACK_VOICES).
const NARRATION_VOICE_ID = "af_heart";

// The backend's real script cap (api_v1 MAX_SCRIPT_CHARS). The form
// previously enforced a lower, wrong limit and labelled a character
// counter as "words" (audit D-14).
const MAX_SCRIPT_CHARS = 60000;

// Redesign F1 (plan §3.3): the form is a stepped production
// brief — one page, sectioned, no wizard hiding state:
// 1 Script · 2 Story (voice from the real catalogue) ·
// 3 Visual approach (the plan-approval toggle) ·
// 4 Configuration (Phase 9: the pre-production configuration —
// presets, motion graphics, theme, music, voice, captions —
// seeded from the user's saved defaults and submitted as the
// project's production_config, which the backend cleans,
// validates and projects onto the consumed option keys) ·
// 5 Review & start (exactly what will be submitted + submit).
// Post-submit status with StageProgress + 3s polling is
// preserved verbatim.
const CREATE_DRAFT_KEY = "cinemora:create-draft:v1";

type CreateDraft = {
  script?: string;
  title?: string;
  requirePlanApproval?: boolean;
  productionConfig?: ProductionConfig;
};

export default function Home() {
  // Phase 6 (A-05): restore the local draft (if any) in the state
  // initialisers, so a refresh never destroys a script again. The
  // catalogue/defaults seed below must not overwrite a restored
  // productionConfig — it checks `restoredDraftRef` first.
  const restoredDraftRef = useRef<CreateDraft | null>(null);
  if (restoredDraftRef.current === null) {
    restoredDraftRef.current = loadDraft<CreateDraft>(CREATE_DRAFT_KEY) ?? {};
  }
  const restored = restoredDraftRef.current;
  const [draftRestored, setDraftRestored] = useState<boolean>(
    Boolean(restored.script || restored.title),
  );
  const [script, setScript] = useState(restored.script ?? "");
  const [title, setTitle] = useState(restored.title ?? "");
  const [voices, setVoices] = useState<VoiceCatalogueEntry[]>(FALLBACK_VOICES);
  // Item 6: the production language, chosen here — BEFORE
  // generation — from the backend language catalogue. The
  // fallback states the one honest fact (English narration)
  // when the catalogue cannot be reached.
  const [languages, setLanguages] = useState<LanguageCatalogueEntry[]>([
    {
      code: "en",
      name: "English",
      narration_supported: true,
      voices: [],
      script_supported: true,
      translation_supported: true,
    },
  ]);
  const [language, setLanguage] = useState("en");
  const selectedLanguage =
    languages.find((entry) => entry.code === language) || languages[0];
  const narrationBlocked = Boolean(
    selectedLanguage && !selectedLanguage.narration_supported,
  );
  const [catalogueState, setCatalogueState] = useState<"loading" | "api" | "fallback">("loading");
  const [requirePlanApproval, setRequirePlanApproval] = useState(restored.requirePlanApproval ?? false);
  const [loading, setLoading] = useState(false);
  const [projectId, setProjectId] = useState("");
  const [jobId, setJobId] = useState("");
  const [status, setStatus] = useState<ProjectStatus | null>(null);
  const [message, setMessage] = useState("");
  const [connection, setConnection] = useState<ApiConnectionStatus | null>(null);
  // Phase 9: the pre-production configuration document. Seeded
  // from the user's saved defaults when they exist; submitted
  // as options.production_config.
  // FS-G (FS-12): the Visual Plan is an admin-only surface
  // (FS-C), so the post-submit "Review the production plan" link
  // in the status card below renders only for admin principals —
  // the same /auth/me role source the dashboard uses. Ordinary
  // users follow the run from the project Overview instead (the
  // "Open the project workspace" link is always shown).
  const [isAdmin, setIsAdmin] = useState(false);
  const [configCatalogue, setConfigCatalogue] = useState<ConfigCatalogue | null>(null);
  const [productionConfig, setProductionConfig] = useState<ProductionConfig>(emptyProductionConfig());
  const [configSeeded, setConfigSeeded] = useState(false);
  // Phase 8 (§9): the billing summary drives the pre-Generate
  // cost estimate in the summary card. Absence (legacy plan /
  // load failure) simply omits the row — never an invented cost.
  const [billing, setBilling] = useState<BillingSummary | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getBillingSummary(controller.signal)
      .then(setBilling)
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  const words = useMemo(() => script.trim().split(/\s+/).filter(Boolean).length, [script]);
  const duration = words ? Math.max(1, Math.round(words / 145)) : 0;
  const isTerminal = status?.status === "completed" || status?.status === "failed";

  // IA-06: load the voice catalogue from the API.
  // (IA-10 / F-04: the sessionStorage draft read that used to sit
  // here is removed — nothing in the active build ever writes
  // that key; its only setter lives in preserved/ code.)
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const voiceData = await getVoiceCatalogue(controller.signal);
          if (controller.signal.aborted) return;
          if (voiceData.voices?.length) setVoices(voiceData.voices);
          // Item 6: the language catalogue rides the same load;
          // its failure leaves the English-only fallback above.
          try {
            const languageData = await getLanguageCatalogue(
              controller.signal,
            );
            if (
              !controller.signal.aborted &&
              languageData.languages?.length
            ) {
              setLanguages(languageData.languages);
            }
          } catch {
            // Fallback facts stay in place (English only).
          }
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
            const restoredConfig = restoredDraftRef.current?.productionConfig;
            const seed = restoredConfig
              ? structuredClone(restoredConfig)
              : defaultsData.defaults
                ? structuredClone(defaultsData.defaults)
                : emptyProductionConfig();
            if (restoredConfig) setDraftRestored(true);
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

  // Phase 6 (A-05): persist the draft as the user types
  // (debounced). An all-empty form removes the key instead of
  // storing an empty draft. saveDraft never throws.
  useEffect(() => {
    const timer = setTimeout(() => {
      if (!script && !title) {
        clearDraft(CREATE_DRAFT_KEY);
        return;
      }
      saveDraft(CREATE_DRAFT_KEY, {
        script,
        title,
        requirePlanApproval,
        productionConfig,
      } satisfies CreateDraft);
    }, 400);
    return () => clearTimeout(timer);
  }, [script, title, requirePlanApproval, productionConfig]);

  function discardDraft() {
    clearDraft(CREATE_DRAFT_KEY);
    restoredDraftRef.current = {};
    setDraftRestored(false);
    setScript("");
    setTitle("");
    setRequirePlanApproval(false);
    setProductionConfig(emptyProductionConfig());
  }

  // FS-G (FS-12): load the account role once (see isAdmin above).
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

  const canonicalVoice = voices.find((voice) => !voice.alias_of) || voices[0];
  const narrationVoiceId = canonicalVoice?.id || NARRATION_VOICE_ID;

  // The voices that genuinely speak the selected language, with
  // the labels the backend composed for them (region, name,
  // gender) from the language catalogue; installed/alias facts
  // are joined from the voice catalogue by id. When the language
  // catalogue carries no voice rows (the labelled fallback
  // state), the rows are derived from the voice catalogue's own
  // per-voice language instead — never from a hardcoded list.
  const voiceById = new Map(voices.map((voice) => [voice.id, voice]));
  const languageVoiceRows = (selectedLanguage?.voices ?? []).map(
    (languageVoice) => {
      const entry = voiceById.get(languageVoice.id);
      return {
        id: languageVoice.id,
        label: languageVoice.label,
        aliasOf: entry?.alias_of ?? null,
        isDefault: entry?.is_default ?? languageVoice.id === "default",
        installed: entry?.installed ?? true,
        model: entry?.model ?? null,
      };
    },
  );
  if (languageVoiceRows.length === 0 && !narrationBlocked) {
    for (const voice of voices) {
      if (voice.language !== language) continue;
      languageVoiceRows.push({
        id: voice.id,
        label: [voice.region, voice.gender ? `(${voice.gender})` : ""]
          .filter(Boolean)
          .join(" "),
        aliasOf: voice.alias_of ?? null,
        isDefault: voice.is_default,
        installed: voice.installed,
        model: voice.model ?? null,
      });
    }
  }
  // The selected language's primary voice: the first real
  // (non-alias) row, in the catalogue order the backend's own
  // per-language default uses.
  const primaryVoiceId =
    languageVoiceRows.find((row) => !row.aliasOf && !row.isDefault)?.id ??
    languageVoiceRows[0]?.id ??
    null;
  // The voice this form will actually submit: the
  // configuration's chosen voice when there is one, otherwise
  // the selected language's primary.
  const submittedVoiceId =
    productionConfig.narration.voice_id || primaryVoiceId || narrationVoiceId;
  // The engine label for a voice row, from the configuration
  // catalogue's provider or the voice's own model file — the
  // retired engine's name is never printed.
  const engineLabel = (model: string | null) =>
    configCatalogue?.narration.provider === "kokoro" ||
    (model ?? "").includes("kokoro")
      ? "Kokoro-82M"
      : "Built-in";
  // The languages with installed voices, from the catalogue
  // data — the blocked-language notice below is composed from
  // this, never from a hardcoded coverage claim.
  const supportedLanguageNames = languages
    .filter((entry) => entry.narration_supported)
    .map((entry) => entry.name)
    .join(", ");

  // Keep the configuration's voice valid for the selected
  // language: the catalogue seed names the English primary once,
  // so when the Step-2 language changes, move the selection to
  // that language's primary voice (and record the language in
  // the configuration — the Step-4 picker filters by it). A
  // voice the user picked that genuinely speaks the selected
  // language is left untouched. Without this, every non-English
  // production submitted through the default path paired the
  // language with the seeded English voice and was refused with
  // voice_language_mismatch.
  useEffect(() => {
    if (!configSeeded) return;
    setProductionConfig((previous) => {
      const current = previous.narration.voice_id;
      const stillValid =
        current !== null &&
        languageVoiceRows.some((row) => row.id === current);
      if (stillValid && previous.narration.language === language) {
        return previous;
      }
      return {
        ...previous,
        narration: {
          ...previous.narration,
          language,
          voice_id: stillValid ? current : primaryVoiceId,
        },
      };
    });
    // languageVoiceRows / primaryVoiceId are derived from
    // `voices` + `language` above; the effect re-runs when
    // either input changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [language, voices, configSeeded]);



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
      // the consumed keys of ProjectOptionsIn — voice_id
      // (narration), captions (burn/off) and
      // require_plan_approval (the plan gate). FS-2: no genre is
      // sent — the genre pack is derived by the backend from the
      // script, never chosen here. The music, sfx,
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
        options: {
          // Item 6: the language chosen in step 2 — the backend
          // stores it in the configuration document as the
          // project's authoritative language. The flat voice is
          // the voice valid for that language (the
          // configuration's selection, kept in step with the
          // language by the re-seed effect above) — never the
          // English primary paired with another language.
          language,
          voice_id: submittedVoiceId,
          captions: configCatalogue
            ? productionConfig.captions.mode
            : "burn",
          require_plan_approval: isAdmin ? requirePlanApproval : false,
          ...(configCatalogue
            ? {
                production_config: {
                  ...productionConfig,
                  narration: {
                    ...productionConfig.narration,
                    language,
                  },
                },
              }
            : {}),
        },
      });

      // A-05: the draft's job is done once the project exists.
      clearDraft(CREATE_DRAFT_KEY);
      setDraftRestored(false);
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
              {draftRestored && (
                <p className="draft-restored" role="status">
                  Draft restored from this browser.{" "}
                  <button type="button" className="text-link draft-discard" onClick={discardDraft}>
                    Discard draft
                  </button>
                </p>
              )}
              <label className="sr-only" htmlFor="documentary-script">Documentary script</label>
              <textarea id="documentary-script" value={script} onChange={(event) => setScript(event.target.value)} maxLength={MAX_SCRIPT_CHARS} placeholder="Paste script…" />
              <div className="script-meta"><span className="editor-hint">{words.toLocaleString()} words</span><span className="count">{script.length.toLocaleString()} / {MAX_SCRIPT_CHARS.toLocaleString()} characters</span></div>
              {script.length >= MAX_SCRIPT_CHARS && <p className="voice-note" role="status">Your script is at the {MAX_SCRIPT_CHARS.toLocaleString()}-character limit — the production API accepts at most {MAX_SCRIPT_CHARS.toLocaleString()} characters, and nothing beyond the limit is kept.</p>}
            </div>
          </section>

          <section className="create-step" aria-labelledby="story-title">
            <div className="create-step-head">
              <div className="step-marker">2</div>
              <div><h2 id="story-title">Story</h2><p>Title and narration voice.</p></div>
            </div>
            <div className="form-fields">
              <label className="field-label" htmlFor="project-title">TITLE
                <input id="project-title" className="thumbnail-input" value={title} onChange={(event) => setTitle(event.target.value)} maxLength={300} placeholder="e.g. The Yangtze Incident" />
              </label>
              <div className="field-label length-field"><span>ESTIMATED RUNTIME</span><div className="runtime-value">{duration ? `~${duration} min` : "Add a script"}</div><small>Based on about 145 spoken words per minute.</small></div>
            </div>
            <div className="form-fields">
              <label className="field-label" htmlFor="project-language">LANGUAGE
                <select id="project-language" className="thumbnail-input" value={language} onChange={(event) => setLanguage(event.target.value)}>
                  {languages.map((entry) => (
                    <option key={entry.code} value={entry.code}>
                      {entry.name}
                      {entry.narration_supported ? "" : " — narration not available yet"}
                    </option>
                  ))}
                </select>
                <small>The production language, chosen before generation. Voices below are the ones that genuinely speak it.</small>
              </label>
            </div>
            {narrationBlocked ? (
              <p className="voice-note" role="status">
                Narration is not available in {selectedLanguage?.name} yet: no installed voice speaks {selectedLanguage?.name}. Cinemora&apos;s installed voices cover {supportedLanguageNames}, and a {selectedLanguage?.name} production would otherwise be another language&apos;s audio under a false label — so generation cannot start in this language. You can generate in one of the supported languages and add translated {selectedLanguage?.name} captions and script afterwards in Localization, or choose a supported language here.
              </p>
            ) : (
              <div className="voice-catalog-list">{languageVoiceRows.map((voice) => <div className="voice-catalog-row" key={voice.id}><div><strong>{voice.id}</strong><small>{voice.aliasOf ? `Default alias of ${voice.aliasOf} · Same installed ${engineLabel(voice.model)} model` : `${voice.label} · ${engineLabel(voice.model)} voice`} · {voice.installed ? "Model installed" : "Model not installed"}</small></div>{voice.id === submittedVoiceId && <span>In use</span>}{voice.isDefault && <span>Default</span>}</div>)}</div>
            )}
            {catalogueState === "fallback" && <p className="voice-note catalogue-note" role="status">The voice catalogue could not be reached, so this form is showing the fallback facts — the one installed voice listed above — labelled as a fallback. Nothing else has been added.</p>}
            {catalogueState === "loading" && <p className="voice-note catalogue-note" role="status">Loading the voice catalogue from the studio API…</p>}
            {!narrationBlocked && <p className="voice-note">Narration language: {selectedLanguage?.name} — the language of the voices above.</p>}
            <div className="workflow-includes"><span className="include-label">INCLUDED IN YOUR WORKFLOW</span><div className="include-chips"><span><Search size={18} strokeWidth={1.7} aria-hidden /> Story research</span><span><Film size={18} strokeWidth={1.7} aria-hidden /> Matched visuals</span><span><Mic size={18} strokeWidth={1.7} aria-hidden /> Narration + captions</span></div></div>
          </section>

          {isAdmin && (
          <section className="create-step" aria-labelledby="visual-title">
            <div className="create-step-head">
              <div className="step-marker">3</div>
              <div><h2 id="visual-title">Visual approach</h2><p>Options the pipeline actually applies.</p></div>
            </div>
            <label className="motion-toggle"><input type="checkbox" checked={requirePlanApproval} onChange={(event) => setRequirePlanApproval(event.target.checked)} /><span><strong>Review plan before rendering</strong><small>Production pauses after story analysis — you approve the per-sentence plan before any footage is retrieved or rendered.</small></span></label>
          </section>
          )}

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
              <div className="summary-rows"><div><span>Language</span><strong>{selectedLanguage?.name || "English"}</strong></div><div><span>Narration voice</span><strong>{submittedVoiceId} · {selectedLanguage?.name || "English"}</strong></div><div><span>Captions</span><strong>{configCatalogue ? (productionConfig.captions.mode === "burn" ? "Burned in" : productionConfig.captions.mode === "sidecar" ? "Sidecar files" : "Off") : "Burned in"}</strong></div>{configCatalogue && <><div><span>Preset</span><strong>{productionConfig.preset === "custom" ? "Custom" : productionConfig.preset[0].toUpperCase() + productionConfig.preset.slice(1)}</strong></div><div><span>Motion graphics</span><strong>{productionConfig.motion_graphics.enabled ? "On" : "Off"}</strong></div><div><span>Visual theme</span><strong>{productionConfig.theme.id ? (productionConfig.theme.id === "custom" ? "Custom" : productionConfig.theme.id[0].toUpperCase() + productionConfig.theme.id.slice(1)) : "Genre pack default"}</strong></div><div><span>Music</span><strong>{!productionConfig.music.enabled ? "Off" : productionConfig.music.source === "upload" ? "Your upload (add it in the project's Configuration tab)" : productionConfig.music.source === "library" ? "Studio library" : "On — no track selected yet"}</strong></div></>}{isAdmin && <div><span>Plan review</span><strong>{requirePlanApproval ? "Required before rendering" : "Not required"}</strong></div>}<div><span>Script length</span><strong>{words ? `${words.toLocaleString()} words · ${script.length.toLocaleString()} characters` : "Not added"}</strong></div><div><span>Estimated runtime</span><strong>{duration ? `About ${duration} min` : "—"}</strong></div>{billing?.plan?.billing_mode === "metered" && duration > 0 && (() => { const perMinute = billing.plan.generation_credits_per_minute ?? billing.credit_pricing.plans[billing.plan.name]?.credits_per_minute ?? billing.credit_pricing.default_credits_per_minute; const estCredits = Math.ceil(duration * perMinute); const slots = billing.period?.generations.remaining ?? null; const label = slots === 0 ? "Monthly video limit reached" : `${estCredits} credits${slots !== null ? ` · 1 of your ${billing.period?.generations.granted ?? ""} monthly videos` : ""}`; return <div><span>Generation cost (estimate)</span><strong>{label}</strong></div>; })()}<div><span>Visual sourcing</span><strong>Research-led</strong></div></div>
              <div className="summary-footnote"><Info size={18} strokeWidth={1.7} aria-hidden /><span>Runtime is an estimate. Final length depends on narration and edit.</span></div>
              <p className="status-message"><ApiConnectionBadge onStatus={setConnection} /></p>
              <button className="generate-button" type="submit" disabled={loading || !script.trim() || apiBlocked || narrationBlocked}>
                {loading ? <><span className="button-spinner" /> Starting…</> : <>Create and start <ArrowRight size={18} strokeWidth={1.7} aria-hidden /></>}
              </button>
              {narrationBlocked ? (
                <p className="button-caption">Generation is unavailable in {selectedLanguage?.name} because no installed voice speaks it yet — choose one of the supported languages ({supportedLanguageNames}) to start, or generate in a supported language and translate the captions and script afterwards.</p>
              ) : (
                <p className="button-caption">You can follow each stage as your film comes together.</p>
              )}
            </div>
          </section>
        </form>

        <section className="status-card" aria-live="polite">
          <div className="status-heading"><span className="status-indicator" /><div><span className="eyebrow">PRODUCTION STATUS</span><h3>{statusTitle(status?.status, !!projectId, loading)}</h3></div></div>
          {projectId ? <>
            <p className="status-message">{message || "Waiting for production status…"}</p>
            {status?.status === "awaiting_approval" && isAdmin && <p className="status-message"><Link className="text-link" href={`/projects/${projectId}/plan`}>Review the production plan</Link></p>}
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
