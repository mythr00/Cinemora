"use client";

import { useEffect, useRef, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  Pause,
  Play,
  SkipBack,
  SkipForward,
} from "lucide-react";
import { fetchStudioFrameObjectUrl } from "@/lib/api";
import { formatTimecode } from "./shared";

const FRAME_SECONDS = 1 / 30;

/**
 * The Studio programme monitor: the frame the CURRENT revision
 * document shows at the playhead, cut by the backend from the
 * covering item's real source media (never the last export, never
 * a placeholder). Playback advances the playhead and steps
 * through those document frames; when the backend cannot produce
 * a frame, the monitor says so and stops.
 */
export function PreviewViewer({
  projectId,
  revisionId,
  duration,
  playhead,
  playing,
  activeLabel,
  captionText,
  onPlayheadChange,
  onPlayingChange,
}: {
  projectId: string;
  revisionId: string;
  duration: number;
  playhead: number;
  playing: boolean;
  activeLabel: string | null;
  captionText: string | null;
  onPlayheadChange: (t: number) => void;
  onPlayingChange: (playing: boolean) => void;
}) {
  const [frameUrl, setFrameUrl] = useState<string | null>(null);
  const [frameError, setFrameError] = useState<string | null>(null);
  const playheadRef = useRef(playhead);
  const requestRef = useRef(0);
  const lastFetchRef = useRef<{ t: number; at: number }>({ t: -1, at: 0 });
  const objectUrlRef = useRef<string | null>(null);

  // Fetch the document frame for the playhead (throttled while
  // playing; the backend caches by file identity + frame number).
  useEffect(() => {
    const now = performance.now();
    const last = lastFetchRef.current;
    const throttleMs = playing ? 140 : 0;
    if (playing && Math.abs(playhead - last.t) < 0.02 && now - last.at < throttleMs) {
      return;
    }
    if (!playing && Math.abs(playhead - last.t) < 0.001 && frameUrl) return;
    const request = ++requestRef.current;
    const delay = playing ? Math.max(0, throttleMs - (now - last.at)) : 40;
    const timer = setTimeout(() => {
      lastFetchRef.current = { t: playhead, at: performance.now() };
      fetchStudioFrameObjectUrl(projectId, revisionId, playhead)
        .then(({ url }) => {
          if (requestRef.current !== request) {
            URL.revokeObjectURL(url);
            return;
          }
          if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
          objectUrlRef.current = url;
          setFrameUrl(url);
          setFrameError(null);
        })
        .catch((error: unknown) => {
          if (requestRef.current !== request) return;
          setFrameError(
            error instanceof Error
              ? error.message
              : "The preview frame could not be produced.",
          );
          if (playing) onPlayingChange(false);
        });
    }, delay);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, revisionId, playhead, playing]);

  useEffect(() => {
    return () => {
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    };
  }, []);

  // Keep the ref in sync outside render (the playback loop seeds
  // from it when Play is pressed).
  useEffect(() => {
    playheadRef.current = playhead;
  }, [playhead]);

  // Playback: advance the playhead with the wall clock.
  useEffect(() => {
    if (!playing) return;
    let raf = 0;
    let last = performance.now();
    let current = playheadRef.current;
    const step = (now: number) => {
      const delta = (now - last) / 1000;
      last = now;
      current += delta;
      if (current >= duration) {
        onPlayheadChange(duration);
        onPlayingChange(false);
        return;
      }
      onPlayheadChange(current);
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, duration]);

  const seek = (t: number) => {
    onPlayingChange(false);
    onPlayheadChange(Math.min(Math.max(0, t), duration));
  };

  return (
    <section className="studio-pane studio-viewer" aria-label="Preview">
      <h2>Preview — current revision, unrendered</h2>
      <div className="studio-screen">
        {frameError ? (
          <p className="studio-screen-note">
            No preview frame at this instant: {frameError}
          </p>
        ) : frameUrl ? (
          // eslint-disable-next-line @next/next/no-img-element -- authenticated object URL; next/image cannot send credentials
          <img src={frameUrl} alt={activeLabel || "Preview frame"} />
        ) : (
          <p className="studio-screen-note">Loading the frame at the playhead…</p>
        )}
      </div>
      <div className="studio-transport">
        <button
          type="button"
          className="studio-icon-btn"
          onClick={() => seek(0)}
          aria-label="Go to start"
        >
          <SkipBack size={15} aria-hidden />
        </button>
        <button
          type="button"
          className="studio-icon-btn"
          onClick={() => seek(playhead - FRAME_SECONDS)}
          aria-label="Step back one frame"
        >
          <ChevronLeft size={15} aria-hidden />
        </button>
        <button
          type="button"
          className="studio-icon-btn"
          onClick={() => {
            if (!playing && playhead >= duration) onPlayheadChange(0);
            onPlayingChange(!playing);
          }}
          aria-label={playing ? "Pause" : "Play"}
        >
          {playing ? <Pause size={15} aria-hidden /> : <Play size={15} aria-hidden />}
        </button>
        <button
          type="button"
          className="studio-icon-btn"
          onClick={() => seek(playhead + FRAME_SECONDS)}
          aria-label="Step forward one frame"
        >
          <ChevronRight size={15} aria-hidden />
        </button>
        <button
          type="button"
          className="studio-icon-btn"
          onClick={() => seek(duration)}
          aria-label="Go to end"
        >
          <SkipForward size={15} aria-hidden />
        </button>
        <span className="studio-timecode">
          {formatTimecode(playhead)} / {formatTimecode(duration)}
        </span>
        {activeLabel && <span className="studio-note">{activeLabel}</span>}
      </div>
      <p className="studio-caption-line">{captionText || ""}</p>
    </section>
  );
}
