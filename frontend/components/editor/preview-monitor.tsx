"use client";

import { useEffect, useRef, useState } from "react";
import { Pause, Play } from "lucide-react";
import { fetchMediaObjectUrl, mediaKeyForProjectPath, type RevisionExport } from "@/lib/api";
import { formatTime } from "./shared";

/**
 * The editor's preview monitor (C-03): plays the actual film —
 * the current revision's latest export when the media endpoint
 * can serve it, otherwise the original production film — through
 * the authenticated media path (a bare <video src> cannot send
 * the X-API-Key header, so the bytes arrive as an object URL via
 * fetchMediaObjectUrl).
 *
 * Honest fallback, surfaced in the UI when it occurs: if the
 * latest revision export cannot be loaded for preview — no
 * export is available yet, or the preview request itself fails —
 * the monitor falls back to the original production film and
 * says so; it never pretends the export is playing when it is
 * not. (IA-10 / F-05: an earlier note here claimed the media
 * endpoint does not serve revision exports; that is no longer
 * the reason a preview falls back, and the copy now states the
 * true one.)
 */

type SourceState =
  | { kind: "loading" }
  | { kind: "ready"; url: string; label: string; isExport: boolean }
  | { kind: "unavailable"; message: string };

export function PreviewMonitor({
  projectId,
  revisionNumber,
  filmMediaKey,
  exports,
  documentDuration,
  playhead,
  playing,
  onPlayheadChange,
  onPlayingChange,
  seekRequest,
}: {
  projectId: string;
  revisionNumber: number;
  filmMediaKey: string | null;
  exports: RevisionExport[];
  documentDuration: number;
  playhead: number;
  playing: boolean;
  onPlayheadChange: (time: number) => void;
  onPlayingChange: (playing: boolean) => void;
  seekRequest: { time: number; nonce: number } | null;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const latestExport = exports.length ? exports[exports.length - 1] : null;
  // The resolved source is tagged with the inputs it was resolved
  // for; while the tag is stale the monitor reads as "loading", so
  // the effect below never has to reset state synchronously.
  const sourceKey = `${projectId}|${latestExport?.export_id ?? ""}|${filmMediaKey ?? ""}`;
  const [resolved, setResolved] = useState<{
    key: string;
    source: SourceState;
    exportNote: string | null;
  } | null>(null);
  const [mediaDuration, setMediaDuration] = useState<{ url: string; duration: number } | null>(null);
  const source: SourceState =
    resolved && resolved.key === sourceKey ? resolved.source : { kind: "loading" };
  const exportNote = resolved && resolved.key === sourceKey ? resolved.exportNote : null;

  // Resolve the playable source: latest export of this revision
  // first, original production film as the fallback.
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | null = null;
    void (async () => {
      let note: string | null = null;
      if (latestExport) {
        try {
          const key = await mediaKeyForProjectPath(projectId, latestExport.file);
          if (!key) throw new Error("no key");
          const result = await fetchMediaObjectUrl(projectId, key, controller.signal);
          objectUrl = result.url;
          if (!controller.signal.aborted) {
            setResolved({
              key: sourceKey,
              exportNote: null,
              source: {
                kind: "ready",
                url: result.url,
                isExport: true,
                label: `Revision ${revisionNumber} export — ${latestExport.file_name} (rendered ${new Date(latestExport.at).toLocaleString()})`,
              },
            });
          }
          return;
        } catch {
          if (controller.signal.aborted) return;
          note =
            "The latest revision export could not be loaded for preview, so the preview shows the original production film instead.";
        }
      }
      if (filmMediaKey) {
        try {
          const result = await fetchMediaObjectUrl(projectId, filmMediaKey, controller.signal);
          objectUrl = result.url;
          if (!controller.signal.aborted) {
            setResolved({
              key: sourceKey,
              exportNote: note,
              source: {
                kind: "ready",
                url: result.url,
                isExport: false,
                label:
                  exports.length === 0
                    ? "Original production film — this revision has not been rendered yet"
                    : "Original production film",
              },
            });
          }
          return;
        } catch {
          // Fall through to unavailable.
        }
      }
      if (!controller.signal.aborted) {
        setResolved({
          key: sourceKey,
          exportNote: note,
          source: {
            kind: "unavailable",
            message:
              "No playable media is available for this project. The production film could not be loaded from the media endpoint.",
          },
        });
      }
    })();
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- sourceKey captures the resolution inputs
  }, [sourceKey]);

  // Timeline → monitor seek requests.
  useEffect(() => {
    if (!seekRequest) return;
    const video = videoRef.current;
    if (video) video.currentTime = seekRequest.time;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- nonce is the trigger
  }, [seekRequest?.nonce]);

  const ready = source.kind === "ready" ? source : null;
  const total =
    (ready && mediaDuration?.url === ready.url ? mediaDuration.duration : null) ??
    documentDuration;

  function togglePlay() {
    const video = videoRef.current;
    if (!video) return;
    if (video.paused) {
      void video.play().catch(() => undefined);
    } else {
      video.pause();
    }
  }

  return (
    <section className="editor-preview" aria-label="Preview monitor">
      <div className="editor-preview-stage">
        {ready ? (
          <video
            ref={videoRef}
            className="editor-preview-video"
            src={ready.url}
            preload="auto"
            playsInline
            onLoadedMetadata={(event) =>
              setMediaDuration({ url: ready.url, duration: event.currentTarget.duration || 0 })
            }
            onTimeUpdate={(event) => onPlayheadChange(event.currentTarget.currentTime)}
            onPlay={() => onPlayingChange(true)}
            onPause={() => onPlayingChange(false)}
            onEnded={() => onPlayingChange(false)}
          />
        ) : (
          <div className="editor-preview-empty" role={source.kind === "unavailable" ? "alert" : "status"}>
            {source.kind === "loading"
              ? "Loading preview…"
              : source.kind === "unavailable"
                ? source.message
                : ""}
          </div>
        )}
      </div>
      <div className="editor-preview-controls">
        <button
          type="button"
          className="editor-icon-button"
          onClick={togglePlay}
          disabled={!ready}
          aria-label={playing ? "Pause preview" : "Play preview"}
        >
          {playing ? <Pause size={16} aria-hidden /> : <Play size={16} aria-hidden />}
        </button>
        <span className="editor-preview-time">
          {formatTime(playhead)} / {formatTime(total)}
        </span>
        <input
          type="range"
          className="editor-scrubber"
          min={0}
          max={Math.max(total, 0.1)}
          step={0.05}
          value={Math.min(playhead, total)}
          disabled={!ready}
          aria-label="Seek"
          onChange={(event) => {
            const time = Number(event.target.value);
            onPlayheadChange(time);
            const video = videoRef.current;
            if (video) video.currentTime = time;
          }}
        />
      </div>
      <p className="editor-preview-label">
        {ready ? <>Previewing: {ready.label}</> : "Preview unavailable"}
      </p>
      {exportNote && <p className="editor-note" role="note">{exportNote}</p>}
    </section>
  );
}
