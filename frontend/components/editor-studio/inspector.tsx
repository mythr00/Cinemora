"use client";

import { useState } from "react";
import type {
  AssetSpec,
  EditorOperation,
  StudioItem,
  StudioPoolEntry,
  StudioView,
} from "@/lib/api";
import { CLASSIFICATION_LABELS, formatDuration } from "@/components/editor/shared";
import { studioItemLabel, type StudioSelection } from "./shared";

/**
 * The Studio Inspector: every control here issues a real editor
 * operation (services/editor.py) against the current revision —
 * the response is the next revision and the page re-reads it.
 * Controls whose operation the backend would refuse for the
 * selected item are not rendered (e.g. no template picker for
 * native-family graphics, no transition picker for gap items).
 */
export function Inspector({
  studio,
  selection,
  poolSelection,
  busy,
  locks,
  applyOps,
}: {
  studio: StudioView;
  selection: StudioSelection;
  poolSelection: StudioPoolEntry | null;
  busy: boolean;
  /** Phase 6 (A-17): server-persisted locked track ids. Controls
   * for a locked track render disabled with a locked note — and
   * the server enforces the same rule (409 track_locked), so this
   * is presentation of a real constraint, not the constraint. */
  locks: ReadonlySet<string>;
  applyOps: (operations: EditorOperation[], note: string) => void;
}) {
  const visualTrack = studio.tracks.find((t) => t.id === "video");
  const graphicsTrack = studio.tracks.find((t) => t.id === "graphics");
  const captionsTrack = studio.tracks.find((t) => t.id === "captions");
  const narrationTrack = studio.tracks.find((t) => t.id === "narration");
  const musicTrack = studio.tracks.find((t) => t.id === "music");
  const allItems: StudioItem[] = [
    ...(visualTrack?.id === "video" ? visualTrack.items : []),
    ...(graphicsTrack?.id === "graphics" ? graphicsTrack.items : []),
  ];
  const sentences = narrationTrack?.id === "narration" ? narrationTrack.sentences : [];

  return (
    <section className="studio-pane studio-inspector" aria-label="Inspector">
      <h2>Inspector</h2>
      {selection?.kind === "item" &&
        (() => {
          const item = allItems.find((i) => i.item_id === selection.itemId);
          if (!item) return <p className="studio-note">That item is no longer on the timeline.</p>;
          const itemTrack = item.kind === "graphic" ? "graphics" : "video";
          const itemLocked = locks.has(itemTrack);
          return (
            <>
              {itemLocked && (
                <p className="studio-note">
                  The {itemTrack} track is locked — unlock it on the timeline to edit this item.
                </p>
              )}
              <ItemInspector
                key={item.item_id}
                studio={studio}
                item={item}
                sentences={sentences}
                poolSelection={poolSelection}
                busy={busy || itemLocked}
                applyOps={applyOps}
              />
            </>
          );
        })()}
      {selection?.kind === "caption" &&
        captionsTrack?.id === "captions" &&
        (() => {
          const segment = captionsTrack.segments.find(
            (s) => s.segment_index === selection.segmentIndex,
          );
          if (!segment) return <p className="studio-note">That caption is gone.</p>;
          const captionsLocked = locks.has("captions");
          return (
            <>
              {captionsLocked && (
                <p className="studio-note">
                  The captions track is locked — unlock it on the timeline to edit captions.
                </p>
              )}
              <CaptionInspector
                key={segment.segment_index}
                segment={segment}
                busy={busy || captionsLocked}
                applyOps={applyOps}
              />
            </>
          );
        })()}
      {selection?.kind === "sentence" &&
        (() => {
          const sentence = sentences.find(
            (s) => s.sentence_number === selection.sentenceNumber,
          );
          if (!sentence) return <p className="studio-note">That sentence is gone.</p>;
          const narrationLocked = locks.has("narration");
          return (
            <>
              {narrationLocked && (
                <p className="studio-note">
                  The narration track is locked — unlock it on the timeline to edit narration.
                </p>
              )}
              <SentenceInspector
                key={sentence.sentence_number}
                sentence={sentence}
                volume={studio.volumes.narration}
                poolSelection={poolSelection}
                busy={busy || narrationLocked}
                applyOps={applyOps}
              />
            </>
          );
        })()}
      {selection?.kind === "music" && musicTrack?.id === "music" && (
        <>
          {locks.has("music") && (
            <p className="studio-note">
              The music track is locked — unlock it on the timeline to edit music.
            </p>
          )}
          <MusicInspector
            key="music"
            fileName={musicTrack.file_name}
            state={musicTrack.state}
            volume={studio.volumes.music}
            poolSelection={poolSelection}
            busy={busy || locks.has("music")}
            applyOps={applyOps}
          />
        </>
      )}
      {!selection && (
        <>
          <p className="studio-note">
            Select a clip, graphic, caption, narration sentence or the music track to
            edit it. Every edit is saved by the server as a new revision — Undo steps
            back through the revision chain.
          </p>
          {(locks.has("narration") || locks.has("music")) && (
            <p className="studio-note">
              Volume controls are disabled while the narration or music track is locked.
            </p>
          )}
          <VolumeFields
            studio={studio}
            busy={busy || locks.has("narration") || locks.has("music")}
            applyOps={applyOps}
          />
          <hr className="studio-divider" />
          <p className="studio-note">
            {allItems.filter((i) => i.kind === "clip").length} clips ·{" "}
            {allItems.filter((i) => i.kind === "graphic").length} graphics ·{" "}
            {allItems.filter((i) => i.kind === "gap").length} gaps · revision{" "}
            {studio.revision_number}
          </p>
        </>
      )}
    </section>
  );
}

