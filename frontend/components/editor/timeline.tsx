"use client";

import { useEffect, useRef, useState } from "react";
import { ZoomIn, ZoomOut } from "lucide-react";
import type { EditorDocument } from "@/lib/api";
import {
  classificationTone,
  formatDuration,
  formatTime,
  itemTitle,
  type EditorSelection,
} from "./shared";

/**
 * The multitrack timeline (C-03): a direct rendering of the
 * revision document the server returned — visual items (clips,
 * graphics, explicit gaps), narration sentence windows, caption
 * segments and the music track, positioned proportionally to
 * time. It holds no editing state of its own: selecting an item
 * only changes the properties panel, and every edit is applied
 * through the operations API by the page, after which the whole
 * timeline re-renders from the re-read document.
 */

const TRACK_LABEL_WIDTH = 86;

export function EditorTimeline({
  doc,
  selection,
  onSelect,
  playhead,
  onSeek,
}: {
  doc: EditorDocument;
  selection: EditorSelection;
  onSelect: (selection: EditorSelection) => void;
  playhead: number;
  onSeek: (time: number) => void;
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [fitPx, setFitPx] = useState(14);
  const [zoom, setZoom] = useState(1);
  const duration = Math.max(doc.duration_seconds, 0.1);
  const pxPerSec = Math.max(fitPx * zoom, 1);
  const contentWidth = Math.ceil(duration * pxPerSec);

  useEffect(() => {
    const element = scrollRef.current;
    if (!element) return;
    const measure = () => {
      const available = element.clientWidth - TRACK_LABEL_WIDTH - 24;
      if (available > 80) setFitPx(available / duration);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [duration]);

  function seekFromClientX(clientX: number, target: HTMLElement) {
    const rect = target.getBoundingClientRect();
    const time = (clientX - rect.left) / pxPerSec;
    onSeek(Math.min(Math.max(time, 0), duration));
  }

  const tickStep = pxPerSec >= 36 ? 1 : pxPerSec >= 10 ? 5 : 15;
  const ticks: number[] = [];
  for (let t = 0; t <= duration; t += tickStep) ticks.push(t);

  const captions = doc.tracks.captions;
  const music = doc.tracks.music;
  const playheadLeft = TRACK_LABEL_WIDTH + playhead * pxPerSec;

  return (
    <section className="editor-timeline" aria-label="Timeline">
      <div className="editor-timeline-head">
        <h2>Timeline</h2>
        <span className="editor-timeline-meta">
          {doc.tracks.visual.length} visual items · {doc.tracks.narration.sentences.length} sentences ·{" "}
          {formatDuration(doc.duration_seconds)}
        </span>
        <span className="editor-timeline-zoom">
          <button type="button" className="editor-icon-button" aria-label="Zoom timeline out" onClick={() => setZoom((z) => Math.max(z / 1.5, 0.25))}>
            <ZoomOut size={14} aria-hidden />
          </button>
          <button type="button" className="editor-zoom-fit" onClick={() => setZoom(1)}>Fit</button>
          <button type="button" className="editor-icon-button" aria-label="Zoom timeline in" onClick={() => setZoom((z) => Math.min(z * 1.5, 12))}>
            <ZoomIn size={14} aria-hidden />
          </button>
        </span>
      </div>
      <div className="editor-timeline-scroll" ref={scrollRef}>
        <div className="editor-timeline-body" style={{ width: TRACK_LABEL_WIDTH + contentWidth }}>
          <div
            className="editor-playhead"
            style={{ left: playheadLeft }}
            aria-hidden
          />
          {/* Ruler */}
          <div className="editor-track editor-track--ruler">
            <span className="editor-track-label" aria-hidden />
            <div
              className="editor-track-lane editor-ruler"
              style={{ width: contentWidth }}
              onClick={(event) => seekFromClientX(event.clientX, event.currentTarget)}
              role="presentation"
            >
              {ticks.map((t) => (
                <span key={t} className="editor-ruler-tick" style={{ left: t * pxPerSec }}>
                  {formatTime(t)}
                </span>
              ))}
              {doc.tracks.narration.sentences.map((sentence) => (
                <span
                  key={sentence.sentence_number}
                  className="editor-ruler-sentence"
                  style={{ left: sentence.start * pxPerSec }}
                  title={`Sentence ${sentence.sentence_number}`}
                >
                  S{sentence.sentence_number}
                </span>
              ))}
            </div>
          </div>
          {/* Visual track */}
          <div className="editor-track editor-track--visual">
            <span className="editor-track-label">Visual</span>
            <div
              className="editor-track-lane"
              style={{ width: contentWidth }}
              onClick={(event) => {
                if (event.target === event.currentTarget) {
                  seekFromClientX(event.clientX, event.currentTarget);
                }
              }}
            >
              {doc.tracks.visual.map((item) => {
                const selected =
                  selection?.kind === "item" && selection.itemId === item.item_id;
                return (
                  <button
                    key={item.item_id}
                    type="button"
                    className={`editor-clip editor-clip--${item.kind} editor-chip--${classificationTone(item.classification)}${selected ? " editor-clip--selected" : ""}`}
                    style={{
                      left: item.start * pxPerSec,
                      width: Math.max(item.duration * pxPerSec, 3),
                    }}
                    title={`${itemTitle(item)} — sentence ${item.sentence_number}, ${formatTime(item.start)}–${formatTime(item.end)}`}
                    aria-pressed={selected}
                    onClick={() => {
                      onSelect({ kind: "item", itemId: item.item_id });
                      onSeek(item.start);
                    }}
                  >
                    <span className="editor-clip-title">
                      {item.kind === "gap"
                        ? `Gap · ${formatDuration(item.duration)}`
                        : itemTitle(item)}
                    </span>
                    <span className="editor-clip-meta">{formatDuration(item.duration)}</span>
                  </button>
                );
              })}
            </div>
          </div>
          {/* Narration track */}
          <div className="editor-track editor-track--narration">
            <span className="editor-track-label">Narration</span>
            <div className="editor-track-lane" style={{ width: contentWidth }}>
              {doc.tracks.narration.sentences.map((sentence) => {
                const selected =
                  selection?.kind === "sentence" &&
                  selection.sentenceNumber === sentence.sentence_number;
                const dirty = doc.tracks.narration.dirty_sentences.includes(sentence.sentence_number);
                return (
                  <button
                    key={sentence.sentence_number}
                    type="button"
                    className={`editor-clip editor-clip--narration${selected ? " editor-clip--selected" : ""}`}
                    style={{
                      left: sentence.start * pxPerSec,
                      width: Math.max((sentence.end - sentence.start) * pxPerSec, 3),
                    }}
                    title={`Sentence ${sentence.sentence_number}: ${sentence.text}`}
                    aria-pressed={selected}
                    onClick={() => {
                      onSelect({ kind: "sentence", sentenceNumber: sentence.sentence_number });
                      onSeek(sentence.start);
                    }}
                  >
                    <span className="editor-clip-title">
                      {sentence.sentence_number}. {sentence.text}
                      {dirty ? " · regenerates at render" : ""}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
          {/* Caption track */}
          <div className="editor-track editor-track--captions">
            <span className="editor-track-label">
              Captions
              {captions.stale && <span className="editor-track-warning">stale</span>}
            </span>
            <div className="editor-track-lane" style={{ width: contentWidth }}>
              {captions.segments.map((segment, index) => {
                const selected = selection?.kind === "caption" && selection.segmentIndex === index;
                return (
                  <button
                    key={index}
                    type="button"
                    className={`editor-clip editor-clip--caption${selected ? " editor-clip--selected" : ""}`}
                    style={{
                      left: segment.start * pxPerSec,
                      width: Math.max((segment.end - segment.start) * pxPerSec, 3),
                    }}
                    title={`Caption ${index + 1}: ${segment.text}`}
                    aria-pressed={selected}
                    onClick={() => {
                      onSelect({ kind: "caption", segmentIndex: index });
                      onSeek(segment.start);
                    }}
                  >
                    <span className="editor-clip-title">{segment.text}</span>
                  </button>
                );
              })}
            </div>
          </div>
          {/* Music track */}
          <div className="editor-track editor-track--music">
            <span className="editor-track-label">Music</span>
            <div className="editor-track-lane" style={{ width: contentWidth }}>
              <button
                type="button"
                className={`editor-clip editor-clip--music${selection?.kind === "music" ? " editor-clip--selected" : ""}`}
                style={{ left: 0, width: contentWidth }}
                aria-pressed={selection?.kind === "music"}
                onClick={() => onSelect({ kind: "music" })}
              >
                <span className="editor-clip-title">
                  {music.file
                    ? `Music: ${music.file.split("/").pop()}${music.source ? ` · ${music.source.replaceAll("_", " ")}` : ""} · music volume ${doc.volumes.music}`
                    : "No music track — select to add one from an upload"}
                </span>
              </button>
            </div>
          </div>
        </div>
      </div>
      {captions.stale && (
        <p className="editor-note" role="note">
          Captions are stale: {captions.stale_reason || "narration changed"}. They rebuild from the
          new narration at render time — the editor never leaves them silently out of sync.
        </p>
      )}
    </section>
  );
}
