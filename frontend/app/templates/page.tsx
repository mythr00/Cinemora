"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { ErrorState, LoadingState } from "@/components/states";
import {
  Copy,
  FileText,
  Film,
  LayoutTemplate,
  Plus,
  Save,
  Trash2,
  X,
} from "lucide-react";
import {
  applyTemplateToDraft,
  applyTemplateToProject,
  copyBuiltinTemplate,
  createTemplate,
  createTemplateFromDraft,
  createTemplateFromProject,
  deleteTemplate,
  getGenreCatalogue,
  getVoiceCatalogue,
  listProjects,
  listScriptDrafts,
  listTemplates,
  updateTemplate,
  type GenreCatalogueEntry,
  type ProjectSummary,
  type ScriptDraft,
  type TemplateDroppedOption,
  type TemplateOptions,
  type TemplateRecord,
  type VoiceCatalogueEntry,
} from "@/lib/api";

/**
 * Templates & production presets (founder priority #15): a
 * template is a starting configuration — a category, an optional
 * target length, a script section structure (headings only) and a
 * bundle of production options. Built-ins are derived from the
 * real genre packs and are read-only; user templates are saved
 * from scratch, from an existing project, or from a Script Studio
 * draft's structure. Every option the form offers is one the
 * production pipeline actually consumes — the backend refuses
 * anything else, and there is no narration-speed setting anywhere
 * (retired by founder ruling). Applying a template goes through
 * the normal creation paths, with their validation unchanged.
 */

type OptionForm = {
  genre: string;
  voice_id: string;
  captions: string;
  caption_sync: string;
  music_file: string;
  music_enabled: string; // "" | "true" | "false"
  require_plan_approval: string; // "" | "true" | "false"
  music_volume: string;
  narration_volume: string;
  music_fade_in_seconds: string;
  music_fade_out_seconds: string;
  music_loop_crossfade_seconds: string;
  duck_threshold: string;
  duck_ratio: string;
  duck_attack_ms: string;
  duck_release_ms: string;
};

const EMPTY_FORM: OptionForm = {
  genre: "",
  voice_id: "",
  captions: "",
  caption_sync: "",
  music_file: "",
  music_enabled: "",
  require_plan_approval: "",
  music_volume: "",
  narration_volume: "",
  music_fade_in_seconds: "",
  music_fade_out_seconds: "",
  music_loop_crossfade_seconds: "",
  duck_threshold: "",
  duck_ratio: "",
  duck_attack_ms: "",
  duck_release_ms: "",
};

const NUMERIC_FIELDS: { key: keyof OptionForm; label: string; hint: string }[] = [
  { key: "music_volume", label: "Music volume", hint: "0 to 2" },
  { key: "narration_volume", label: "Narration volume", hint: "0 to 2" },
  { key: "music_fade_in_seconds", label: "Music fade in (seconds)", hint: "0 to 30" },
  { key: "music_fade_out_seconds", label: "Music fade out (seconds)", hint: "0 to 30" },
  { key: "music_loop_crossfade_seconds", label: "Music loop crossfade (seconds)", hint: "0 to 10" },
  { key: "duck_threshold", label: "Ducking threshold", hint: "Above 0, up to 1" },
  { key: "duck_ratio", label: "Ducking ratio", hint: "1 to 100" },
  { key: "duck_attack_ms", label: "Ducking attack (ms)", hint: "0 to 10000" },
  { key: "duck_release_ms", label: "Ducking release (ms)", hint: "0 to 60000" },
];

const DROP_REASONS: Record<string, string> = {
  retired_option: "a retired setting that no longer exists",
  not_consumed_by_pipeline: "stored on the project but not used by the production pipeline",
  invalid_value: "a value the pipeline no longer accepts",
};

function formFromOptions(options: TemplateOptions): OptionForm {
  const num = (value: number | undefined) =>
    value === undefined || value === null ? "" : String(value);
  const bool = (value: boolean | undefined) =>
    value === undefined || value === null ? "" : value ? "true" : "false";
  return {
    genre: options.genre ?? "",
    voice_id: options.voice_id ?? "",
    captions: options.captions ?? "",
    caption_sync: options.caption_sync ?? "",
    music_file: options.music_file ?? "",
    music_enabled: bool(options.music_enabled),
    require_plan_approval: bool(options.require_plan_approval),
    music_volume: num(options.music_volume),
    narration_volume: num(options.narration_volume),
    music_fade_in_seconds: num(options.music_fade_in_seconds),
    music_fade_out_seconds: num(options.music_fade_out_seconds),
    music_loop_crossfade_seconds: num(options.music_loop_crossfade_seconds),
    duck_threshold: num(options.duck_threshold),
    duck_ratio: num(options.duck_ratio),
    duck_attack_ms: num(options.duck_attack_ms),
    duck_release_ms: num(options.duck_release_ms),
  };
}

