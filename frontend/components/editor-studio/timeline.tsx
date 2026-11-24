"use client";

import { useRef, useState } from "react";
import { Lock, Unlock, Volume2, VolumeX, ZoomIn, ZoomOut } from "lucide-react";
import type { StudioItem, StudioTrack, StudioView, StudioWaveform } from "@/lib/api";
import { ItemThumb } from "@/components/editor/item-thumb";
import { Waveform } from "./waveform";
import {
  formatTimecode,
  sentenceAt,
  studioItemLabel,
  type StudioSelection,
} from "./shared";

const HEADER_W = 132;
const MIN_CLIP_PX = 8;

type DragInfo = {
  item: StudioItem;
  mode: "move" | "trim-left" | "trim-right";
  startX: number;
  deltaSeconds: number;
};
type DragState = DragInfo | null;

/**
 * The Studio multitrack timeline: video / motion-graphics /
 * captions / narration / music lanes over the revision document,
 * with a mini-overview, ruler, playhead, per-track lock (an
 * editing guard) and audio mute (a real set_volumes operation).
 * Dragging a clip across sentence windows commits move_clip;
 * dragging an edge commits trim_clip (or set_graphic_duration on
 * the graphics lane) — every gesture ends in a server operation
 * that creates the next revision; nothing is edited locally.
 */
