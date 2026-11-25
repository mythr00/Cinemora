"use client";

import { useEffect, useState } from "react";
import { AuthenticatedDownloadLink } from "@/components/authenticated-media";
import { type RetentionView } from "@/lib/api";

/**
 * §21 retention warning (Phase 8 MON): the exact founder
 * headline, the exact expiry, a live countdown and a prominent
 * download action. Data is the server's retention registry —
 * nothing here is estimated.
 *
 * Extracted verbatim from app/projects/[id]/page.tsx (Phase 9)
 * so the warning can be unit-tested; behaviour is unchanged.
 */
export function RetentionWarning({
  retention,
  filmPath,
  filmFilename,
}: {
  retention: RetentionView;
  filmPath: string;
  filmFilename: string;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  if (!retention) return null;
  const video =
    retention.videos.find((v) => v.kind === "final" && v.status === "active") ??
    retention.videos.find((v) => v.status === "active");
  if (!video) return null;
  const deadline = new Date(video.delete_after).getTime();
  const remainingMs = Math.max(0, deadline - now);
  const hours = Math.floor(remainingMs / 3_600_000);
  const minutes = Math.floor((remainingMs % 3_600_000) / 60_000);
  const seconds = Math.floor((remainingMs % 60_000) / 1000);
  return (
    <div className="retention-warning" role="alert">
      <h3>{retention.headline}</h3>
      <p>
        This video will be permanently deleted on{" "}
        <strong>{new Date(video.delete_after).toLocaleString()}</strong>.
        Your project, script and production history are kept — only
        the video file is deleted.
      </p>
      <p className="retention-countdown">
        {hours}h {String(minutes).padStart(2, "0")}m{" "}
        {String(seconds).padStart(2, "0")}s remaining
      </p>
      <p>
        <AuthenticatedDownloadLink
          path={filmPath}
          filename={filmFilename}
          className="button button-primary"
        >
          Download your video now
        </AuthenticatedDownloadLink>
      </p>
      <p className="hub-panel-note">
        {video.downloaded
          ? `Downloaded ${video.download_count} time${video.download_count === 1 ? "" : "s"} — keep your copy safe.`
          : "You have not downloaded this video yet."}
      </p>
    </div>
  );
}