function VolumeFields({
  studio,
  busy,
  applyOps,
}: {
  studio: StudioView;
  busy: boolean;
  applyOps: (operations: EditorOperation[], note: string) => void;
}) {
  const [narration, setNarration] = useState(studio.volumes.narration);
  const [music, setMusic] = useState(studio.volumes.music);
  return (
    <>
      <div className="studio-field">
        <label htmlFor="studio-vol-narration">Narration volume</label>
        <input
          id="studio-vol-narration"
          className="studio-range"
          type="range"
          min={0}
          max={2}
          step={0.05}
          value={narration}
          disabled={busy}
          onChange={(event) => setNarration(Number(event.target.value))}
        />
      </div>
      <div className="studio-field">
        <label htmlFor="studio-vol-music">Music volume</label>
        <input
          id="studio-vol-music"
          className="studio-range"
          type="range"
          min={0}
          max={2}
          step={0.05}
          value={music}
          disabled={busy}
          onChange={(event) => setMusic(Number(event.target.value))}
        />
      </div>
      <button
        type="button"
        className="studio-btn"
        disabled={busy}
        onClick={() =>
          applyOps(
            [{ type: "set_volumes", narration, music }],
            "Volumes updated.",
          )
        }
      >
        Apply volumes ({narration.toFixed(2)} / {music.toFixed(2)})
      </button>
    </>
  );
}

function poolSpec(entry: StudioPoolEntry): AssetSpec | null {
  if (entry.origin === "ledger" && entry.decision_id) {
    return { library_decision_id: entry.decision_id };
  }
  if (entry.origin === "upload" && entry.upload_id) {
    return { upload_id: entry.upload_id };
  }
  return null;
}

