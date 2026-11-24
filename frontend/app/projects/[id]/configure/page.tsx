"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { ProjectTabs } from "@/components/project-tabs";
import {
  ProductionConfigForm,
  emptyProductionConfig,
} from "@/components/production-config-form";
import { ErrorState, LoadingState } from "@/components/states";
import {
  ArrowLeft,
  CheckCircle2,
  FileAudio,
  History,
  Mic,
  Save,
  ShieldCheck,
  Sparkles,
  Trash2,
} from "lucide-react";
import {
  ApiClientError,
  applyVoiceScript,
  clearConfigDefaults,
  getConfigCatalogue,
  getConfigDefaults,
  getProjectConfig,
  getVoiceTranscript,
  removeConfigMusicUpload,
  saveConfigDefaults,
  saveProjectConfig,
  transcribeVoice,
  treatVoiceScript,
  uploadConfigFile,
  validateProjectConfig,
  type ConfigCatalogue,
  type ConfigValidation,
  type ProductionConfig,
  type ProjectConfigView,
  type VoiceTranscript,
} from "@/lib/api";

/**
 * The project's Pre-Production Configuration Studio (Phase 9,
 * §§13–17): the configuration this project will run with —
 * stored on the project, snapshotted onto every run at
 * submission, and validated before generation (§14.2/§15).
 * Nothing here is frontend-only: saving goes through the API,
 * which cleans, validates against the live system and projects
 * the settings onto the option keys the pipeline consumes.
 *
 * The voice-to-documentary panel (§13.4) turns an uploaded
 * recording into a reviewable script: transcription through the
 * provider interface, four conversion modes as proposals, and
 * an explicit apply that sets the project script and opens a
 * Script Studio draft.
 */

const TREATMENTS = [
  { id: "preserve", label: "Preserve", note: "The transcript verbatim." },
  { id: "clean", label: "Clean", note: "Fillers and false starts removed; wording untouched." },
  { id: "improve", label: "Improve", note: "Light clarity edits; meaning and facts unchanged." },
  { id: "expand", label: "Expand", note: "Transitions developed from what the speaker said — no new facts." },
] as const;

