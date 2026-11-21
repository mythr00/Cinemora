"use client";

import { useEffect, useState } from "react";
import { Film, Image as ImageIcon } from "lucide-react";
import { fetchMediaObjectUrl, mediaKeyForProjectPath, streamMediaUrl } from "@/lib/api";

/**
 * A real thumbnail for an editor timeline item's media file.
 *
 * The revision document carries absolute file paths, not media
 * keys, so the key is derived with the backend's documented
 * derivation (lib/api `mediaKeyForProjectPath`) and the bytes are
 * fetched through the authenticated media endpoint — a bare
 * <img src> cannot send the X-API-Key header. Files the media
 * index does not record (uploads, editor-generated media,
 * regenerated graphics) answer 404: the tile then falls back to
 * the neutral labelled placeholder. No fake thumbnail is ever
 * shown, and the fallback says why.
 */
export function ItemThumb({
  projectId,
  filePath,
  mediaType,
  label,
}: {
  projectId: string;
  filePath: string | null;
  mediaType: string;
  label: string;
}) {
  const [media, setMedia] = useState<{ url: string; contentType: string } | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  // Callers key this component by file path, so a changed file
  // remounts it and the state below only ever describes one file.
  useEffect(() => {
    if (!filePath) return;
    const controller = new AbortController();
    let objectUrl: string | null = null;
    void (async () => {
      try {
        const key = await mediaKeyForProjectPath(projectId, filePath);
        if (!key) throw new Error("path outside project");
        const streamUrl = streamMediaUrl(projectId, key);
        if (streamUrl) {
          // Same-origin deployments stream the thumbnail source
          // directly; an element error (unrecorded file) still
          // lands on the honest fallback via onError below.
          if (!controller.signal.aborted) {
            setMedia({
              url: streamUrl,
              contentType: mediaType === "video" ? "video/mp4" : "image/*",
            });
          }
          return;
        }
        const result = await fetchMediaObjectUrl(projectId, key, controller.signal);
        objectUrl = result.url;
        if (!controller.signal.aborted) setMedia(result);
      } catch {
        if (!controller.signal.aborted) setUnavailable(true);
      }
    })();
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [projectId, filePath, mediaType]);

  if (unavailable || !filePath) {
    const Icon = mediaType === "image" ? ImageIcon : Film;
    return (
      <span className="editor-thumb editor-thumb-fallback" role="img" aria-label={`No preview available for ${label}`}>
        <Icon size={15} strokeWidth={1.8} aria-hidden />
        <small>No preview</small>
      </span>
    );
  }
  if (!media) {
    return (
      <span className="editor-thumb editor-thumb-fallback" role="status" aria-label={`Loading preview for ${label}`}>
        <Film size={15} strokeWidth={1.8} aria-hidden />
        <small>Loading…</small>
      </span>
    );
  }
  if (media.contentType.startsWith("video/") || mediaType === "video") {
    return (
      <video
        className="editor-thumb"
        src={media.url}
        preload="metadata"
        muted
        playsInline
        aria-label={label}
        onError={() => setUnavailable(true)}
      />
    );
  }
  // eslint-disable-next-line @next/next/no-img-element -- authenticated media URL; next/image cannot send the API token header
  return <img className="editor-thumb" src={media.url} alt={label} onError={() => setUnavailable(true)} />;
}
