"use client";

import { useState } from "react";
import { ChevronDown, Music, Plus, Trash2, Upload } from "lucide-react";
import {
  voiceDisplayName,
  type ConfigCatalogue,
  type ProductionConfig,
} from "@/lib/api";

/**
 * The Phase 9 pre-production configuration surface (§14): one
 * form over services.production_config's document — presets,
 * motion graphics, visual theme, music, narration voice,
 * captions. Every control maps to a setting the pipeline
 * consumes; anything the pipeline cannot honour is not offered
 * (own-music upload needs a project to attach to, so in the
 * creation flow it is labelled and disabled rather than fake).
 *
 * The component is controlled: the parent owns the config
 * object (the create page sends it with the project; the
 * project Configuration tab saves it through the API, which
 * re-validates and reports errors back).
 */

const TOKEN_LABELS: Record<string, string> = {
  bg: "Background",
  surface: "Surface / panels",
  text: "Text",
  muted: "Muted text",
  accent: "Accent",
  accent2: "Second accent",
  paper: "Paper",
  brass: "Brass",
};

const MIX_FIELDS: Array<{
  key: string;
  label: string;
  min: number;
  max: number;
  step: number;
}> = [
  { key: "music_fade_in_seconds", label: "Fade in (s)", min: 0, max: 30, step: 0.5 },
  { key: "music_fade_out_seconds", label: "Fade out (s)", min: 0, max: 30, step: 0.5 },
  { key: "music_loop_crossfade_seconds", label: "Loop crossfade (s)", min: 0, max: 10, step: 0.5 },
  { key: "duck_threshold", label: "Duck threshold", min: 0.05, max: 1, step: 0.01 },
  { key: "duck_ratio", label: "Duck ratio", min: 1, max: 100, step: 1 },
  { key: "duck_attack_ms", label: "Duck attack (ms)", min: 0, max: 10000, step: 10 },
  { key: "duck_release_ms", label: "Duck release (ms)", min: 0, max: 60000, step: 50 },
];

function rgbToHex(rgb: number[]): string {
  return (
    "#" +
    rgb
      .map((c) => Math.max(0, Math.min(255, c)).toString(16).padStart(2, "0"))
      .join("")
  );
}

function hexToRgb(hex: string): number[] {
  const value = hex.replace("#", "");
  return [
    parseInt(value.slice(0, 2), 16),
    parseInt(value.slice(2, 4), 16),
    parseInt(value.slice(4, 6), 16),
  ];
}

function swatch(rgb: number[]): string {
  return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
}

