"use client";

import { useState } from "react";
import type {
  EditorDocument,
  EditorOperation,
  UploadRecord,
  VisualItem,
} from "@/lib/api";
import { ClassificationChip } from "./classification-chip";
import type { PendingSpec } from "./asset-panel";
import {
  fileNameOf,
  formatDuration,
  formatTime,
  gapTimeForSentence,
  itemTitle,
  rightsLabel,
  verificationLabel,
  type EditorSelection,
} from "./shared";

/**
 * The properties + operations panel (C-03). Every control here
 * maps to exactly one Stage 6 operation with the backend's own
 * payload (see the operation→control table in the register):
 * the panel builds the operation, the page applies it as one
 * atomic batch against the current revision, and the panel then
 * re-renders from the re-read document. Controls the backend
 * cannot honour for the selected item are not rendered (a still
 * has no in-point; a graphic without recorded inputs cannot be
 * regenerated) — nothing is decorative.
 */

function parseSeconds(value: string): number | null {
  const parsed = Number(value);
  return value.trim() !== "" && Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

export function PropertiesPanel({
  doc,
  selection,
  pendingSpec,
  sessionUploads,
  busy,
  onApply,
}: {
  doc: EditorDocument;
  selection: EditorSelection;
  pendingSpec: PendingSpec | null;
  sessionUploads: UploadRecord[];
  busy: boolean;
  onApply: (operations: EditorOperation[], intent: string) => void;
}) {
  const sentences = doc.tracks.narration.sentences;
  const selectedItem: VisualItem | null =
    selection?.kind === "item"
      ? (doc.tracks.visual.find((item) => item.item_id === selection.itemId) ?? null)
      : null;
  const selectedSentence =
    selection?.kind === "sentence"
      ? (sentences.find((s) => s.sentence_number === selection.sentenceNumber) ?? null)
      : null;
  const selectedCaption =
    selection?.kind === "caption" ? (doc.tracks.captions.segments[selection.segmentIndex] ?? null) : null;

  // Form state (initialised from the server document; the page
  // remounts this panel per selection + revision).
  const [trimStart, setTrimStart] = useState(
    selectedItem?.asset ? String(selectedItem.asset.clip_start ?? 0) : "0",
  );
  const [trimDuration, setTrimDuration] = useState(
    selectedItem ? String(selectedItem.duration) : "",
  );
  const [splitOffset, setSplitOffset] = useState(
    selectedItem ? String(Math.max(0.1, Math.round((selectedItem.duration / 2) * 10) / 10)) : "",
  );
  const [moveSentence, setMoveSentence] = useState(
    selectedItem ? String(selectedItem.sentence_number) : "",
  );
  const [moveAfter, setMoveAfter] = useState("");
  const [addSentence, setAddSentence] = useState(() => {
    if (selectedItem) return String(selectedItem.sentence_number);
    const withGap = sentences.find((s) => gapTimeForSentence(doc.tracks.visual, s.sentence_number) > 0);
    return String((withGap ?? sentences[0])?.sentence_number ?? "");
  });
  const [addDuration, setAddDuration] = useState("");
  const [captionText, setCaptionText] = useState(selectedCaption?.text ?? "");
  const [captionStart, setCaptionStart] = useState(
    selectedCaption ? String(selectedCaption.start) : "",
  );
  const [captionEnd, setCaptionEnd] = useState(selectedCaption ? String(selectedCaption.end) : "");
  const [narrationText, setNarrationText] = useState(selectedSentence?.text ?? "");
  const [musicUploadId, setMusicUploadId] = useState("");
  const [volumeNarration, setVolumeNarration] = useState(String(doc.volumes.narration));
  const [volumeMusic, setVolumeMusic] = useState(String(doc.volumes.music));
  const [formError, setFormError] = useState("");

  const audioUploads = sessionUploads.filter((upload) => upload.type === "audio");
  const moveTarget = parseSeconds(moveSentence);
  const moveTargetItems =
    moveTarget !== null
      ? doc.tracks.visual.filter((item) => item.sentence_number === moveTarget)
      : [];
  const addTarget = parseSeconds(addSentence);
  const addGap = addTarget !== null ? gapTimeForSentence(doc.tracks.visual, addTarget) : 0;

  function guard(run: () => void) {
    setFormError("");
    run();
  }

  return (
    <section className="editor-panel editor-properties" aria-label="Properties and operations">
      <h2>Properties</h2>
      {formError && <p className="editor-error-text" role="alert">{formError}</p>}

      {!selection && (
        <p className="editor-panel-note">
          Select a clip, graphic, gap, caption, narration sentence, or the music track on the
          timeline to inspect it and see the operations that apply to it.
        </p>
      )}

      {/* ------------------------------------------------ item */}
      {selection?.kind === "item" && !selectedItem && (
        <p className="editor-panel-note">That item is not in this revision.</p>
      )}
      {selectedItem && (
        <div className="editor-properties-body">
          <h3>{itemTitle(selectedItem)}</h3>
          <p><ClassificationChip classification={selectedItem.classification} /></p>
          <dl className="editor-detail-grid">
            <div><dt>Kind</dt><dd>{selectedItem.kind}</dd></div>
            <div><dt>Sentence</dt><dd>{selectedItem.sentence_number} (scene {selectedItem.scene_number})</dd></div>
            <div><dt>Timeline</dt><dd>{formatTime(selectedItem.start)} – {formatTime(selectedItem.end)}</dd></div>
            <div><dt>Duration</dt><dd>{formatDuration(selectedItem.duration)}</dd></div>
            {selectedItem.assignment && <div><dt>Assignment</dt><dd>{selectedItem.assignment.replaceAll("_", " ")}</dd></div>}
            {selectedItem.flags.length > 0 && (
              <div><dt>Flags</dt><dd>{selectedItem.flags.join(", ").replaceAll("_", " ")}</dd></div>
            )}
          </dl>

          {selectedItem.kind === "gap" && (
            <p className="editor-panel-note">
              An explicit gap: time in sentence {selectedItem.sentence_number} with no visual.
              It is never silently filled. Add footage into it below, or export with the gap
              covered by a recorded holdover (the render panel explains the choice).
            </p>
          )}

          {selectedItem.asset && (
            <>
              <h4>Asset</h4>
              <dl className="editor-detail-grid">
                <div><dt>File</dt><dd>{fileNameOf(selectedItem.asset.file_path)}</dd></div>
                <div><dt>Type</dt><dd>{selectedItem.asset.type}</dd></div>
                <div><dt>Source</dt><dd>{selectedItem.asset.provider || selectedItem.asset.source || "—"}</dd></div>
                {selectedItem.asset.source_url && (
                  <div><dt>Source page</dt><dd><a href={selectedItem.asset.source_url} target="_blank" rel="noreferrer">view source</a></dd></div>
                )}
                <div><dt>Source interval</dt><dd>
                  in-point {formatDuration(selectedItem.asset.clip_start)} of{" "}
                  {formatDuration(selectedItem.asset.source_duration)} recorded
                </dd></div>
                <div><dt>Evidence</dt><dd>{selectedItem.asset.evidence_class?.replaceAll("_", " ") || "—"}</dd></div>
                <div><dt>Verification</dt><dd>{verificationLabel(selectedItem.asset.verification ?? null)}</dd></div>
                <div><dt>Rights</dt><dd>{rightsLabel(selectedItem.asset)}</dd></div>
                {selectedItem.asset.rights?.creator && (
                  <div><dt>Creator</dt><dd>{selectedItem.asset.rights.creator}</dd></div>
                )}
                {selectedItem.asset.user_supplied && <div><dt>Origin</dt><dd>User supplied</dd></div>}
                {selectedItem.asset.swapped_from && (
                  <div><dt>Replaced</dt><dd>Original evidence was not inherited; this asset earned its own probe and verdict.</dd></div>
                )}
              </dl>
            </>
          )}

          {selectedItem.kind === "graphic" && selectedItem.graphic && (
            <>
              <h4>Graphic</h4>
              <dl className="editor-detail-grid">
                <div><dt>Template</dt><dd>{selectedItem.graphic.template || "—"}</dd></div>
                <div><dt>Family</dt><dd>{selectedItem.graphic.family || "—"}</dd></div>
                <div><dt>Style</dt><dd>{selectedItem.graphic.style || "—"}</dd></div>
                <div><dt>Recorded inputs</dt><dd>
                  {selectedItem.graphic.inputs
                    ? Object.keys(selectedItem.graphic.inputs).join(", ")
                    : "none recorded"}
                </dd></div>
              </dl>
            </>
          )}

          {selectedItem.kind !== "gap" && (
            <div className="editor-ops">
              <h4>Trim</h4>
              <div className="editor-field-row">
                {selectedItem.asset?.type !== "image" && (
                  <label>
                    In-point (s)
                    <input type="number" min={0} step={0.1} value={trimStart} onChange={(e) => setTrimStart(e.target.value)} />
                  </label>
                )}
                <label>
                  Duration (s)
                  <input type="number" min={0.1} step={0.1} value={trimDuration} onChange={(e) => setTrimDuration(e.target.value)} />
                </label>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    guard(() => {
                      const duration = parseSeconds(trimDuration);
                      if (duration === null || duration <= 0) {
                        setFormError("Enter a duration above zero.");
                        return;
                      }
                      if (selectedItem.kind === "graphic") {
                        onApply(
                          [{ type: "set_graphic_duration", item_id: selectedItem.item_id, duration }],
                          "Set graphic duration",
                        );
                        return;
                      }
                      const op: EditorOperation = {
                        type: "trim_clip",
                        item_id: selectedItem.item_id,
                        duration,
                      };
                      if (selectedItem.asset?.type !== "image") {
                        const start = parseSeconds(trimStart);
                        if (start === null) {
                          setFormError("Enter a valid in-point.");
                          return;
                        }
                        op.clip_start = start;
                      }
                      onApply([op], "Trim clip");
                    })
                  }
                >
                  {selectedItem.kind === "graphic" ? "Set duration" : "Apply trim"}
                </button>
              </div>
              <p className="editor-panel-note">
                Shortening frees the time into an explicit gap in this sentence; lengthening
                consumes gap time there. Bounds come from the probed source interval of the asset.
              </p>

              <h4>Split</h4>
              <div className="editor-field-row">
                <label>
                  Offset (s)
                  <input type="number" min={0.1} step={0.1} value={splitOffset} onChange={(e) => setSplitOffset(e.target.value)} />
                </label>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    guard(() => {
                      const offset = parseSeconds(splitOffset);
                      if (offset === null || offset <= 0) {
                        setFormError("Enter a split offset above zero.");
                        return;
                      }
                      onApply(
                        [{ type: "split_clip", item_id: selectedItem.item_id, at_offset: offset }],
                        "Split clip",
                      );
                    })
                  }
                >
                  Split clip
                </button>
              </div>
              <p className="editor-panel-note">
                Both halves keep the same asset, evidence and decision record.
              </p>

              <h4>Move</h4>
              <div className="editor-field-row">
                <label>
                  To sentence
                  <select value={moveSentence} onChange={(e) => { setMoveSentence(e.target.value); setMoveAfter(""); }}>
                    {sentences.map((s) => (
                      <option key={s.sentence_number} value={s.sentence_number}>
                        Sentence {s.sentence_number}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Position
                  <select value={moveAfter} onChange={(e) => setMoveAfter(e.target.value)}>
                    <option value="">End of sentence</option>
                    {moveTargetItems
                      .filter((item) => item.item_id !== selectedItem.item_id)
                      .map((item) => (
                        <option key={item.item_id} value={item.item_id}>
                          After {itemTitle(item)}
                        </option>
                      ))}
                  </select>
                </label>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    guard(() => {
                      if (moveTarget === null) {
                        setFormError("Choose a target sentence.");
                        return;
                      }
                      const op: EditorOperation = { type: "move_clip", item_id: selectedItem.item_id };
                      if (moveTarget !== selectedItem.sentence_number) op.to_sentence_number = moveTarget;
                      if (moveAfter) op.after_item_id = moveAfter;
                      onApply([op], "Move clip");
                    })
                  }
                >
                  Move clip
                </button>
              </div>
              {moveTarget !== null && moveTarget !== selectedItem.sentence_number && (
                <p className="editor-panel-note">
                  Moving across sentences consumes {formatDuration(selectedItem.duration)} of gap
                  time in sentence {moveTarget} (available:{" "}
                  {formatDuration(gapTimeForSentence(doc.tracks.visual, moveTarget))}) and frees
                  the same time in sentence {selectedItem.sentence_number}.
                </p>
              )}

              <h4>Replace</h4>
              {pendingSpec ? (
                <div className="editor-field-row">
                  <span className="editor-panel-note">With: {pendingSpec.label}</span>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      onApply(
                        [{ type: "replace_clip", item_id: selectedItem.item_id, replacement: pendingSpec.spec }],
                        "Replace clip",
                      )
                    }
                  >
                    Replace clip
                  </button>
                </div>
              ) : (
                <p className="editor-panel-note">
                  Select a source in the asset library first (ledger asset, upload, or URL). The
                  replacement is probed fresh and re-earns its verdict — it never inherits this
                  evidence of this clip, and its rights are re-stamped unknown.
                </p>
              )}

              <h4>Remove</h4>
              <div className="editor-field-row">
                <button
                  type="button"
                  className="editor-danger-button"
                  disabled={busy}
                  onClick={() =>
                    onApply(
                      [{ type: "remove_clip", item_id: selectedItem.item_id, gap_policy: "leave_gap" }],
                      "Remove clip (leave gap)",
                    )
                  }
                >
                  Remove — leave gap
                </button>
                <button
                  type="button"
                  className="editor-danger-button"
                  disabled={busy}
                  onClick={() =>
                    onApply(
                      [{ type: "remove_clip", item_id: selectedItem.item_id, gap_policy: "close_gap" }],
                      "Remove clip (close gap)",
                    )
                  }
                >
                  Remove — close gap
                </button>
              </div>
              <p className="editor-panel-note">
                Leave gap keeps the time as an explicit gap. Close gap lets siblings inside the
                same sentence absorb the time (a still extends freely; a video only up to its
                probed headroom) and leaves any remainder as a gap. Unrelated footage is never
                inserted.
              </p>

              {selectedItem.kind === "graphic" && (
                <>
                  <h4>Regenerate graphic</h4>
                  {selectedItem.graphic?.inputs ? (
                    <div className="editor-field-row">
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          onApply(
                            [{ type: "regenerate_graphic", item_id: selectedItem.item_id }],
                            "Regenerate graphic",
                          )
                        }
                      >
                        Regenerate from recorded inputs
                      </button>
                    </div>
                  ) : (
                    <p className="editor-panel-note">
                      The template inputs of this graphic were not recorded by the production, so it
                      cannot be regenerated without inventing data. Replace it or leave it as
                      rendered.
                    </p>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      )}

      {/* ------------------------------------------------ caption */}
      {selection?.kind === "caption" && selectedCaption && (
        <div className="editor-properties-body">
          <h3>Caption {selection.segmentIndex + 1}</h3>
          {doc.tracks.captions.stale && (
            <p className="editor-warning-text" role="note">
              Captions are stale ({doc.tracks.captions.stale_reason || "narration changed"}); they
              rebuild at render time.
            </p>
          )}
          <label className="editor-field-block">
            Text
            <textarea rows={3} value={captionText} onChange={(e) => setCaptionText(e.target.value)} />
          </label>
          <div className="editor-field-row">
            <label>
              Start (s)
              <input type="number" min={0} step={0.1} value={captionStart} onChange={(e) => setCaptionStart(e.target.value)} />
            </label>
            <label>
              End (s)
              <input type="number" min={0} step={0.1} value={captionEnd} onChange={(e) => setCaptionEnd(e.target.value)} />
            </label>
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                guard(() => {
                  const start = parseSeconds(captionStart);
                  const end = parseSeconds(captionEnd);
                  if (!captionText.trim()) {
                    setFormError("Caption text cannot be empty.");
                    return;
                  }
                  if (start === null || end === null || end <= start) {
                    setFormError("Caption end must follow its start.");
                    return;
                  }
                  onApply(
                    [{ type: "edit_caption", segment_index: selection.segmentIndex, text: captionText, start, end }],
                    "Edit caption",
                  );
                })
              }
            >
              Save caption
            </button>
          </div>
          <p className="editor-panel-note">
            Saved through the single caption writer of the production, so the caption file, SRT and
            VTT stay in agreement. A text edit drops the recorded word timings of the segment; a pure
            timing shift moves them by the same amount.
          </p>
        </div>
      )}

      {/* ------------------------------------------------ sentence / narration */}
      {selection?.kind === "sentence" && selectedSentence && (
        <div className="editor-properties-body">
          <h3>Narration — sentence {selectedSentence.sentence_number}</h3>
          <dl className="editor-detail-grid">
            <div><dt>Window</dt><dd>{formatTime(selectedSentence.start)} – {formatTime(selectedSentence.end)}</dd></div>
            <div><dt>Voice</dt><dd>{doc.tracks.narration.voice_id || "Studio default voice"}</dd></div>
            <div><dt>State</dt><dd>
              {doc.tracks.narration.dirty_sentences.includes(selectedSentence.sentence_number)
                ? "Marked for re-synthesis at the next render"
                : "Recorded narration"}
            </dd></div>
          </dl>
          <label className="editor-field-block">
            Sentence text
            <textarea rows={3} value={narrationText} onChange={(e) => setNarrationText(e.target.value)} />
          </label>
          <div className="editor-field-row">
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                guard(() => {
                  const op: EditorOperation = {
                    type: "regenerate_narration_sentence",
                    sentence_number: selectedSentence.sentence_number,
                  };
                  if (narrationText.trim() && narrationText !== selectedSentence.text) {
                    op.text = narrationText;
                  }
                  onApply([op], "Regenerate narration sentence");
                })
              }
            >
              Regenerate this sentence
            </button>
          </div>
          <p className="editor-panel-note">
            Regeneration happens at render time (unchanged sentences are never re-synthesised).
            This flags the captions <strong>stale</strong> until that render rebuilds them — they
            are never silently left out of sync. The studio has one installed voice, so no voice
            choice is offered.
          </p>
        </div>
      )}

      {/* ------------------------------------------------ add footage */}
      <div className="editor-properties-body editor-add-block">
        <h3>Add footage</h3>
        <div className="editor-field-row">
          <label>
            Into sentence
            <select value={addSentence} onChange={(e) => setAddSentence(e.target.value)}>
              {sentences.map((s) => (
                <option key={s.sentence_number} value={s.sentence_number}>
                  Sentence {s.sentence_number} — {formatDuration(gapTimeForSentence(doc.tracks.visual, s.sentence_number))} gap
                </option>
              ))}
            </select>
          </label>
          <label>
            Duration (s, optional)
            <input type="number" min={0.1} step={0.1} value={addDuration} placeholder="Auto" onChange={(e) => setAddDuration(e.target.value)} />
          </label>
          <button
            type="button"
            disabled={busy || !pendingSpec}
            onClick={() =>
              guard(() => {
                if (!pendingSpec || addTarget === null) return;
                const op: EditorOperation = {
                  type: "add_clip",
                  asset: pendingSpec.spec,
                  sentence_number: addTarget,
                };
                if (addDuration.trim()) {
                  const duration = parseSeconds(addDuration);
                  if (duration === null || duration <= 0) {
                    setFormError("Enter a valid duration, or leave it blank for the backend default.");
                    return;
                  }
                  op.duration = duration;
                }
                onApply([op], "Add clip");
              })
            }
          >
            Add clip
          </button>
        </div>
        {!pendingSpec && (
          <p className="editor-panel-note">Select a source in the asset library first.</p>
        )}
        {pendingSpec && addGap <= 0 && (
          <p className="editor-panel-note">
            Sentence {addTarget} holds no gap time. Adding consumes explicit gap time only —
            remove or trim a clip in that sentence first. Coverage is never compressed or
            invented.
          </p>
        )}
      </div>

      {/* ------------------------------------------------ soundtrack & volumes */}
      <div className="editor-properties-body editor-audio-block">
        <h3>Soundtrack &amp; volumes</h3>
        <dl className="editor-detail-grid">
          <div><dt>Music</dt><dd>
            {doc.tracks.music.file
              ? `${fileNameOf(doc.tracks.music.file)}${doc.tracks.music.source ? ` · ${doc.tracks.music.source.replaceAll("_", " ")}` : ""}`
              : "No music track"}
          </dd></div>
          <div><dt>Mix state</dt><dd>{doc.tracks.music.mixed || "not recorded"}</dd></div>
        </dl>
        <div className="editor-field-row">
          <label>
            Set music from upload
            <select value={musicUploadId} onChange={(e) => setMusicUploadId(e.target.value)}>
              <option value="">Choose an audio upload…</option>
              {audioUploads.map((upload) => (
                <option key={upload.upload_id} value={upload.upload_id}>
                  {upload.file_name}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            disabled={busy || !musicUploadId}
            onClick={() =>
              onApply([{ type: "set_music", upload_id: musicUploadId }], "Set music")
            }
          >
            Set music
          </button>
          {doc.tracks.music.file && (
            <button
              type="button"
              className="editor-danger-button"
              disabled={busy}
              onClick={() => onApply([{ type: "set_music", music_file: null }], "Remove music")}
            >
              Remove music
            </button>
          )}
        </div>
        {audioUploads.length === 0 && (
          <p className="editor-panel-note">
            No audio uploads in this session yet — upload an audio file in the asset library to
            use it as the soundtrack. Uploaded music is user-supplied with licence unknown.
          </p>
        )}
        <div className="editor-field-row">
          <label>
            Narration volume (0–2)
            <input type="number" min={0} max={2} step={0.05} value={volumeNarration} onChange={(e) => setVolumeNarration(e.target.value)} />
          </label>
          <label>
            Music volume (0–2)
            <input type="number" min={0} max={2} step={0.05} value={volumeMusic} onChange={(e) => setVolumeMusic(e.target.value)} />
          </label>
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              guard(() => {
                const narration = parseSeconds(volumeNarration);
                const music = parseSeconds(volumeMusic);
                if (narration === null || music === null || narration > 2 || music > 2) {
                  setFormError("Volumes must be between 0 and 2.");
                  return;
                }
                onApply([{ type: "set_volumes", narration, music }], "Set volumes");
              })
            }
          >
            Apply volumes
          </button>
        </div>
      </div>
    </section>
  );
}