function optionsFromForm(form: OptionForm): TemplateOptions {
  const options: TemplateOptions = {};
  if (form.genre) options.genre = form.genre;
  if (form.voice_id) options.voice_id = form.voice_id;
  if (form.captions) options.captions = form.captions as TemplateOptions["captions"];
  if (form.caption_sync) {
    options.caption_sync = form.caption_sync as TemplateOptions["caption_sync"];
  }
  if (form.music_file.trim()) options.music_file = form.music_file.trim();
  if (form.music_enabled) options.music_enabled = form.music_enabled === "true";
  if (form.require_plan_approval) {
    options.require_plan_approval = form.require_plan_approval === "true";
  }
  for (const { key } of NUMERIC_FIELDS) {
    const raw = form[key].trim();
    if (!raw) continue;
    const value = Number(raw);
    if (!Number.isFinite(value)) {
      throw new Error(`The ${key.replaceAll("_", " ")} value is not a number.`);
    }
    (options as Record<string, unknown>)[key] = value;
  }
  return options;
}

function optionSummary(template: TemplateRecord): string[] {
  const parts: string[] = [];
  const options = template.options || {};
  if (options.genre) parts.push(`Genre pack: ${options.genre}`);
  if (options.voice_id) parts.push(`Voice: ${options.voice_id}`);
  if (options.captions) parts.push(`Captions: ${options.captions}`);
  if (options.caption_sync) parts.push(`Caption sync: ${options.caption_sync}`);
  if (options.music_enabled !== undefined) {
    parts.push(options.music_enabled ? "Music on" : "Music off");
  }
  if (options.music_file) parts.push(`Music file: ${options.music_file}`);
  if (options.require_plan_approval) parts.push("Plan approval required");
  const numericCount = NUMERIC_FIELDS.filter(
    ({ key }) => (options as Record<string, unknown>)[key] !== undefined,
  ).length;
  if (numericCount > 0) parts.push(`${numericCount} mix setting${numericCount === 1 ? "" : "s"}`);
  return parts;
}

