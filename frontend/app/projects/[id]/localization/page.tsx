"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { ProjectTabs } from "@/components/project-tabs";
import { ErrorState, LoadingState } from "@/components/states";
import { AuthenticatedDownloadLink } from "@/components/authenticated-media";
import {
  ArrowLeft,
  Captions,
  Download,
  FileText,
  Languages,
  Trash2,
} from "lucide-react";
import {
  ApiClientError,
  deleteLocalizationArtefact,
  getLocalization,
  localizeCaptions,
  localizeScript,
  localizationDownloadPath,
  type LocalizationArtefact,
  type LocalizationStudioView,
} from "@/lib/api";

/**
 * Localization Studio (founder priority #14) — sub-phase 1 of the
 * gap audit's phasing: this project's recorded captions and its
 * script, translated through the AI provider, with the English
 * narration retained. The page says exactly that, everywhere:
 * a translated caption file is not a dubbed film, and the
 * dubbing panel states why no dub exists (the installed
 * narration voices are English-only) instead of offering a
 * control that would fake one. Caption timings are the source's
 * own, preserved exactly; segments whose translation reads too
 * fast for their window are flagged with the measured rate —
 * flagged, never truncated.
 */

const DOWNLOAD_LABELS: Record<string, string> = {
  srt: "SRT subtitles",
  vtt: "WebVTT subtitles",
  json: "Caption document (JSON)",
  txt: "Script text",
};

function ArtefactCard({ projectId, artefact, onDelete }: {
  projectId: string;
  artefact: LocalizationArtefact;
  onDelete: (id: string) => void;
}) {
  const isCaptions = artefact.kind === "captions";
  return (
    <article className="loc-card">
      <div className="loc-card-body">
        <h3>
          {isCaptions ? <Captions size={16} strokeWidth={2} aria-hidden /> : <FileText size={16} strokeWidth={2} aria-hidden />}{" "}
          {artefact.language_name} {isCaptions ? "captions" : "script"}
        </h3>
        <p className="plan-meta">
          {isCaptions
            ? `${artefact.segment_count ?? 0} segments · source: ${
                artefact.source.kind === "editor_revision"
                  ? `editor revision ${artefact.source.revision_number ?? ""}`
                  : "production captions"
              } · timings identical to the source`
            : `${artefact.paragraph_count ?? 0} paragraphs · translated from the project script`}
          {" · "}created {new Date(artefact.created_at).toLocaleString()}
        </p>
        {artefact.warnings.length > 0 && (
          <div className="loc-warnings">
            <h4>Reading-speed flags ({artefact.warnings.length})</h4>
            <p className="plan-meta">
              These segments must be read faster than 25 characters per second inside
              their unchanged timing window — a mechanical measurement. The full
              translation is kept; nothing was shortened to fit.
            </p>
            <ul className="thumb-checks">
              {artefact.warnings.map((warning) => (
                <li key={warning.segment_index}>
                  Segment {warning.segment_index + 1} ({warning.start.toFixed(1)}s–{warning.end.toFixed(1)}s):{" "}
                  {warning.reason === "zero_duration_window"
                    ? "the source window has no duration"
                    : `${warning.characters_per_second?.toFixed(1)} characters/second`}{" "}
                  · {warning.translated_characters} characters (source: {warning.source_characters})
                </li>
              ))}
            </ul>
          </div>
        )}
        {!isCaptions && artefact.script_text && (
          <details className="loc-script-preview">
            <summary>Read the translated script</summary>
            <pre>{artefact.script_text}</pre>
          </details>
        )}
        {!isCaptions && artefact.script_draft_id && (
          <p className="plan-meta">
            This translation also lives in the Script Studio as an editable draft:{" "}
            <Link href="/scripts">open the Script Studio</Link> to revise it or send it
            to production.
          </p>
        )}
        <div className="thumb-card-actions">
          {artefact.downloads.map((kind) =>
            artefact.files_available[kind] ? (
              <AuthenticatedDownloadLink
                key={kind}
                path={localizationDownloadPath(projectId, artefact.id, kind)}
                filename={`${artefact.language}-${isCaptions ? "captions" : "script"}.${kind === "json" ? "json" : kind}`}
                className="button button-secondary"
              >
                <Download size={14} strokeWidth={2} aria-hidden /> {DOWNLOAD_LABELS[kind] ?? kind}
              </AuthenticatedDownloadLink>
            ) : (
              <span key={kind} className="plan-meta">
                {DOWNLOAD_LABELS[kind] ?? kind}: file no longer on disk
              </span>
            ),
          )}
          <button type="button" className="button button-quiet" onClick={() => onDelete(artefact.id)}>
            <Trash2 size={14} strokeWidth={2} aria-hidden /> Delete
          </button>
        </div>
      </div>
    </article>
  );
}

