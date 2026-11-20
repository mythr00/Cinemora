"use client";

import {
  useEffect,
  useState,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
} from "react";
import { Film } from "lucide-react";
import {
  downloadAuthenticatedMedia,
  fetchAuthenticatedMediaObjectUrl,
  fetchMediaObjectUrl,
} from "@/lib/api";

/**
 * Authenticated media playback + download (IA-01, audit F-02).
 *
 * The v1 media endpoint requires the X-API-Key header, which a
 * bare <video src> or <a href> cannot send — those received the
 * 401 JSON body instead of the film in authenticated deployments.
 * These are the shared replacements, following the established
 * media-thumb / editor pattern: the bytes are fetched through
 * lib/api (fetchMediaObjectUrl, token as header only — never in
 * a URL, never logged), rendered or downloaded from an object
 * URL, and the object URL is revoked on unmount / src change.
 *
 * Recorded limitation: playback fetches the whole file as a blob
 * first (see fetchAuthenticatedMediaObjectUrl), so the media
 * endpoint's Range support cannot be used here and very large
 * films cost their full size in memory.
 *
 * Failures are honest: a 401 / 404 / network failure shows the
 * neutral "Preview unavailable" tile (or an inline download
 * error), never a fake player or a dead link.
 */

type MediaSource = { projectId?: string; mediaKey?: string; src?: string };

function resolveMedia(
  source: MediaSource,
  signal: AbortSignal,
): Promise<{ url: string; contentType: string }> {
  if (source.projectId && source.mediaKey) {
    return fetchMediaObjectUrl(source.projectId, source.mediaKey, signal);
  }
  if (source.src) return fetchAuthenticatedMediaObjectUrl(source.src, signal);
  return Promise.reject(new Error("No media source was provided."));
}

export function AuthenticatedVideo({
  projectId,
  mediaKey,
  src,
  className,
  controls,
  muted,
  playsInline,
  preload,
  "aria-label": ariaLabel,
}: MediaSource & {
  className?: string;
  controls?: boolean;
  muted?: boolean;
  playsInline?: boolean;
  preload?: "none" | "metadata" | "auto";
  "aria-label"?: string;
}) {
  // The resolved media is tagged with the source it was resolved
  // for (the preview-monitor pattern): while the tag is stale the
  // component reads as loading, so a src change never renders the
  // previous — about to be revoked — object URL.
  const sourceKey = `${projectId ?? ""}|${mediaKey ?? ""}|${src ?? ""}`;
  const [resolved, setResolved] = useState<{
    key: string;
    media: { url: string; contentType: string } | null;
    failed: boolean;
  } | null>(null);
  const media =
    resolved && resolved.key === sourceKey && !resolved.failed
      ? resolved.media
      : null;
  const failed = !!resolved && resolved.key === sourceKey && resolved.failed;

  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | null = null;
    resolveMedia({ projectId, mediaKey, src }, controller.signal)
      .then((result) => {
        objectUrl = result.url;
        if (!controller.signal.aborted) {
          setResolved({ key: sourceKey, media: result, failed: false });
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setResolved({ key: sourceKey, media: null, failed: true });
        }
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- sourceKey captures the source inputs
  }, [sourceKey]);

  if (failed) {
    return (
      <span
        className="authenticated-media-state"
        role="img"
        aria-label={ariaLabel ? `${ariaLabel} — preview unavailable` : "Preview unavailable"}
      >
        <Film size={16} strokeWidth={1.8} aria-hidden />
        <small>Preview unavailable</small>
      </span>
    );
  }
  if (!media) {
    return (
      <span
        className="authenticated-media-state"
        role="status"
        aria-label={ariaLabel ? `${ariaLabel} — loading preview` : "Loading preview"}
      >
        <Film size={16} strokeWidth={1.8} aria-hidden />
        <small>Loading preview…</small>
      </span>
    );
  }
  return (
    <video
      className={className}
      src={media.url}
      controls={controls}
      muted={muted}
      playsInline={playsInline}
      preload={preload}
      aria-label={ariaLabel}
    />
  );
}

/**
 * A download control for a recorded media file. Renders an anchor
 * (so existing anchor/button styling is preserved) with no href
 * to the protected endpoint: activating it runs
 * downloadAuthenticatedMedia — blob fetch with the token as a
 * header, temporary object-URL anchor, revoke — and reports
 * "Preparing…" while fetching and an inline error on failure,
 * like the editor's revision-export download.
 */
export function AuthenticatedDownloadLink({
  projectId,
  mediaKey,
  src,
  path,
  filename,
  className,
  children,
}: MediaSource & {
  path?: string;
  filename?: string;
  className?: string;
  children: ReactNode;
}) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  async function onDownload() {
    if (busy) return;
    setBusy(true);
    setFailed(false);
    try {
      await downloadAuthenticatedMedia({
        projectId,
        mediaKey,
        path: path ?? src,
        filename,
      });
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  }

  function handleClick(event: MouseEvent<HTMLAnchorElement>) {
    event.preventDefault();
    void onDownload();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLAnchorElement>) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      void onDownload();
    }
  }

  return (
    <>
      <a
        className={className}
        role="button"
        tabIndex={0}
        aria-disabled={busy || undefined}
        onClick={handleClick}
        onKeyDown={handleKeyDown}
      >
        {busy ? "Preparing…" : children}
      </a>
      {failed && (
        <span className="authenticated-download-error" role="alert">
          {" "}
          Download unavailable — the file could not be fetched.
        </span>
      )}
    </>
  );
}
