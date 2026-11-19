"use client";

import { useEffect, useState } from "react";
import { Film } from "lucide-react";
import { fetchMediaObjectUrl } from "@/lib/api";

/**
 * A real media thumbnail for a recorded media key (Brief Stage 5,
 * C-07). The Stage 2 placeholder TODO lived here: plan rows carried
 * a media_key but rendered no preview. The media endpoint requires
 * the X-API-Key header, which a bare <img src> cannot send, so the
 * bytes are fetched through lib/api (fetchMediaObjectUrl) and
 * rendered from an object URL. Any failure — unknown key, missing
 * file, auth — falls back to the neutral media tile; the row
 * itself never breaks, and no fake thumbnail is ever shown.
 */
export function MediaThumb({
  projectId,
  mediaKey,
  alt,
}: {
  projectId: string;
  mediaKey: string;
  alt: string;
}) {
  const [media, setMedia] = useState<{ url: string; contentType: string } | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | null = null;
    fetchMediaObjectUrl(projectId, mediaKey, controller.signal)
      .then((result) => {
        objectUrl = result.url;
        setMedia(result);
      })
      .catch(() => {
        if (!controller.signal.aborted) setFailed(true);
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [projectId, mediaKey]);

  if (failed) {
    return (
      <span className="media-thumb media-thumb-fallback" role="img" aria-label={`Preview unavailable for ${alt}`}>
        <Film size={16} strokeWidth={1.8} aria-hidden />
        <small>Preview unavailable</small>
      </span>
    );
  }
  if (!media) {
    return (
      <span className="media-thumb media-thumb-loading" role="status" aria-label={`Loading preview for ${alt}`}>
        <Film size={16} strokeWidth={1.8} aria-hidden />
        <small>Loading preview…</small>
      </span>
    );
  }
  if (media.contentType.startsWith("video/")) {
    return (
      <video
        className="media-thumb"
        src={media.url}
        preload="metadata"
        muted
        playsInline
        aria-label={alt}
      />
    );
  }
  // eslint-disable-next-line @next/next/no-img-element -- authenticated blob URL; next/image cannot send the API token header
  return <img className="media-thumb" src={media.url} alt={alt} />;
}
