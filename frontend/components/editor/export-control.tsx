"use client";

import { useState } from "react";
import { Download, Upload } from "lucide-react";
import type { RevisionExport } from "@/lib/api";
import { downloadRevisionExport } from "./revision-panel";

/**
 * The editor's top-bar Export control (founder specification,
 * 2026-11-20): ONE Export button → the render starts → when the
 * render completes, a Download button for the finished file is
 * presented beside it.
 *
 * This control owns no export logic of its own:
 * - Export invokes the page's existing render handler — the
 *   same `onRender` the revision panel's "Render revision"
 *   button calls, which posts the same render request for the
 *   current revision (requestRevisionRender).
 * - The gaps gate is the server's and the page's, unchanged:
 *   when a render is refused for unresolved gaps, the page sets
 *   renderBlockedByGaps and this control surfaces the same
 *   explicit "Export anyway — hold over the gaps" choice the
 *   revision panel offers (onRender(true)). The gate is never
 *   bypassed silently and no new policy is invented here.
 * - While a render runs, the state shown is the page's real
 *   render state (renderBusy / the revision's polled "rendering"
 *   status) — no progress percentage is invented.
 * - The Download button appears only for the export the page's
 *   polling recorded when a render completed in this session
 *   (producedExport), and downloads through the exact helper
 *   the revision panel's export cards use
 *   (downloadRevisionExport). A failed render shows the page's
 *   recorded failure state and no download button.
 */
export function ExportControl({
  projectId,
  status,
  renderBusy,
  renderBlockedByGaps,
  renderError,
  lastRenderError,
  producedExport,
  onRender,
}: {
  projectId: string;
  status: string;
  renderBusy: boolean;
  renderBlockedByGaps: boolean;
  renderError: string | null;
  lastRenderError: string | null;
  producedExport: RevisionExport | null;
  onRender: (allowGaps: boolean) => void;
}) {
  const rendering = status === "rendering";
  const [downloadState, setDownloadState] = useState<
    { status: "idle" } | { status: "busy" } | { status: "failed" }
  >({ status: "idle" });

  async function download() {
    if (!producedExport) return;
    setDownloadState({ status: "busy" });
    try {
      await downloadRevisionExport(projectId, producedExport);
      setDownloadState({ status: "idle" });
    } catch {
      setDownloadState({ status: "failed" });
    }
  }

  return (
    <div className="editor-export-control">
      <button
        type="button"
        className="button button-primary editor-export-button"
        disabled={renderBusy || rendering}
        onClick={() => onRender(false)}
      >
        <Upload size={15} aria-hidden />{" "}
        {rendering ? "Rendering…" : renderBusy ? "Submitting…" : "Export"}
      </button>
      {renderBlockedByGaps && (
        <button
          type="button"
          className="button button-secondary editor-render-anyway"
          disabled={renderBusy || rendering}
          onClick={() => onRender(true)}
        >
          Export anyway — hold over the gaps
        </button>
      )}
      {producedExport && !rendering && !renderBusy && (
        <button
          type="button"
          className="button button-outline editor-export-download"
          disabled={downloadState.status === "busy"}
          onClick={() => void download()}
        >
          <Download size={15} aria-hidden />{" "}
          {downloadState.status === "busy"
            ? "Preparing…"
            : `Download ${producedExport.file_name}`}
        </button>
      )}
      {rendering && (
        <span className="editor-export-note" role="status">
          Render running — the Download button for the finished file appears here when it
          completes.
        </span>
      )}
      {downloadState.status === "failed" && producedExport && (
        <span className="editor-export-note" role="alert">
          Download failed — the export file could not be fetched from the media endpoint. The
          file is stored on the server as {producedExport.file_name}; try again, and check the
          API connection if it keeps failing.
        </span>
      )}
      {renderError && (
        <span className="editor-error-text editor-export-error" role="alert">
          {renderError}
        </span>
      )}
      {!renderError && lastRenderError && status === "failed" && (
        <span className="editor-error-text editor-export-error" role="alert">
          The last render failed: {lastRenderError}
        </span>
      )}
    </div>
  );
}