export default function LocalizationPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [view, setView] = useState<LocalizationStudioView | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);
  const [language, setLanguage] = useState("es");

  const load = useCallback(async () => {
    setError(null);
    try {
      setView(await getLocalization(projectId));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }, [projectId]);

  useEffect(() => {
    const timer = setTimeout(() => { void load(); }, 0);
    return () => clearTimeout(timer);
  }, [load]);

  async function runAction(fn: () => Promise<void>) {
    setActing(true);
    setActionError(null);
    try {
      await fn();
    } catch (err) {
      setActionError(err instanceof ApiClientError ? err.message : "The action failed.");
    } finally {
      setActing(false);
    }
  }

  async function onTranslateCaptions() {
    await runAction(async () => {
      await localizeCaptions(projectId, language);
      await load();
    });
  }

  async function onTranslateScript() {
    await runAction(async () => {
      await localizeScript(projectId, language);
      await load();
    });
  }

  async function onDelete(id: string) {
    await runAction(async () => {
      await deleteLocalizationArtefact(projectId, id);
      await load();
    });
  }

  const captionsRecorded = view?.source.captions.state === "recorded";
  const scriptRecorded = view?.source.script.state === "recorded";

  return (
    <main className="app-page localization-page">
      <AppNavigation />
      <ProjectTabs projectId={projectId} />
      <section className="plan-content">
        <Link href={`/projects/${projectId}`} className="editor-back">
          <ArrowLeft size={14} strokeWidth={2} aria-hidden /> Project
        </Link>
        <p className="eyebrow">LOCALIZATION</p>
        <h1>Localization</h1>
        <p className="plan-intro">
          Translated captions and script in another language — the narration stays English; these are text assets, not a dub.
        </p>

        {busy && <LoadingState className="plan-message" label="Loading the localization studio…" />}
        {!busy && error != null && <ErrorState error={error} onRetry={() => { setBusy(true); void load(); }} />}

        {view && (
          <>
            <section className="hub-panel" aria-label="Source material">
              <h2>Source material — as recorded</h2>
              <div className="plan-summary">
                <div>
                  <h3>Captions</h3>
                  <p>
                    {captionsRecorded
                      ? `${view.source.captions.segment_count} segments, from ${
                          view.source.captions.source === "editor_revision"
                            ? `editor revision ${view.source.captions.revision_number ?? ""}`
                            : "the production's caption file"
                        }`
                      : "Not recorded yet — captions are produced by a production run."}
                  </p>
                </div>
                <div>
                  <h3>Script</h3>
                  <p>
                    {scriptRecorded
                      ? `${view.source.script.word_count} words in ${view.source.script.paragraph_count} paragraphs`
                      : "No script text on this project."}
                  </p>
                </div>
                <div>
                  <h3>Narration</h3>
                  <p>English — retained in every localisation.</p>
                </div>
              </div>
            </section>

            <section className="hub-panel" aria-label="Translate">
              <h2>Translate</h2>
              {!view.ai_available && (
                <p className="plan-meta">
                  Translation is unavailable on this deployment: no AI provider is
                  configured. Nothing here will pretend to translate without one.
                </p>
              )}
              <div className="form-fields">
                <label>
                  Target language
                  <select value={language} onChange={(event) => setLanguage(event.target.value)}>
                    {view.languages.map((entry) => (
                      <option key={entry.code} value={entry.code}>{entry.name}</option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="thumb-actions">
                <button
                  type="button"
                  className="button button-primary"
                  onClick={() => void onTranslateCaptions()}
                  disabled={acting || !view.ai_available || !captionsRecorded}
                >
                  <Captions size={14} strokeWidth={2} aria-hidden /> Translate captions
                </button>
                <button
                  type="button"
                  className="button button-secondary"
                  onClick={() => void onTranslateScript()}
                  disabled={acting || !view.ai_available || !scriptRecorded}
                >
                  <Languages size={14} strokeWidth={2} aria-hidden /> Translate script
                </button>
              </div>
              {actionError && <p className="thumb-error" role="alert">{actionError}</p>}
            </section>

            <section className="hub-panel" aria-label="Dubbing">
              <h2>Dubbing — not available, and why</h2>
              <p className="hub-panel-note">{view.dubbing.reason}</p>
              <p className="plan-meta">
                Installed narration voices:{" "}
                {view.dubbing.voices.length
                  ? view.dubbing.voices
                      .map((voice) => `${voice.voice_id}${voice.installed ? "" : " (not installed)"}`)
                      .join(" · ")
                  : "none"}
              </p>
            </section>

            <section aria-label="Localised artefacts">
              <h2 className="thumb-variants-title">Localised artefacts ({view.artefacts.length})</h2>
              {view.artefacts.length === 0 ? (
                <p className="plan-meta">
                  Nothing localised yet. Pick a language above and translate the captions
                  or the script.
                </p>
              ) : (
                <div className="loc-grid">
                  {view.artefacts.map((artefact) => (
                    <ArtefactCard
                      key={artefact.id}
                      projectId={projectId}
                      artefact={artefact}
                      onDelete={(id) => void onDelete(id)}
                    />
                  ))}
                </div>
              )}
            </section>
          </>
        )}
      </section>
    </main>
  );
}