export default function TemplatesPage() {
  const [builtins, setBuiltins] = useState<TemplateRecord[]>([]);
  const [mine, setMine] = useState<TemplateRecord[]>([]);
  const [genres, setGenres] = useState<GenreCatalogueEntry[]>([]);
  const [voices, setVoices] = useState<VoiceCatalogueEntry[]>([]);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [drafts, setDrafts] = useState<ScriptDraft[]>([]);

  const [busy, setBusy] = useState(true);
  const [working, setWorking] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");

  // Editor state: "new" for a blank template, otherwise the id of
  // the user template being edited.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [category, setCategory] = useState("");
  const [target, setTarget] = useState("");
  const [form, setForm] = useState<OptionForm>(EMPTY_FORM);
  const [headings, setHeadings] = useState<string[]>([]);

  // Save-from state.
  const [fromProjectId, setFromProjectId] = useState("");
  const [fromDraftId, setFromDraftId] = useState("");
  const [dropped, setDropped] = useState<TemplateDroppedOption[] | null>(null);

  // Apply state.
  const [applyTarget, setApplyTarget] = useState<TemplateRecord | null>(null);
  const [applyTitle, setApplyTitle] = useState("");
  const [applyScript, setApplyScript] = useState("");
  const [createdProjectId, setCreatedProjectId] = useState<string | null>(null);
  const [createdDraftId, setCreatedDraftId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [list, genreData, voiceData, projectData, draftData] = await Promise.all([
        listTemplates(),
        getGenreCatalogue().catch(() => ({ genres: [] as GenreCatalogueEntry[] })),
        getVoiceCatalogue().catch(() => ({
          voices: [] as VoiceCatalogueEntry[],
          default_voice_id: "",
        })),
        listProjects().catch(() => ({ projects: [] as ProjectSummary[] })),
        listScriptDrafts().catch(() => ({ scripts: [] as ScriptDraft[] })),
      ]);
      setBuiltins(list.builtins || []);
      setMine(list.templates || []);
      setGenres(genreData.genres || []);
      setVoices(voiceData.voices || []);
      setProjects(projectData.projects || []);
      setDrafts(draftData.scripts || []);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => {
      void load();
    }, 0);
    return () => clearTimeout(timer);
  }, [load]);

  const run = async (label: string, action: () => Promise<void>) => {
    setWorking(label);
    setError(null);
    setNotice("");
    try {
      await action();
    } catch (caught) {
      setError(caught);
    } finally {
      setWorking("");
    }
  };

  const openEditor = (template: TemplateRecord | null) => {
    if (template === null) {
      setEditingId("new");
      setName("");
      setCategory("");
      setTarget("");
      setForm(EMPTY_FORM);
      setHeadings([]);
      return;
    }
    setEditingId(template.id);
    setName(template.name);
    setCategory(template.category);
    setTarget(template.target_minutes === null ? "" : String(template.target_minutes));
    setForm(formFromOptions(template.options || {}));
    setHeadings((template.sections || []).map((section) => section.heading || ""));
  };

  const saveEditor = () =>
    run("save", async () => {
      const options = optionsFromForm(form);
      const sections = headings
        .map((heading) => heading.trim())
        .filter(Boolean)
        .map((heading) => ({ heading }));
      const targetMinutes = target.trim() ? Number(target) : null;
      if (target.trim() && !Number.isFinite(targetMinutes)) {
        throw new Error("Target length must be a number of minutes.");
      }
      if (editingId === "new") {
        const created = await createTemplate({
          name: name.trim() || undefined,
          category: category.trim() || undefined,
          target_minutes: targetMinutes,
          options,
          sections,
        });
        setNotice(`Template "${created.name}" saved.`);
        await load();
        openEditor(created);
      } else if (editingId) {
        const updated = await updateTemplate(editingId, {
          name: name.trim() || undefined,
          category: category.trim() || undefined,
          target_minutes: targetMinutes,
          clear_target: targetMinutes === null,
          options,
          sections,
        });
        setNotice(`Template "${updated.name}" updated.`);
        await load();
        openEditor(updated);
      }
    });

  const removeTemplate = (template: TemplateRecord) =>
    run(`delete-${template.id}`, async () => {
      await deleteTemplate(template.id);
      if (editingId === template.id) setEditingId(null);
      if (applyTarget?.id === template.id) setApplyTarget(null);
      setNotice(`Template "${template.name}" deleted.`);
      await load();
    });

  const copyBuiltin = (template: TemplateRecord) =>
    run(`copy-${template.id}`, async () => {
      const packId = template.source?.pack_id;
      if (!packId) return;
      const created = await copyBuiltinTemplate(packId);
      setNotice(
        `Copied "${template.name}" into your templates — it is yours to edit now.`,
      );
      await load();
      openEditor(created);
    });

  const saveFromProject = () =>
    run("from-project", async () => {
      if (!fromProjectId) return;
      const result = await createTemplateFromProject(fromProjectId);
      setDropped(result.dropped_options || []);
      setNotice(
        `Template "${result.template.name}" saved from the project. Only settings the pipeline uses were kept.`,
      );
      await load();
      openEditor(result.template);
    });

  const saveFromDraft = () =>
    run("from-draft", async () => {
      if (!fromDraftId) return;
      const created = await createTemplateFromDraft(fromDraftId);
      setDropped(null);
      setNotice(
        `Template "${created.name}" saved from the draft's structure — headings only, never the draft's words.`,
      );
      await load();
      openEditor(created);
    });

  const openApply = (template: TemplateRecord) => {
    setApplyTarget(template);
    setApplyTitle(template.name);
    setApplyScript("");
    setCreatedProjectId(null);
    setCreatedDraftId(null);
  };

  const applyDraft = () =>
    run("apply-draft", async () => {
      if (!applyTarget) return;
      const draft = await applyTemplateToDraft(
        applyTarget.id,
        applyTitle.trim() || undefined,
      );
      setCreatedDraftId(draft.id);
      setNotice(`Draft "${draft.title}" created in the Script Studio.`);
    });

  const applyProject = () =>
    run("apply-project", async () => {
      if (!applyTarget) return;
      const project = await applyTemplateToProject(applyTarget.id, {
        title: applyTitle.trim() || undefined,
        script: applyScript,
      });
      setCreatedProjectId(project.id);
      setNotice("Project created through the normal creation path.");
    });

  const setField = (key: keyof OptionForm, value: string) =>
    setForm((current) => ({ ...current, [key]: value }));

  return (
    <main className="app-page templates-page">
      <AppNavigation />
      <section className="templates-content">
        <p className="eyebrow">TEMPLATES &amp; PRESETS</p>
        <h1>Templates</h1>
        <p className="templates-intro">
          A template is a starting configuration: a category, an optional target
          length, a script section structure, and a bundle of production options.
          Built-in templates come straight from the genre packs — nothing here is
          a setting the pipeline ignores, and applying a template goes through
          the same creation paths (and validation) as starting by hand.
        </p>

        {busy && (
          <LoadingState className="templates-message" label="Loading templates…" />
        )}
        {error ? <ErrorState error={error} onRetry={() => void load()} /> : null}
        {notice && <p className="templates-message">{notice}</p>}

        <div className="panel templates-panel">
          <h2>
            <LayoutTemplate size={18} aria-hidden /> Built-in templates
          </h2>
          <p className="templates-note">
            One per genre pack, derived from the pack itself — its label is the
            category and its rules drive the production. Built-ins are
            read-only; copy one to make it yours.
          </p>
          <div className="templates-grid">
            {builtins.map((template) => (
              <article key={template.id} className="template-card">
                <h3>{template.name}</h3>
                {template.source?.description && (
                  <p className="templates-note">{template.source.description}</p>
                )}
                <p className="templates-note">
                  {[
                    template.source?.style_family &&
                      `Style: ${template.source.style_family}`,
                    template.source?.visual_theme &&
                      `Theme: ${template.source.visual_theme}`,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
                <div className="templates-actions">
                  <button
                    type="button"
                    className="button button-secondary"
                    onClick={() => void copyBuiltin(template)}
                    disabled={working === `copy-${template.id}`}
                  >
                    <Copy size={14} aria-hidden /> Copy to my templates
                  </button>
                  <button
                    type="button"
                    className="button button-quiet"
                    onClick={() => openApply(template)}
                  >
                    Use template
                  </button>
                </div>
              </article>
            ))}
            {!busy && builtins.length === 0 && (
              <p className="templates-note">No genre packs are available.</p>
            )}
          </div>
        </div>

        <div className="templates-layout">
          <aside className="templates-sidebar">
            <div className="templates-sidebar-head">
              <h2>My templates</h2>
              <button
                type="button"
                className="button button-primary"
                onClick={() => openEditor(null)}
              >
                <Plus size={14} aria-hidden /> New
              </button>
            </div>
            <div className="templates-list">
              {mine.map((template) => (
                <button
                  key={template.id}
                  type="button"
                  className={`template-row${editingId === template.id ? " template-row--active" : ""}`}
                  onClick={() => openEditor(template)}
                >
                  <span className="template-row-name">{template.name}</span>
                  <span className="template-row-meta">
                    {template.category}
                    {template.target_minutes !== null &&
                      ` · ${template.target_minutes} min`}
                    {template.sections.length > 0 &&
                      ` · ${template.sections.length} sections`}
                  </span>
                </button>
              ))}
              {!busy && mine.length === 0 && (
                <p className="templates-note">
                  None yet — create one, copy a built-in, or save one from a
                  project or draft below.
                </p>
              )}
            </div>

            <div className="panel templates-panel">
              <h3>Save from a project</h3>
              <p className="templates-note">
                Keeps the project&apos;s category and the options the pipeline
                uses. Anything stored but unused is dropped and reported, never
                silently carried.
              </p>
              <label className="templates-field">
                Project
                <select
                  value={fromProjectId}
                  onChange={(event) => setFromProjectId(event.target.value)}
                >
                  <option value="">Choose a project…</option>
                  {projects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.title}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="button button-secondary"
                onClick={() => void saveFromProject()}
                disabled={!fromProjectId || working === "from-project"}
              >
                <Save size={14} aria-hidden /> Save as template
              </button>
              {dropped !== null && dropped.length > 0 && (
                <ul className="templates-dropped">
                  {dropped.map((entry) => (
                    <li key={entry.key}>
                      Dropped <code>{entry.key}</code> —{" "}
                      {DROP_REASONS[entry.reason] || entry.reason}.
                    </li>
                  ))}
                </ul>
              )}
              {dropped !== null && dropped.length === 0 && (
                <p className="templates-note">
                  Nothing was dropped — every stored option is one the pipeline
                  uses.
                </p>
              )}
            </div>

            <div className="panel templates-panel">
              <h3>Save from a script draft</h3>
              <p className="templates-note">
                Keeps the draft&apos;s category, target length and section
                headings. The draft&apos;s words are never copied.
              </p>
              <label className="templates-field">
                Draft
                <select
                  value={fromDraftId}
                  onChange={(event) => setFromDraftId(event.target.value)}
                >
                  <option value="">Choose a draft…</option>
                  {drafts.map((draft) => (
                    <option key={draft.id} value={draft.id}>
                      {draft.title}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="button button-secondary"
                onClick={() => void saveFromDraft()}
                disabled={!fromDraftId || working === "from-draft"}
              >
                <Save size={14} aria-hidden /> Save structure as template
              </button>
            </div>
          </aside>

          <div className="templates-main">
            {editingId !== null && (
              <div className="panel templates-panel">
                <h2>{editingId === "new" ? "New template" : "Edit template"}</h2>
                <div className="templates-form">
                  <label className="templates-field">
                    Name
                    <input
                      value={name}
                      onChange={(event) => setName(event.target.value)}
                      placeholder="My production setup"
                    />
                  </label>
                  <label className="templates-field">
                    Category
                    <input
                      value={category}
                      onChange={(event) => setCategory(event.target.value)}
                      placeholder="Documentary"
                    />
                  </label>
                  <label className="templates-field">
                    Target length (minutes, optional)
                    <input
                      value={target}
                      onChange={(event) => setTarget(event.target.value)}
                      inputMode="decimal"
                      placeholder="e.g. 12"
                    />
                  </label>
                  <label className="templates-field">
                    Genre pack
                    <select
                      value={form.genre}
                      onChange={(event) => setField("genre", event.target.value)}
                    >
                      <option value="">Detected from the script</option>
                      {genres.map((genre) => (
                        <option key={genre.id} value={genre.id}>
                          {genre.label || genre.name || genre.id}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="templates-field">
                    Narration voice
                    <select
                      value={form.voice_id}
                      onChange={(event) => setField("voice_id", event.target.value)}
                    >
                      <option value="">Production default</option>
                      {voices.map((voice) => (
                        <option key={voice.id} value={voice.id}>
                          {voice.id}
                          {voice.is_default ? " (default)" : ""}
                          {voice.installed ? "" : " — not installed"}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="templates-field">
                    Captions
                    <select
                      value={form.captions}
                      onChange={(event) => setField("captions", event.target.value)}
                    >
                      <option value="">Production default</option>
                      <option value="burn">Burned into the picture</option>
                      <option value="sidecar">Sidecar file</option>
                      <option value="off">Off</option>
                    </select>
                  </label>
                  <label className="templates-field">
                    Caption sync
                    <select
                      value={form.caption_sync}
                      onChange={(event) =>
                        setField("caption_sync", event.target.value)
                      }
                    >
                      <option value="">Production default</option>
                      <option value="word">Word timing</option>
                      <option value="syllable">Syllable timing</option>
                      <option value="off">Off</option>
                    </select>
                  </label>
                  <label className="templates-field">
                    Music
                    <select
                      value={form.music_enabled}
                      onChange={(event) =>
                        setField("music_enabled", event.target.value)
                      }
                    >
                      <option value="">Production default</option>
                      <option value="true">On</option>
                      <option value="false">Off</option>
                    </select>
                  </label>
                  <label className="templates-field">
                    Music file (optional)
                    <input
                      value={form.music_file}
                      onChange={(event) => setField("music_file", event.target.value)}
                      placeholder="Name of an uploaded music file"
                    />
                  </label>
                  <label className="templates-field">
                    Plan approval
                    <select
                      value={form.require_plan_approval}
                      onChange={(event) =>
                        setField("require_plan_approval", event.target.value)
                      }
                    >
                      <option value="">Production default</option>
                      <option value="true">Review the plan before production</option>
                      <option value="false">Start without plan review</option>
                    </select>
                  </label>
                  {NUMERIC_FIELDS.map(({ key, label, hint }) => (
                    <label key={key} className="templates-field">
                      {label}
                      <input
                        value={form[key]}
                        onChange={(event) => setField(key, event.target.value)}
                        inputMode="decimal"
                        placeholder={hint}
                      />
                    </label>
                  ))}
                </div>

                <h3>Script section structure</h3>
                <p className="templates-note">
                  Headings only — a template is a scaffold. Applying it to a
                  draft creates sections with these headings and empty bodies
                  for you to write.
                </p>
                <div className="templates-sections">
                  {headings.map((heading, index) => (
                    <div key={index} className="templates-section-row">
                      <input
                        value={heading}
                        onChange={(event) =>
                          setHeadings((current) =>
                            current.map((value, i) =>
                              i === index ? event.target.value : value,
                            ),
                          )
                        }
                        placeholder={`Section ${index + 1} heading`}
                      />
                      <button
                        type="button"
                        className="button button-quiet"
                        onClick={() =>
                          setHeadings((current) =>
                            current.filter((_, i) => i !== index),
                          )
                        }
                        aria-label={`Remove section ${index + 1}`}
                      >
                        <X size={14} aria-hidden />
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    className="button button-quiet"
                    onClick={() => setHeadings((current) => [...current, ""])}
                  >
                    <Plus size={14} aria-hidden /> Add section
                  </button>
                </div>

                <div className="templates-actions">
                  <button
                    type="button"
                    className="button button-primary"
                    onClick={() => void saveEditor()}
                    disabled={working === "save"}
                  >
                    <Save size={14} aria-hidden /> Save template
                  </button>
                  {editingId !== "new" && (
                    <>
                      <button
                        type="button"
                        className="button button-secondary"
                        onClick={() => {
                          const current = mine.find((t) => t.id === editingId);
                          if (current) openApply(current);
                        }}
                      >
                        Use template
                      </button>
                      <button
                        type="button"
                        className="button button-quiet"
                        onClick={() => {
                          const current = mine.find((t) => t.id === editingId);
                          if (current) void removeTemplate(current);
                        }}
                        disabled={working === `delete-${editingId}`}
                      >
                        <Trash2 size={14} aria-hidden /> Delete
                      </button>
                    </>
                  )}
                </div>
              </div>
            )}

            {applyTarget && (
              <div className="panel templates-panel">
                <h2>Use “{applyTarget.name}”</h2>
                <p className="templates-note">
                  {applyTarget.category}
                  {applyTarget.target_minutes !== null &&
                    ` · target ${applyTarget.target_minutes} min`}
                  {applyTarget.sections.length > 0 &&
                    ` · ${applyTarget.sections.length} section headings`}
                </p>
                {optionSummary(applyTarget).length > 0 && (
                  <ul className="templates-summary">
                    {optionSummary(applyTarget).map((part) => (
                      <li key={part}>{part}</li>
                    ))}
                  </ul>
                )}
                {optionSummary(applyTarget).length === 0 && (
                  <p className="templates-note">
                    This template sets no production options — a production
                    from it uses the standard defaults.
                  </p>
                )}
                <div className="templates-form">
                  <label className="templates-field">
                    Title
                    <input
                      value={applyTitle}
                      onChange={(event) => setApplyTitle(event.target.value)}
                    />
                  </label>
                </div>
                <div className="templates-actions">
                  <button
                    type="button"
                    className="button button-secondary"
                    onClick={() => void applyDraft()}
                    disabled={working === "apply-draft"}
                  >
                    <FileText size={14} aria-hidden /> Create script draft
                  </button>
                  {createdDraftId && (
                    <Link className="button button-text" href="/scripts">
                      Open in Script Studio
                    </Link>
                  )}
                </div>
                <h3>Create a project</h3>
                <p className="templates-note">
                  Paste the narration script. The project is created through
                  the normal creation path — plan limits and option validation
                  apply exactly as they do by hand.
                </p>
                <label className="templates-field">
                  Script
                  <textarea
                    value={applyScript}
                    onChange={(event) => setApplyScript(event.target.value)}
                    rows={10}
                    placeholder="The narration script for the new production…"
                  />
                </label>
                <div className="templates-actions">
                  <button
                    type="button"
                    className="button button-primary"
                    onClick={() => void applyProject()}
                    disabled={!applyScript.trim() || working === "apply-project"}
                  >
                    <Film size={14} aria-hidden /> Create project
                  </button>
                  {createdProjectId && (
                    <Link
                      className="button button-text"
                      href={`/projects/${createdProjectId}`}
                    >
                      Open the new project
                    </Link>
                  )}
                </div>
              </div>
            )}

            {editingId === null && !applyTarget && (
              <div className="panel templates-panel">
                <h2>Your starting points</h2>
                <p className="templates-note">
                  Pick one of your templates on the left to edit it, copy a
                  built-in above, or use any template to start a script draft
                  or a project with its settings already in place.
                </p>
              </div>
            )}
          </div>
        </div>
      </section>
    </main>
  );
}