export function ProductionConfigForm({
  catalogue,
  value,
  onChange,
  mode,
  onMusicUpload,
  onRemoveMusicUpload,
}: {
  catalogue: ConfigCatalogue;
  value: ProductionConfig;
  onChange: (next: ProductionConfig) => void;
  mode: "create" | "project";
  onMusicUpload?: (file: File) => void;
  onRemoveMusicUpload?: () => void;
}) {
  const [uploadBusy, setUploadBusy] = useState(false);
  const library = catalogue.music_library;
  const libraryUsable = library.configured && library.available;

  // Any manual edit after a preset makes the document custom;
  // the backend re-derives the honest label at save regardless.
  function edit(next: ProductionConfig) {
    onChange({ ...next, preset: "custom" });
  }

  function applyPreset(presetId: string) {
    if (presetId === "custom") {
      onChange({ ...value, preset: "custom" });
      return;
    }
    const preset = catalogue.presets.find((p) => p.id === presetId);
    if (preset) onChange(structuredClone(preset.config));
  }

  const music = value.music;
  const moods = Array.from(
    new Set(library.tracks.flatMap((track) => track.moods)),
  ).sort();

  return (
    <div className="p9-form">
      <fieldset className="p9-group">
        <legend>Preset</legend>
        <p className="p9-hint">
          A preset is a starting bundle of real settings — everything stays
          editable afterwards, and editing marks the configuration Custom.
        </p>
        <div className="p9-preset-row" role="radiogroup" aria-label="Preset">
          {catalogue.presets.map((preset) => (
            <button
              key={preset.id}
              type="button"
              role="radio"
              aria-checked={value.preset === preset.id}
              className={`p9-chip${value.preset === preset.id ? " is-active" : ""}`}
              onClick={() => applyPreset(preset.id)}
            >
              {preset.label}
            </button>
          ))}
          <button
            type="button"
            role="radio"
            aria-checked={value.preset === "custom"}
            className={`p9-chip${value.preset === "custom" ? " is-active" : ""}`}
            onClick={() => applyPreset("custom")}
          >
            Custom
          </button>
        </div>
      </fieldset>

      <fieldset className="p9-group">
        <legend>Motion graphics</legend>
        <label className="motion-toggle">
          <input
            type="checkbox"
            checked={value.motion_graphics.enabled}
            onChange={(event) =>
              edit({
                ...value,
                motion_graphics: {
                  ...value.motion_graphics,
                  enabled: event.target.checked,
                },
              })
            }
          />
          <span>
            <strong>Motion graphics</strong>
            <small>Designed graphics for scenes that need them.</small>
          </span>
        </label>
      </fieldset>

      <fieldset className="p9-group">
        <legend>Visual theme</legend>
        <p className="p9-hint">
          Themes govern the graphics — backgrounds, typography, panels,
          cards and timelines — never a filter over real footage. Leave
          unset to use the genre pack&rsquo;s own theme.
        </p>
        <div className="p9-theme-grid" role="radiogroup" aria-label="Visual theme">
          <button
            type="button"
            role="radio"
            aria-checked={value.theme.id === null}
            className={`p9-theme${value.theme.id === null ? " is-active" : ""}`}
            onClick={() => edit({ ...value, theme: { id: null } })}
          >
            <span className="p9-theme-name">Genre pack default</span>
          </button>
          {catalogue.themes.map((theme) => (
            <button
              key={theme.id}
              type="button"
              role="radio"
              aria-checked={value.theme.id === theme.id}
              className={`p9-theme${value.theme.id === theme.id ? " is-active" : ""}`}
              onClick={() => edit({ ...value, theme: { id: theme.id } })}
            >
              <span className="p9-swatches" aria-hidden>
                <i style={{ background: swatch(theme.swatch.bg) }} />
                <i style={{ background: swatch(theme.swatch.surface) }} />
                <i style={{ background: swatch(theme.swatch.accent) }} />
              </span>
              <span className="p9-theme-name">
                {theme.id[0].toUpperCase() + theme.id.slice(1)}
              </span>
            </button>
          ))}
          <button
            type="button"
            role="radio"
            aria-checked={value.theme.id === "custom"}
            className={`p9-theme${value.theme.id === "custom" ? " is-active" : ""}`}
            onClick={() =>
              edit({
                ...value,
                theme: { id: "custom", base: "dark", tokens: {} },
              })
            }
          >
            <span className="p9-theme-name">Custom</span>
          </button>
        </div>
        {value.theme.id === "custom" && (
          <div className="p9-custom-theme">
            <label className="field-label" htmlFor="p9-theme-base">
              BUILT ON
              <span className="select-wrap">
                <select
                  id="p9-theme-base"
                  value={value.theme.base || "dark"}
                  onChange={(event) =>
                    edit({
                      ...value,
                      theme: { ...value.theme, base: event.target.value },
                    })
                  }
                >
                  {catalogue.custom_theme.bases.map((base) => (
                    <option key={base} value={base}>
                      {base[0].toUpperCase() + base.slice(1)}
                    </option>
                  ))}
                </select>
                <ChevronDown size={18} strokeWidth={1.7} aria-hidden />
              </span>
            </label>
            <div className="p9-token-grid">
              {catalogue.custom_theme.token_keys.map((key) => {
                const baseTheme = catalogue.themes.find(
                  (t) => t.id === (value.theme.base || "dark"),
                );
                const current =
                  value.theme.tokens?.[key] ?? baseTheme?.swatch[key] ?? [0, 0, 0];
                return (
                  <label key={key} className="p9-token">
                    <input
                      type="color"
                      value={rgbToHex(current)}
                      onChange={(event) =>
                        edit({
                          ...value,
                          theme: {
                            ...value.theme,
                            tokens: {
                              ...(value.theme.tokens || {}),
                              [key]: hexToRgb(event.target.value),
                            },
                          },
                        })
                      }
                    />
                    <span>{TOKEN_LABELS[key] || key}</span>
                  </label>
                );
              })}
            </div>
          </div>
        )}
      </fieldset>

      <fieldset className="p9-group">
        <legend>Music</legend>
        <label className="motion-toggle">
          <input
            type="checkbox"
            checked={music.enabled}
            onChange={(event) =>
              edit({
                ...value,
                music: { ...music, enabled: event.target.checked },
              })
            }
          />
          <span>
            <strong>Background music</strong>
            <small>
              Mixed under the narration with automatic ducking, so narration
              stays clear. When off, no music is added at render.
            </small>
          </span>
        </label>
        {music.enabled && (
          <>
            <div className="p9-source-row" role="radiogroup" aria-label="Music source">
              <label>
                <input
                  type="radio"
                  name="p9-music-source"
                  checked={music.source === "none"}
                  onChange={() =>
                    edit({ ...value, music: { ...music, source: "none" } })
                  }
                />{" "}
                No track selected
              </label>
              <label>
                <input
                  type="radio"
                  name="p9-music-source"
                  checked={music.source === "upload"}
                  disabled={mode === "create"}
                  onChange={() =>
                    edit({ ...value, music: { ...music, source: "upload" } })
                  }
                />{" "}
                My uploaded track
              </label>
              <label>
                <input
                  type="radio"
                  name="p9-music-source"
                  checked={music.source === "library"}
                  disabled={!libraryUsable}
                  onChange={() =>
                    edit({ ...value, music: { ...music, source: "library" } })
                  }
                />{" "}
                Studio library
              </label>
            </div>
            {mode === "create" && (
              <p className="p9-hint">
                Uploading your own track needs the project to exist — it is
                available in the project&rsquo;s Configuration tab right after
                creation.
              </p>
            )}
            {!libraryUsable && (
              <p className="p9-hint">
                {library.configured
                  ? "The configured music catalogue is empty or unreadable, so no library tracks can be selected."
                  : "No music catalogue is configured on this deployment, so library selection is unavailable."}
              </p>
            )}
            {music.source === "upload" && mode === "project" && (
              <div className="p9-upload-box">
                {music.upload ? (
                  <p className="plan-meta">
                    <Music size={15} strokeWidth={2} aria-hidden />{" "}
                    {music.upload.file_name || "Uploaded track"}
                    {music.upload.duration_seconds
                      ? ` · ${music.upload.duration_seconds.toFixed(1)}s`
                      : ""}
                    {music.upload.codec ? ` · ${music.upload.codec}` : ""}{" "}
                    (your upload — the original is preserved as project media)
                  </p>
                ) : (
                  <p className="plan-meta">No track uploaded yet.</p>
                )}
                <div className="p9-upload-actions">
                  <label className="p9-upload-button">
                    <Upload size={15} strokeWidth={2} aria-hidden />{" "}
                    {music.upload ? "Replace track" : "Upload track"}
                    <input
                      type="file"
                      accept="audio/*"
                      hidden
                      disabled={uploadBusy}
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        if (file && onMusicUpload) {
                          setUploadBusy(true);
                          onMusicUpload(file);
                          setUploadBusy(false);
                        }
                        event.target.value = "";
                      }}
                    />
                  </label>
                  {music.upload && onRemoveMusicUpload && (
                    <button
                      type="button"
                      className="p9-text-button"
                      onClick={onRemoveMusicUpload}
                    >
                      <Trash2 size={15} strokeWidth={2} aria-hidden /> Remove
                    </button>
                  )}
                </div>
              </div>
            )}
            {music.source === "library" && libraryUsable && (
              <div className="form-fields">
                <label className="field-label" htmlFor="p9-track">
                  TRACK
                  <span className="select-wrap">
                    <select
                      id="p9-track"
                      value={music.track_id || ""}
                      onChange={(event) =>
                        edit({
                          ...value,
                          music: {
                            ...music,
                            track_id: event.target.value || null,
                            mood: event.target.value ? music.mood : music.mood,
                          },
                        })
                      }
                    >
                      <option value="">Choose by mood instead…</option>
                      {library.tracks.map((track) => (
                        <option key={track.track_id} value={track.track_id}>
                          {track.title || track.track_id}
                          {track.composer ? ` — ${track.composer}` : ""} (
                          {track.licence_state})
                        </option>
                      ))}
                    </select>
                    <ChevronDown size={18} strokeWidth={1.7} aria-hidden />
                  </span>
                </label>
                <label className="field-label" htmlFor="p9-mood">
                  OR MOOD
                  <span className="select-wrap">
                    <select
                      id="p9-mood"
                      value={music.mood || ""}
                      onChange={(event) =>
                        edit({
                          ...value,
                          music: {
                            ...music,
                            mood: event.target.value || null,
                            track_id: event.target.value ? null : music.track_id,
                          },
                        })
                      }
                    >
                      <option value="">Any mood</option>
                      {moods.map((mood) => (
                        <option key={mood} value={mood}>
                          {mood}
                        </option>
                      ))}
                    </select>
                    <ChevronDown size={18} strokeWidth={1.7} aria-hidden />
                  </span>
                </label>
              </div>
            )}
            <div className="form-fields">
              <label className="field-label" htmlFor="p9-volume">
                MUSIC VOLUME — {music.volume === null ? "pack default" : music.volume.toFixed(2)}
                <input
                  id="p9-volume"
                  type="range"
                  min={0}
                  max={2}
                  step={0.05}
                  value={music.volume ?? 0.1}
                  onChange={(event) =>
                    edit({
                      ...value,
                      music: { ...music, volume: Number(event.target.value) },
                    })
                  }
                />
              </label>
              <label className="field-label" htmlFor="p9-placement">
                PLACEMENT
                <span className="select-wrap">
                  <select
                    id="p9-placement"
                    value={music.placement}
                    onChange={(event) =>
                      edit({
                        ...value,
                        music: { ...music, placement: event.target.value },
                      })
                    }
                  >
                    <option value="throughout">Throughout the film</option>
                    <option value="sections">Selected sections only</option>
                  </select>
                  <ChevronDown size={18} strokeWidth={1.7} aria-hidden />
                </span>
              </label>
            </div>
            {music.placement === "sections" && (
              <div className="p9-sections">
                {music.sections.map((section, index) => (
                  <div key={index} className="p9-section-row">
                    <label>
                      Start (s)
                      <input
                        type="number"
                        min={0}
                        step={0.5}
                        value={section.start}
                        onChange={(event) => {
                          const sections = music.sections.map((s, i) =>
                            i === index
                              ? { ...s, start: Number(event.target.value) }
                              : s,
                          );
                          edit({ ...value, music: { ...music, sections } });
                        }}
                      />
                    </label>
                    <label>
                      End (s)
                      <input
                        type="number"
                        min={0}
                        step={0.5}
                        value={section.end}
                        onChange={(event) => {
                          const sections = music.sections.map((s, i) =>
                            i === index
                              ? { ...s, end: Number(event.target.value) }
                              : s,
                          );
                          edit({ ...value, music: { ...music, sections } });
                        }}
                      />
                    </label>
                    <button
                      type="button"
                      className="p9-text-button"
                      aria-label={`Remove section ${index + 1}`}
                      onClick={() =>
                        edit({
                          ...value,
                          music: {
                            ...music,
                            sections: music.sections.filter(
                              (_, i) => i !== index,
                            ),
                          },
                        })
                      }
                    >
                      <Trash2 size={15} strokeWidth={2} aria-hidden />
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  className="p9-text-button"
                  onClick={() =>
                    edit({
                      ...value,
                      music: {
                        ...music,
                        sections: [...music.sections, { start: 0, end: 10 }],
                      },
                    })
                  }
                >
                  <Plus size={15} strokeWidth={2} aria-hidden /> Add section
                </button>
                <p className="p9-hint">
                  Times are seconds into the finished film. The bed is
                  silenced outside these ranges before ducking.
                </p>
              </div>
            )}
            <details className="p9-mix">
              <summary>Advanced mix (fades &amp; ducking)</summary>
              <div className="form-fields">
                {MIX_FIELDS.map((field) => (
                  <label key={field.key} className="field-label">
                    {field.label.toUpperCase()}
                    <input
                      className="thumbnail-input"
                      type="number"
                      min={field.min}
                      max={field.max}
                      step={field.step}
                      placeholder="Pack default"
                      value={music.mix[field.key] ?? ""}
                      onChange={(event) => {
                        const mix = { ...music.mix };
                        if (event.target.value === "") delete mix[field.key];
                        else mix[field.key] = Number(event.target.value);
                        edit({ ...value, music: { ...music, mix } });
                      }}
                    />
                  </label>
                ))}
              </div>
            </details>
          </>
        )}
      </fieldset>

      <fieldset className="p9-group">
        <legend>Narration</legend>
        <label className="field-label" htmlFor="p9-voice">
          VOICE
          <span className="select-wrap">
            <select
              id="p9-voice"
              value={value.narration.voice_id || ""}
              onChange={(event) =>
                edit({
                  ...value,
                  narration: {
                    ...value.narration,
                    voice_id: event.target.value || null,
                  },
                })
              }
            >
              <option value="">Deployment default</option>
              {catalogue.narration.voices
                .filter(
                  (voice) =>
                    // Offer the voices that genuinely speak the
                    // configuration's narration language (the
                    // catalogue now carries each voice's language).
                    // Voices without language data, and the voice
                    // already selected, stay visible so an existing
                    // configuration is never silently blanked.
                    !value.narration.language ||
                    !voice.language ||
                    voice.language === value.narration.language ||
                    voice.id === value.narration.voice_id,
                )
                .map((voice) => (
                  <option
                    key={voice.id}
                    value={voice.id}
                    disabled={!voice.installed}
                  >
                    {voiceDisplayName(voice)}
                    {voice.is_default ? " · Default" : ""}
                    {voice.installed ? "" : " — not installed"}
                  </option>
                ))}
            </select>
            <ChevronDown size={18} strokeWidth={1.7} aria-hidden />
          </span>
        </label>
        <p className="p9-hint">{catalogue.narration.note}</p>
      </fieldset>

      <fieldset className="p9-group">
        <legend>Captions</legend>
        <div className="form-fields">
          <label className="field-label" htmlFor="p9-caption-mode">
            CAPTIONS
            <span className="select-wrap">
              <select
                id="p9-caption-mode"
                value={value.captions.mode}
                onChange={(event) =>
                  edit({
                    ...value,
                    captions: { ...value.captions, mode: event.target.value },
                  })
                }
              >
                <option value="burn">Burned into the video</option>
                <option value="sidecar">Sidecar files (.srt / .vtt)</option>
                <option value="off">Off</option>
              </select>
              <ChevronDown size={18} strokeWidth={1.7} aria-hidden />
            </span>
          </label>
          <label className="field-label" htmlFor="p9-caption-sync">
            SYNC STYLE
            <span className="select-wrap">
              <select
                id="p9-caption-sync"
                value={value.captions.sync}
                disabled={value.captions.mode === "off"}
                onChange={(event) =>
                  edit({
                    ...value,
                    captions: { ...value.captions, sync: event.target.value },
                  })
                }
              >
                <option value="word">Word-by-word (karaoke)</option>
                <option value="off">Plain lines</option>
              </select>
              <ChevronDown size={18} strokeWidth={1.7} aria-hidden />
            </span>
          </label>
        </div>
      </fieldset>
    </div>
  );
}

export function emptyProductionConfig(): ProductionConfig {
  return {
    version: 1,
    preset: "custom",
    motion_graphics: { enabled: true, intensity: "auto" },
    theme: { id: null },
    music: {
      enabled: true,
      source: "none",
      upload: null,
      track_id: null,
      mood: null,
      volume: null,
      placement: "throughout",
      sections: [],
      mix: {},
    },
    narration: { source: "ai", voice_id: null, voice_recording: null },
    captions: { mode: "burn", sync: "word" },
  };
}