export default function ConfigurePage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [catalogue, setCatalogue] = useState<ConfigCatalogue | null>(null);
  const [view, setView] = useState<ProjectConfigView | null>(null);
  const [config, setConfig] = useState<ProductionConfig>(emptyProductionConfig());
  const [defaults, setDefaults] = useState<ProductionConfig | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [liveValidation, setLiveValidation] = useState<ConfigValidation | null>(null);

  const [transcript, setTranscript] = useState<VoiceTranscript | null>(null);
  const [reviewText, setReviewText] = useState("");
  const [voiceBusy, setVoiceBusy] = useState(false);
  const [voiceNotice, setVoiceNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [catalogueData, viewData, defaultsData] = await Promise.all([
      getConfigCatalogue(),
      getProjectConfig(projectId),
      getConfigDefaults(),
    ]);
    setCatalogue(catalogueData);
    setView(viewData);
    setConfig(structuredClone(viewData.config));
    setDefaults(defaultsData.defaults);
    try {
      const record = await getVoiceTranscript(projectId);
      setTranscript(record);
      setReviewText(record.text);
    } catch {
      setTranscript(null);
    }
  }, [projectId]);

  useEffect(() => {
    void (async () => {
      try {
        await load();
      } catch (loadError) {
        setError(
          loadError instanceof Error
            ? loadError.message
            : "Could not load the configuration studio.",
        );
      } finally {
        setBusy(false);
      }
    })();
  }, [load]);

  async function runAction(action: () => Promise<void>) {
    setError(null);
    setNotice(null);
    try {
      await action();
    } catch (actionError) {
      setError(
        actionError instanceof ApiClientError
          ? actionError.message
          : actionError instanceof Error
            ? actionError.message
            : "The action failed.",
      );
    }
  }

  async function onSave() {
    await runAction(async () => {
      const saved = await saveProjectConfig(projectId, config);
      setView(saved);
      setConfig(structuredClone(saved.config));
      setLiveValidation(null);
      setNotice(
        saved.validation.warnings.length
          ? "Configuration saved. Review the warning below before generating."
          : "Configuration saved — this is what the next run will use.",
      );
    });
  }

  async function onValidate() {
    await runAction(async () => {
      const result = await validateProjectConfig(projectId, config);
      setLiveValidation(result.validation);
      setNotice(
        result.validation.errors.length
          ? "Validation found problems that would refuse a run — see below."
          : "Validation passed: a run can start on this configuration.",
      );
    });
  }

  async function onSaveDefaults() {
    await runAction(async () => {
      const saved = await saveConfigDefaults(config);
      setDefaults(saved.defaults);
      setNotice("Saved as your default configuration for new projects.");
    });
  }

  async function onApplyDefaults() {
    if (!defaults) return;
    setConfig(structuredClone(defaults));
    setNotice("Your saved defaults are loaded — save to apply them to this project.");
  }

  async function onClearDefaults() {
    await runAction(async () => {
      await clearConfigDefaults();
      setDefaults(null);
      setNotice("Saved defaults cleared.");
    });
  }

  async function onMusicUpload(file: File) {
    await runAction(async () => {
      const result = await uploadConfigFile(projectId, "music", file);
      setView(result.config);
      setConfig(structuredClone(result.config.config));
      setNotice("Music track uploaded and selected as this project's music.");
    });
  }

  async function onRemoveMusic() {
    await runAction(async () => {
      const updated = await removeConfigMusicUpload(projectId);
      setView(updated);
      setConfig(structuredClone(updated.config));
      setNotice("Uploaded track deselected. The file itself stays as project media.");
    });
  }

  async function onVoiceUpload(file: File) {
    setVoiceBusy(true);
    setVoiceNotice(null);
    try {
      const result = await uploadConfigFile(projectId, "voice", file);
      setView(result.config);
      setConfig(structuredClone(result.config.config));
      setVoiceNotice("Recording uploaded. Transcribe it when ready.");
    } catch (uploadError) {
      setVoiceNotice(
        uploadError instanceof Error
          ? uploadError.message
          : "The upload failed.",
      );
    } finally {
      setVoiceBusy(false);
    }
  }

  async function onTranscribe() {
    setVoiceBusy(true);
    setVoiceNotice(null);
    try {
      const record = await transcribeVoice(projectId);
      setTranscript(record);
      setReviewText(record.text);
      setVoiceNotice(
        record.unclear_sections.length
          ? `Transcribed. ${record.unclear_sections.length} section(s) were flagged unclear — check them in the review text.`
          : "Transcribed. Review the text, choose a conversion, then apply.",
      );
    } catch (transcribeError) {
      setVoiceNotice(
        transcribeError instanceof Error
          ? transcribeError.message
          : "Transcription failed.",
      );
    } finally {
      setVoiceBusy(false);
    }
  }

  async function onTreat(mode: string) {
    if (!transcript) return;
    setVoiceBusy(true);
    setVoiceNotice(null);
    try {
      const result = await treatVoiceScript(projectId, mode, transcript.text);
      setReviewText(result.text);
      setVoiceNotice(
        mode === "preserve"
          ? "The review text is the transcript verbatim."
          : "Proposal loaded into the review text — nothing is applied until you apply it.",
      );
    } catch (treatError) {
      setVoiceNotice(
        treatError instanceof Error ? treatError.message : "Conversion failed.",
      );
    } finally {
      setVoiceBusy(false);
    }
  }

  async function onApplyScript() {
    setVoiceBusy(true);
    setVoiceNotice(null);
    try {
      const result = await applyVoiceScript(projectId, reviewText);
      setVoiceNotice(
        `Applied: the project script is now this text, and a Script Studio draft (${result.draft.id.slice(0, 8)}…) holds it for editing.`,
      );
    } catch (applyError) {
      setVoiceNotice(
        applyError instanceof Error ? applyError.message : "Applying failed.",
      );
    } finally {
      setVoiceBusy(false);
    }
  }

  const shownValidation = liveValidation ?? view?.validation ?? null;
  const recording = view?.config.narration.voice_recording ?? null;

  return (
    <main className="app-page configure-page">
      <AppNavigation />
      <ProjectTabs projectId={projectId} />
      <section className="plan-content">
        <Link href={`/projects/${projectId}`} className="editor-back">
          <ArrowLeft size={14} strokeWidth={2} aria-hidden /> Project
        </Link>
        <p className="eyebrow">PRE-PRODUCTION CONFIGURATION</p>
        <h1>Configuration</h1>
        <p className="plan-intro">
          What this production will run with — motion graphics, visual theme,
          music, narration voice and captions, set before generation and
          enforced by every pipeline stage. The configuration is stored on
          the project, snapshotted onto each run, and validated before a run
          can start: a setting the pipeline cannot honour refuses the run
          with the reason, rather than being silently ignored.
        </p>

        {busy && (
          <LoadingState
            className="plan-message"
            label="Loading the configuration studio…"
          />
        )}
        {!busy && error != null && (
          <ErrorState
            error={new Error(error)}
            onRetry={() => {
              setBusy(true);
              void load().finally(() => setBusy(false));
            }}
          />
        )}

        {catalogue && view && (
          <>
            <section className="hub-panel" aria-label="Production configuration">
              <h2>
                Production configuration
                {view.stored ? "" : " — not saved yet (defaults shown)"}
              </h2>
              <ProductionConfigForm
                catalogue={catalogue}
                value={config}
                onChange={setConfig}
                mode="project"
                onMusicUpload={(file) => void onMusicUpload(file)}
                onRemoveMusicUpload={() => void onRemoveMusic()}
              />
              <div className="p9-actions">
                <button type="button" className="generate-button" onClick={() => void onSave()}>
                  <Save size={16} strokeWidth={2} aria-hidden /> Save configuration
                </button>
                <button type="button" className="p9-secondary-button" onClick={() => void onValidate()}>
                  <ShieldCheck size={16} strokeWidth={2} aria-hidden /> Validate
                </button>
                <button type="button" className="p9-secondary-button" onClick={() => void onSaveDefaults()}>
                  Save as my defaults
                </button>
                {defaults && (
                  <>
                    <button type="button" className="p9-secondary-button" onClick={onApplyDefaults}>
                      Use my defaults
                    </button>
                    <button type="button" className="p9-secondary-button" onClick={() => void onClearDefaults()}>
                      <Trash2 size={15} strokeWidth={2} aria-hidden /> Clear defaults
                    </button>
                  </>
                )}
              </div>
              {notice && (
                <p className="plan-message" role="status">
                  <CheckCircle2 size={15} strokeWidth={2} aria-hidden /> {notice}
                </p>
              )}
              {shownValidation && (
                <div className="p9-validation" aria-live="polite">
                  {shownValidation.errors.map((item) => (
                    <p key={item.code} className="p9-validation-error">
                      <strong>Cannot run:</strong> {item.message}
                    </p>
                  ))}
                  {shownValidation.warnings.map((item) => (
                    <p key={item.code} className="p9-validation-warning">
                      <strong>Note:</strong> {item.message}
                    </p>
                  ))}
                </div>
              )}
              <p className="plan-meta">
                <History size={14} strokeWidth={2} aria-hidden /> Every run
                records the exact configuration it ran with — see the{" "}
                <Link className="text-link" href={`/projects/${projectId}/versions`}>
                  Versions tab
                </Link>{" "}
                for the per-run configuration history.
              </p>
            </section>

            <section className="hub-panel" aria-label="Voice to documentary">
              <h2>
                <Mic size={17} strokeWidth={2} aria-hidden /> Voice to documentary
              </h2>
              <p className="plan-meta">
                Upload a voice recording and turn it into this project&rsquo;s
                script: transcription first (with the sections the provider
                flagged unclear), then a conversion you review before
                anything is applied. The original recording stays as project
                media.
              </p>
              {recording ? (
                <p className="plan-meta">
                  <FileAudio size={15} strokeWidth={2} aria-hidden />{" "}
                  {recording.file_name || "Voice recording"}
                  {recording.duration_seconds
                    ? ` · ${recording.duration_seconds.toFixed(1)}s`
                    : ""}
                  {recording.codec ? ` · ${recording.codec}` : ""}
                </p>
              ) : (
                <p className="plan-meta">No recording uploaded yet.</p>
              )}
              <div className="p9-actions">
                <label className="p9-upload-button">
                  <Mic size={15} strokeWidth={2} aria-hidden />{" "}
                  {recording ? "Replace recording" : "Upload recording"}
                  <input
                    type="file"
                    accept="audio/*"
                    hidden
                    disabled={voiceBusy}
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) void onVoiceUpload(file);
                      event.target.value = "";
                    }}
                  />
                </label>
                <button
                  type="button"
                  className="p9-secondary-button"
                  disabled={voiceBusy || !recording}
                  onClick={() => void onTranscribe()}
                >
                  <Sparkles size={15} strokeWidth={2} aria-hidden /> Transcribe
                </button>
              </div>

              {transcript && (
                <>
                  <p className="plan-meta">
                    Transcribed by {transcript.provider}
                    {transcript.language
                      ? ` · language: ${transcript.language}`
                      : ""}
                    {transcript.unclear_sections.length
                      ? ` · unclear sections: ${transcript.unclear_sections
                          .map((i) => i + 1)
                          .join(", ")}`
                      : " · no unclear sections flagged"}
                    . {transcript.speaker_note}
                  </p>
                  <div className="p9-treatment-row" role="group" aria-label="Conversion mode">
                    {TREATMENTS.map((treatment) => (
                      <button
                        key={treatment.id}
                        type="button"
                        className="p9-chip"
                        disabled={voiceBusy}
                        title={treatment.note}
                        onClick={() => void onTreat(treatment.id)}
                      >
                        {treatment.label}
                      </button>
                    ))}
                  </div>
                  <label className="field-label" htmlFor="p9-review-text">
                    REVIEW — EDIT BEFORE APPLYING
                    <textarea
                      id="p9-review-text"
                      className="p9-review"
                      value={reviewText}
                      onChange={(event) => setReviewText(event.target.value)}
                      rows={10}
                    />
                  </label>
                  <div className="p9-actions">
                    <button
                      type="button"
                      className="generate-button"
                      disabled={voiceBusy || !reviewText.trim()}
                      onClick={() => void onApplyScript()}
                    >
                      Apply as project script
                    </button>
                  </div>
                </>
              )}
              {voiceNotice && (
                <p className="plan-message" role="status">{voiceNotice}</p>
              )}
            </section>
          </>
        )}
      </section>
    </main>
  );
}