function ItemInspector({
  studio,
  item,
  sentences,
  poolSelection,
  busy,
  applyOps,
}: {
  studio: StudioView;
  item: StudioItem;
  sentences: Array<{ sentence_number: number; start: number; end: number; text: string }>;
  poolSelection: StudioPoolEntry | null;
  busy: boolean;
  applyOps: (operations: EditorOperation[], note: string) => void;
}) {
  const [transition, setTransition] = useState(item.transition || "cut");
  const [reason, setReason] = useState(item.transition_reason || "");
  const [windowFrames, setWindowFrames] = useState(
    item.transition_window_frames ? String(item.transition_window_frames) : "",
  );
  const [clipStart, setClipStart] = useState(String(item.asset.clip_start || 0));
  const [duration, setDuration] = useState(String(item.duration));
  const [moveTarget, setMoveTarget] = useState(String(item.sentence_number));
  const [splitAt, setSplitAt] = useState("");
  const [template, setTemplate] = useState(item.graphic?.template || "");
  const [style, setStyle] = useState(item.graphic?.style || "");
  const [inputsText, setInputsText] = useState(
    item.graphic?.inputs ? JSON.stringify(item.graphic.inputs, null, 2) : "",
  );
  const [formError, setFormError] = useState<string | null>(null);

  const spec = poolSelection ? poolSpec(poolSelection) : null;
  const isGraphic = item.kind === "graphic";

  function assignGraphic() {
    setFormError(null);
    let inputs: Record<string, unknown> | undefined;
    if (inputsText.trim()) {
      try {
        const parsed: unknown = JSON.parse(inputsText);
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
          throw new Error("inputs must be a JSON object");
        }
        inputs = parsed as Record<string, unknown>;
      } catch (error) {
        setFormError(
          `Inputs are not valid JSON: ${error instanceof Error ? error.message : "parse error"}`,
        );
        return;
      }
    }
    const op: EditorOperation = {
      type: "assign_graphic",
      item_id: item.item_id,
      ...(template ? { template } : {}),
      ...(inputs ? { inputs } : {}),
      ...(style ? { style } : {}),
    };
    applyOps([op], "Graphic assignment updated and re-rendered.");
  }

  return (
    <>
      <div>
        <strong style={{ fontSize: 13.5 }}>{studioItemLabel(item)}</strong>
        <div className="studio-field-row" style={{ marginTop: 6 }}>
          <span className="studio-chip">
            {CLASSIFICATION_LABELS[item.classification] || item.classification}
          </span>
          <span className="studio-chip">
            Sentence {item.sentence_number} · Scene {item.scene_number}
          </span>
        </div>
      </div>
      <p className="studio-note">
        {item.start.toFixed(2)}s – {item.end.toFixed(2)}s ({item.duration.toFixed(2)}s)
        {item.asset.file_name ? ` · ${item.asset.file_name}` : ""}
        {item.asset.source ? ` · ${item.asset.source}` : ""}
        {item.asset.source_duration
          ? ` · source ${formatDuration(item.asset.source_duration)}`
          : ""}
      </p>

      <hr className="studio-divider" />
      <div className="studio-field">
        <label htmlFor="studio-transition">Transition in</label>
        <div className="studio-field-row">
          <select
            id="studio-transition"
            className="studio-select"
            value={transition}
            disabled={busy}
            onChange={(event) => setTransition(event.target.value)}
          >
            {studio.catalogues.transitions.map((name) => (
              <option key={name} value={name}>
                {name.replaceAll("_", " ")}
              </option>
            ))}
          </select>
          <select
            className="studio-select"
            value={reason}
            disabled={busy}
            onChange={(event) => setReason(event.target.value)}
            aria-label="Transition reason"
          >
            <option value="">No reason</option>
            {studio.catalogues.transition_reasons.map((name) => (
              <option key={name} value={name}>
                {name.replaceAll("_", " ")}
              </option>
            ))}
          </select>
          <input
            className="studio-input"
            style={{ width: 76 }}
            value={windowFrames}
            disabled={busy}
            onChange={(event) => setWindowFrames(event.target.value)}
            placeholder="Frames"
            aria-label="Transition window in frames"
            inputMode="numeric"
          />
          <button
            type="button"
            className="studio-btn"
            disabled={busy}
            onClick={() =>
              applyOps(
                [
                  {
                    type: "set_transition",
                    item_id: item.item_id,
                    transition,
                    ...(reason ? { reason } : {}),
                    ...(windowFrames.trim()
                      ? { window_frames: Number(windowFrames) }
                      : {}),
                  },
                ],
                `Transition set to ${transition}.`,
              )
            }
          >
            Apply
          </button>
        </div>
        <span className="studio-note">
          Match cuts are not offered: they need production-recorded continuity
          evidence the editor does not carry.
        </span>
      </div>

      <hr className="studio-divider" />
      {isGraphic ? (
        <>
          <div className="studio-field">
            <label htmlFor="studio-graphic-duration">Graphic duration (s)</label>
            <div className="studio-field-row">
              <input
                id="studio-graphic-duration"
                className="studio-input"
                style={{ width: 90 }}
                value={duration}
                disabled={busy}
                onChange={(event) => setDuration(event.target.value)}
                inputMode="decimal"
              />
              <button
                type="button"
                className="studio-btn"
                disabled={busy}
                onClick={() =>
                  applyOps(
                    [
                      {
                        type: "set_graphic_duration",
                        item_id: item.item_id,
                        duration: Number(duration),
                      },
                    ],
                    "Graphic duration updated.",
                  )
                }
              >
                Apply
              </button>
              <button
                type="button"
                className="studio-btn"
                disabled={busy}
                onClick={() =>
                  applyOps(
                    [{ type: "regenerate_graphic", item_id: item.item_id }],
                    "Graphic regenerated from its recorded inputs.",
                  )
                }
              >
                Regenerate
              </button>
            </div>
          </div>
          <div className="studio-field">
            <label htmlFor="studio-graphic-template">Assign template</label>
            <select
              id="studio-graphic-template"
              className="studio-select"
              value={template}
              disabled={busy}
              onChange={(event) => setTemplate(event.target.value)}
            >
              <option value="">Keep current template</option>
              {studio.catalogues.graphic_templates.map((name) => (
                <option key={name} value={name}>
                  {name.replaceAll("_", " ")}
                </option>
              ))}
            </select>
          </div>
          <div className="studio-field">
            <label htmlFor="studio-graphic-style">Style</label>
            <input
              id="studio-graphic-style"
              className="studio-input"
              value={style}
              disabled={busy}
              onChange={(event) => setStyle(event.target.value)}
              placeholder="editorial"
            />
          </div>
          <div className="studio-field">
            <label htmlFor="studio-graphic-inputs">Template inputs (JSON)</label>
            <textarea
              id="studio-graphic-inputs"
              className="studio-textarea"
              value={inputsText}
              disabled={busy}
              onChange={(event) => setInputsText(event.target.value)}
              spellCheck={false}
            />
          </div>
          {formError && <p className="studio-error">{formError}</p>}
          <button
            type="button"
            className="studio-btn is-primary"
            disabled={busy}
            onClick={assignGraphic}
          >
            Assign &amp; re-render graphic
          </button>
        </>
      ) : (
        <>
          <div className="studio-field">
            <label>Trim</label>
            <div className="studio-field-row">
              <input
                className="studio-input"
                style={{ width: 82 }}
                value={clipStart}
                disabled={busy}
                onChange={(event) => setClipStart(event.target.value)}
                aria-label="Source in-point (seconds)"
                inputMode="decimal"
              />
              <input
                className="studio-input"
                style={{ width: 82 }}
                value={duration}
                disabled={busy}
                onChange={(event) => setDuration(event.target.value)}
                aria-label="Duration (seconds)"
                inputMode="decimal"
              />
              <button
                type="button"
                className="studio-btn"
                disabled={busy}
                onClick={() =>
                  applyOps(
                    [
                      {
                        type: "trim_clip",
                        item_id: item.item_id,
                        clip_start: Number(clipStart),
                        duration: Number(duration),
                      },
                    ],
                    "Clip trimmed.",
                  )
                }
              >
                Apply trim
              </button>
            </div>
            <span className="studio-note">In-point and duration, in seconds.</span>
          </div>
          <div className="studio-field">
            <label htmlFor="studio-move-target">Move to sentence</label>
            <div className="studio-field-row">
              <select
                id="studio-move-target"
                className="studio-select"
                value={moveTarget}
                disabled={busy}
                onChange={(event) => setMoveTarget(event.target.value)}
              >
                {sentences.map((sentence) => (
                  <option key={sentence.sentence_number} value={sentence.sentence_number}>
                    {sentence.sentence_number}. {sentence.text.slice(0, 42)}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="studio-btn"
                disabled={busy}
                onClick={() =>
                  applyOps(
                    [
                      {
                        type: "move_clip",
                        item_id: item.item_id,
                        to_sentence_number: Number(moveTarget),
                      },
                    ],
                    `Clip moved to sentence ${moveTarget}.`,
                  )
                }
              >
                Move
              </button>
            </div>
            <span className="studio-note">
              Moving across sentences consumes explicit gap time in the target
              sentence — the server refuses when there is not enough.
            </span>
          </div>
          <div className="studio-field">
            <label htmlFor="studio-split-at">Split at offset (s)</label>
            <div className="studio-field-row">
              <input
                id="studio-split-at"
                className="studio-input"
                style={{ width: 82 }}
                value={splitAt}
                disabled={busy}
                onChange={(event) => setSplitAt(event.target.value)}
                inputMode="decimal"
              />
              <button
                type="button"
                className="studio-btn"
                disabled={busy || !splitAt.trim()}
                onClick={() =>
                  applyOps(
                    [
                      {
                        type: "split_clip",
                        item_id: item.item_id,
                        at_offset: Number(splitAt),
                      },
                    ],
                    "Clip split.",
                  )
                }
              >
                Split
              </button>
            </div>
          </div>
        </>
      )}

      <hr className="studio-divider" />
      <div className="studio-field">
        <label>Replace from Media Pool</label>
        {poolSelection && spec ? (
          <>
            <span className="studio-note">
              Selected: {poolSelection.file_name} (
              {CLASSIFICATION_LABELS[poolSelection.classification] ||
                poolSelection.classification}
              )
            </span>
            <button
              type="button"
              className="studio-btn"
              disabled={busy}
              onClick={() =>
                applyOps(
                  [{ type: "replace_clip", item_id: item.item_id, replacement: spec }],
                  `Replaced with ${poolSelection.file_name}.`,
                )
              }
            >
              Replace this {isGraphic ? "graphic" : "clip"}
            </button>
          </>
        ) : (
          <span className="studio-note">
            Select an asset in the Media Pool to replace this{" "}
            {isGraphic ? "graphic" : "clip"} with it.
          </span>
        )}
      </div>
      <div className="studio-field-row">
        <button
          type="button"
          className="studio-btn is-danger"
          disabled={busy}
          onClick={() =>
            applyOps(
              [{ type: "remove_clip", item_id: item.item_id, gap_policy: "leave_gap" }],
              "Removed; an explicit gap remains.",
            )
          }
        >
          Remove (leave gap)
        </button>
        <button
          type="button"
          className="studio-btn is-danger"
          disabled={busy}
          onClick={() =>
            applyOps(
              [{ type: "remove_clip", item_id: item.item_id, gap_policy: "close_gap" }],
              "Removed; the window absorbed the time where it could.",
            )
          }
        >
          Remove (close gap)
        </button>
      </div>
    </>
  );
}

function CaptionInspector({
  segment,
  busy,
  applyOps,
}: {
  segment: { segment_index: number; start: number; end: number; text: string; edited: boolean };
  busy: boolean;
  applyOps: (operations: EditorOperation[], note: string) => void;
}) {
  const [text, setText] = useState(segment.text);
  const [start, setStart] = useState(String(segment.start));
  const [end, setEnd] = useState(String(segment.end));
  return (
    <>
      <strong style={{ fontSize: 13.5 }}>Caption {segment.segment_index + 1}</strong>
      <div className="studio-field">
        <label htmlFor="studio-caption-text">Text</label>
        <textarea
          id="studio-caption-text"
          className="studio-textarea"
          value={text}
          disabled={busy}
          onChange={(event) => setText(event.target.value)}
        />
      </div>
      <div className="studio-field">
        <label>Timing (s)</label>
        <div className="studio-field-row">
          <input
            className="studio-input"
            style={{ width: 82 }}
            value={start}
            disabled={busy}
            onChange={(event) => setStart(event.target.value)}
            aria-label="Caption start (seconds)"
            inputMode="decimal"
          />
          <input
            className="studio-input"
            style={{ width: 82 }}
            value={end}
            disabled={busy}
            onChange={(event) => setEnd(event.target.value)}
            aria-label="Caption end (seconds)"
            inputMode="decimal"
          />
          <button
            type="button"
            className="studio-btn is-primary"
            disabled={busy}
            onClick={() =>
              applyOps(
                [
                  {
                    type: "edit_caption",
                    segment_index: segment.segment_index,
                    text,
                    start: Number(start),
                    end: Number(end),
                  },
                ],
                "Caption updated.",
              )
            }
          >
            Apply caption
          </button>
        </div>
        {segment.edited && <span className="studio-note">Previously edited.</span>}
      </div>
    </>
  );
}

function SentenceInspector({
  sentence,
  volume,
  poolSelection,
  busy,
  applyOps,
}: {
  sentence: {
    sentence_number: number;
    start: number;
    end: number;
    text: string;
    dirty: boolean;
  };
  volume: number;
  poolSelection: StudioPoolEntry | null;
  busy: boolean;
  applyOps: (operations: EditorOperation[], note: string) => void;
}) {
  const [text, setText] = useState(sentence.text);
  const [level, setLevel] = useState(volume);
  return (
    <>
      <strong style={{ fontSize: 13.5 }}>
        Narration sentence {sentence.sentence_number}
      </strong>
      <p className="studio-note">
        {sentence.start.toFixed(2)}s – {sentence.end.toFixed(2)}s. The narration is
        the timeline&apos;s spine: its windows never move; visuals are edited
        inside them.
        {sentence.dirty
          ? " This sentence is marked for re-synthesis at the next render."
          : ""}
      </p>
      <div className="studio-field">
        <label htmlFor="studio-sentence-text">Sentence text</label>
        <textarea
          id="studio-sentence-text"
          className="studio-textarea"
          value={text}
          disabled={busy}
          onChange={(event) => setText(event.target.value)}
        />
      </div>
      <button
        type="button"
        className="studio-btn"
        disabled={busy}
        onClick={() =>
          applyOps(
            [
              {
                type: "regenerate_narration_sentence",
                sentence_number: sentence.sentence_number,
                text,
              },
            ],
            "Sentence marked for re-synthesis; captions flagged stale until the render rebuilds them.",
          )
        }
      >
        Save text &amp; re-synthesise at render
      </button>
      <hr className="studio-divider" />
      <div className="studio-field">
        <label>Add from Media Pool</label>
        {poolSelection && poolSpec(poolSelection) && poolSelection.type !== "audio" ? (
          <>
            <span className="studio-note">
              Selected: {poolSelection.file_name}. Adding consumes this
              sentence&apos;s explicit gap time; the server refuses when there
              is not enough.
            </span>
            <button
              type="button"
              className="studio-btn"
              disabled={busy}
              onClick={() =>
                applyOps(
                  [
                    {
                      type: "add_clip",
                      asset: poolSpec(poolSelection) as AssetSpec,
                      sentence_number: sentence.sentence_number,
                    },
                  ],
                  `${poolSelection.file_name} added to sentence ${sentence.sentence_number}.`,
                )
              }
            >
              Add {poolSelection.file_name} here
            </button>
          </>
        ) : (
          <span className="studio-note">
            Select a visual asset in the Media Pool to add it inside this
            sentence&apos;s gap time.
          </span>
        )}
      </div>
      <hr className="studio-divider" />
      <div className="studio-field">
        <label htmlFor="studio-narration-volume">Narration volume</label>
        <input
          id="studio-narration-volume"
          className="studio-range"
          type="range"
          min={0}
          max={2}
          step={0.05}
          value={level}
          disabled={busy}
          onChange={(event) => setLevel(Number(event.target.value))}
        />
        <button
          type="button"
          className="studio-btn"
          disabled={busy}
          onClick={() =>
            applyOps([{ type: "set_volumes", narration: level }], "Narration volume updated.")
          }
        >
          Apply volume ({level.toFixed(2)})
        </button>
      </div>
    </>
  );
}

function MusicInspector({
  fileName,
  state,
  volume,
  poolSelection,
  busy,
  applyOps,
}: {
  fileName: string | null;
  state?: string | null;
  volume: number;
  poolSelection: StudioPoolEntry | null;
  busy: boolean;
  applyOps: (operations: EditorOperation[], note: string) => void;
}) {
  const [level, setLevel] = useState(volume);
  const uploadSpec =
    poolSelection && poolSelection.origin === "upload" && poolSelection.type === "audio"
      ? poolSelection
      : null;
  return (
    <>
      <strong style={{ fontSize: 13.5 }}>Music track</strong>
      <p className="studio-note">
        {fileName
          ? `Current: ${fileName}${state ? ` (${state.replaceAll("_", " ")})` : ""}. Its licence is recorded as unknown unless the production recorded one.`
          : "No music is set on this revision."}
      </p>
      <div className="studio-field">
        <label htmlFor="studio-music-volume">Music volume</label>
        <input
          id="studio-music-volume"
          className="studio-range"
          type="range"
          min={0}
          max={2}
          step={0.05}
          value={level}
          disabled={busy}
          onChange={(event) => setLevel(Number(event.target.value))}
        />
        <button
          type="button"
          className="studio-btn"
          disabled={busy}
          onClick={() =>
            applyOps([{ type: "set_volumes", music: level }], "Music volume updated.")
          }
        >
          Apply volume ({level.toFixed(2)})
        </button>
      </div>
      <hr className="studio-divider" />
      {uploadSpec ? (
        <button
          type="button"
          className="studio-btn"
          disabled={busy}
          onClick={() =>
            applyOps(
              [{ type: "set_music", upload_id: uploadSpec.upload_id ?? undefined }],
              `Music set to your upload ${uploadSpec.file_name}.`,
            )
          }
        >
          Use selected upload as music ({uploadSpec.file_name})
        </button>
      ) : (
        <p className="studio-note">
          To change the music, upload an audio file in the Media Pool and select it
          there, then use it here.
        </p>
      )}
      {fileName && (
        <button
          type="button"
          className="studio-btn is-danger"
          disabled={busy}
          onClick={() =>
            applyOps([{ type: "set_music", music_file: null }], "Music removed.")
          }
        >
          Remove music
        </button>
      )}
    </>
  );
}