export function StudioTimeline({
  projectId,
  studio,
  playhead,
  pxPerSec,
  selection,
  locks,
  waves,
  busy,
  onZoom,
  onSeek,
  onSelect,
  onToggleLock,
  onToggleMute,
  onItemMove,
  onItemTrim,
  onGraphicResize,
}: {
  projectId: string;
  studio: StudioView;
  playhead: number;
  pxPerSec: number;
  selection: StudioSelection;
  locks: ReadonlySet<string>;
  waves: { narration: StudioWaveform | null; music: StudioWaveform | null };
  busy: boolean;
  onZoom: (pxPerSec: number) => void;
  onSeek: (t: number) => void;
  onSelect: (selection: StudioSelection) => void;
  onToggleLock: (trackId: string) => void;
  onToggleMute: (trackId: "narration" | "music") => void;
  onItemMove: (item: StudioItem, toSentenceNumber: number) => void;
  onItemTrim: (item: StudioItem, trim: { clipStart?: number; duration?: number }) => void;
  onGraphicResize: (item: StudioItem, duration: number) => void;
}) {
  const duration = studio.duration_seconds || 0;
  const [drag, setDrag] = useState<DragState>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const narrationTrack = studio.tracks.find((t) => t.id === "narration");
  const sentences =
    narrationTrack && narrationTrack.id === "narration" ? narrationTrack.sentences : [];
  const visualItems = studio.tracks
    .filter((t) => t.id === "video" || t.id === "graphics")
    .flatMap((t) => (t.id === "video" || t.id === "graphics" ? t.items : []))
    .sort((a, b) => a.start - b.start);

  const contentWidth = duration * pxPerSec;

  function seekFromClientX(clientX: number, element: HTMLElement) {
    const rect = element.getBoundingClientRect();
    const t = (clientX - rect.left) / pxPerSec;
    onSeek(Math.min(Math.max(0, t), duration));
  }

  function beginDrag(event: React.PointerEvent, item: StudioItem, trackId: string) {
    if (busy || locks.has(trackId) || item.kind === "gap") return;
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
    const offsetX = event.clientX - rect.left;
    let mode: DragInfo["mode"] = "move";
    if (item.kind === "graphic") {
      if (offsetX < rect.width - 8) return; // graphics: right-edge duration only
      mode = "trim-right";
    } else if (offsetX <= 7) {
      mode = "trim-left";
    } else if (offsetX >= rect.width - 7) {
      mode = "trim-right";
    }
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    setDrag({ item, mode, startX: event.clientX, deltaSeconds: 0 });
  }

  function updateDrag(event: React.PointerEvent) {
    if (!drag) return;
    setDrag({ ...drag, deltaSeconds: (event.clientX - drag.startX) / pxPerSec });
  }

  function endDrag() {
    if (!drag) return;
    const { item, mode, deltaSeconds } = drag;
    setDrag(null);
    if (Math.abs(deltaSeconds) < 0.02) return;
    if (mode === "move") {
      const target = sentenceAt(sentences, item.start + deltaSeconds + item.duration / 2);
      if (target && target.sentence_number !== item.sentence_number) {
        onItemMove(item, target.sentence_number);
      }
      return;
    }
    if (item.kind === "graphic") {
      const next = Math.max(0.2, item.duration + deltaSeconds);
      if (Math.abs(next - item.duration) >= 0.02) onGraphicResize(item, next);
      return;
    }
    if (mode === "trim-right") {
      const next = Math.max(0.2, item.duration + deltaSeconds);
      if (Math.abs(next - item.duration) >= 0.02) onItemTrim(item, { duration: next });
      return;
    }
    // trim-left: shift the in-point, keep the end fixed.
    const clipStart = item.asset.clip_start || 0;
    const shift = Math.min(
      Math.max(deltaSeconds, -clipStart),
      item.duration - 0.2,
    );
    if (Math.abs(shift) >= 0.02) {
      onItemTrim(item, {
        clipStart: Math.round((clipStart + shift) * 1000) / 1000,
        duration: Math.round((item.duration - shift) * 1000) / 1000,
      });
    }
  }

  function clipStyle(item: StudioItem): React.CSSProperties {
    let left = item.start * pxPerSec;
    let width = Math.max(item.duration * pxPerSec, MIN_CLIP_PX);
    if (drag && drag.item.item_id === item.item_id) {
      if (drag.mode === "move") left += drag.deltaSeconds * pxPerSec;
      if (drag.mode === "trim-right") {
        width = Math.max(0.2 * pxPerSec, width + drag.deltaSeconds * pxPerSec);
      }
      if (drag.mode === "trim-left") {
        left += drag.deltaSeconds * pxPerSec;
        width = Math.max(0.2 * pxPerSec, width - drag.deltaSeconds * pxPerSec);
      }
    }
    return { left, width };
  }

  function renderVisualClip(item: StudioItem, trackId: string) {
    const selected =
      selection?.kind === "item" && selection.itemId === item.item_id;
    return (
      <div
        key={item.item_id}
        className={`studio-clip studio-clip-kind-${item.kind === "graphic" ? "graphic" : item.kind === "gap" ? "gap" : "video"}${selected ? " is-selected" : ""}${locks.has(trackId) ? " is-locked" : ""}`}
        style={clipStyle(item)}
        role="button"
        tabIndex={0}
        aria-label={studioItemLabel(item)}
        onClick={() => item.kind !== "gap" && onSelect({ kind: "item", itemId: item.item_id })}
        onPointerDown={(event) => beginDrag(event, item, trackId)}
        onPointerMove={updateDrag}
        onPointerUp={endDrag}
      >
        {item.kind === "clip" && item.asset.file_path && (
          <span className="studio-clip-media" aria-hidden>
            <ItemThumb
              projectId={projectId}
              filePath={item.asset.file_path}
              mediaType={item.asset.type || "video"}
              label={studioItemLabel(item)}
            />
          </span>
        )}
        <span className="studio-clip-label">
          {studioItemLabel(item)} · {item.duration.toFixed(1)}s
        </span>
      </div>
    );
  }

  function renderTrack(track: StudioTrack) {
    const locked = locks.has(track.id);
    const isAudio = track.id === "narration" || track.id === "music";
    const volume =
      track.id === "narration" || track.id === "music" ? track.volume : null;
    return (
      <div key={track.id} className="studio-track">
        <div className="studio-track-head">
          <span className="studio-track-name">{track.name}</span>
          <span className="studio-track-tools">
            <button
              type="button"
              className={`studio-icon-btn${locked ? " is-on" : ""}`}
              onClick={() => onToggleLock(track.id)}
              aria-label={locked ? `Unlock ${track.name} track` : `Lock ${track.name} track`}
              title={locked ? "Track locked (editing guard)" : "Lock track (editing guard)"}
            >
              {locked ? <Lock size={13} aria-hidden /> : <Unlock size={13} aria-hidden />}
            </button>
            {isAudio && (
              <button
                type="button"
                className={`studio-icon-btn${volume === 0 ? " is-on" : ""}`}
                onClick={() => onToggleMute(track.id as "narration" | "music")}
                aria-label={volume === 0 ? `Unmute ${track.name}` : `Mute ${track.name}`}
                title={volume === 0 ? "Unmute (restores the volume)" : "Mute (sets volume 0)"}
              >
                {volume === 0 ? <VolumeX size={13} aria-hidden /> : <Volume2 size={13} aria-hidden />}
              </button>
            )}
          </span>
        </div>
        <div
          className={`studio-lane${isAudio ? " is-audio" : ""}${track.id === "captions" ? " is-captions" : ""}`}
          style={{ width: contentWidth }}
        >
          {(track.id === "video" || track.id === "graphics") &&
            track.items.map((item) => renderVisualClip(item, track.id))}
          {track.id === "captions" &&
            track.segments.map((segment) => (
              <div
                key={segment.segment_index}
                className={`studio-clip studio-clip-kind-caption${selection?.kind === "caption" && selection.segmentIndex === segment.segment_index ? " is-selected" : ""}`}
                style={{
                  left: segment.start * pxPerSec,
                  width: Math.max((segment.end - segment.start) * pxPerSec, MIN_CLIP_PX),
                }}
                role="button"
                tabIndex={0}
                aria-label={`Caption: ${segment.text}`}
                onClick={() => onSelect({ kind: "caption", segmentIndex: segment.segment_index })}
              >
                <span className="studio-clip-label">{segment.text}</span>
              </div>
            ))}
          {track.id === "narration" && (
            <>
              {waves.narration && (
                <div
                  style={{
                    position: "absolute",
                    left: 0,
                    top: 0,
                    bottom: 0,
                    width: waves.narration.duration_seconds * pxPerSec,
                  }}
                >
                  <Waveform wave={waves.narration} offsetSeconds={0} spanSeconds={waves.narration.duration_seconds} />
                </div>
              )}
              {track.sentences.map((sentence) => (
                <div
                  key={sentence.sentence_number}
                  className={`studio-clip studio-clip-kind-audio${selection?.kind === "sentence" && selection.sentenceNumber === sentence.sentence_number ? " is-selected" : ""}`}
                  style={{
                    left: sentence.start * pxPerSec,
                    width: Math.max((sentence.end - sentence.start) * pxPerSec, MIN_CLIP_PX),
                    background: "transparent",
                    borderColor: "transparent",
                  }}
                  role="button"
                  tabIndex={0}
                  aria-label={`Narration sentence ${sentence.sentence_number}: ${sentence.text}`}
                  onClick={() => onSelect({ kind: "sentence", sentenceNumber: sentence.sentence_number })}
                >
                  <span className="studio-clip-label">
                    {sentence.sentence_number}. {sentence.text}
                  </span>
                </div>
              ))}
            </>
          )}
          {track.id === "music" &&
            (track.file_name ? (
              <>
                {waves.music && (
                  <div
                    style={{
                      position: "absolute",
                      left: 0,
                      top: 0,
                      bottom: 0,
                      width: waves.music.duration_seconds * pxPerSec,
                    }}
                  >
                    <Waveform wave={waves.music} offsetSeconds={0} spanSeconds={waves.music.duration_seconds} />
                  </div>
                )}
                <div
                  className={`studio-clip studio-clip-kind-audio${selection?.kind === "music" ? " is-selected" : ""}`}
                  style={{
                    left: 0,
                    width: Math.max(contentWidth, MIN_CLIP_PX),
                    background: "transparent",
                    borderColor: "transparent",
                  }}
                  role="button"
                  tabIndex={0}
                  aria-label={`Music: ${track.file_name}`}
                  onClick={() => onSelect({ kind: "music" })}
                >
                  <span className="studio-clip-label">{track.file_name}</span>
                </div>
              </>
            ) : (
              <span className="studio-note" style={{ position: "absolute", left: 8, top: 12 }}>
                No music on this revision
                {track.state ? ` (${track.state.replaceAll("_", " ")})` : ""}
              </span>
            ))}
        </div>
      </div>
    );
  }

  const tickStep = pxPerSec >= 40 ? 1 : pxPerSec >= 14 ? 5 : 10;
  const ticks: number[] = [];
  for (let t = 0; t <= duration; t += tickStep) ticks.push(t);

  return (
    <section className="studio-timeline-pane" aria-label="Timeline">
      <div className="studio-timeline-tools">
        <strong style={{ fontSize: 12.5 }}>Timeline</strong>
        <span className="studio-note">{formatTimecode(duration)} · 30 fps</span>
        <span style={{ flex: 1 }} />
        <button
          type="button"
          className="studio-icon-btn"
          onClick={() => onZoom(Math.max(4, pxPerSec / 1.5))}
          aria-label="Zoom out timeline"
        >
          <ZoomOut size={14} aria-hidden />
        </button>
        <button
          type="button"
          className="studio-icon-btn"
          onClick={() => onZoom(Math.min(160, pxPerSec * 1.5))}
          aria-label="Zoom in timeline"
        >
          <ZoomIn size={14} aria-hidden />
        </button>
      </div>

      <div
        className="studio-mini"
        role="button"
        aria-label="Timeline overview — click to seek"
        tabIndex={0}
        onClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          onSeek(((event.clientX - rect.left) / rect.width) * duration);
        }}
      >
        {visualItems.map((item) => (
          <span
            key={item.item_id}
            className="studio-mini-seg"
            style={{
              left: `${(item.start / duration) * 100}%`,
              width: `${Math.max((item.duration / duration) * 100, 0.4)}%`,
              background:
                item.kind === "graphic"
                  ? "var(--studio-graphic-edge)"
                  : item.kind === "gap"
                    ? "var(--studio-gap-edge)"
                    : "var(--studio-video-edge)",
            }}
          />
        ))}
        <span
          className="studio-mini-playhead"
          style={{ left: `${(playhead / Math.max(duration, 0.001)) * 100}%` }}
        />
      </div>

      <div className="studio-scroll" ref={scrollRef}>
        <div className="studio-tracks" style={{ width: HEADER_W + contentWidth }}>
          <div
            className="studio-ruler"
            style={{ width: contentWidth }}
            onPointerDown={(event) => seekFromClientX(event.clientX, event.currentTarget)}
          >
            {ticks.map((t) => (
              <span key={t} className="studio-ruler-tick" style={{ left: t * pxPerSec }}>
                {formatTimecode(t).slice(3)}
              </span>
            ))}
          </div>
          {studio.tracks.map(renderTrack)}
          <div
            className="studio-playhead-line"
            style={{ left: HEADER_W + playhead * pxPerSec }}
          />
        </div>
      </div>
    </section>
  );
}
