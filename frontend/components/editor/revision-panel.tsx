"use client";

import { useEffect, useState } from "react";
import { Download, GitBranch, Redo2, Undo2 } from "lucide-react";
import {
  downloadAuthenticatedMedia,
  getRevisionCertificate,
  listRevisionExports,
  mediaKeyForFileName,
  mediaKeyForProjectPath,
  type RevisionCertificate,
  type RevisionExport,
  type RevisionView,
  type RevisionWithDocument,
} from "@/lib/api";
import { StatusBadge } from "@/components/status-badge";
import { ErrorState, LoadingState } from "@/components/states";
import { formatDuration } from "./shared";

/**
 * Revision history, rendering and exports (C-03).
 *
 * Revisions form a chain with branches (each names its parent);
 * the list below is the server's list, oldest first as returned.
 * Undo steps the working head back to the current revision's
 * parent; Redo returns along the path Undo took. Both are pure
 * navigation — no revision is ever rewritten — and a fresh edit
 * from an undone state branches, which the hint under the buttons
 * states plainly. Exports are the current revision's own records;
 * a failed or interrupted render records no export, and this
 * panel shows only what the server recorded.
 *
 * UI-001 (redesign F2): the export list is read from the
 * backend's exports route (listRevisionExports), which has
 * existed since Stage 7 — the document's embedded copy can lag
 * a just-completed render, and the previous build had no caller
 * for the route at all. If the route cannot be read, the panel
 * falls back to the document's embedded records. Downloads go
 * through the authenticated media endpoint: revision exports
 * ARE recorded in the media index (Stage 7B), so the old "in-app
 * download is unavailable for revision exports" note was stale
 * and is gone; a failed fetch now reports itself as a failure.
 */

export function RevisionPanel({
  projectId,
  revisions,
  current,
  canRedo,
  branching,
  onNavigate,
  onUndo,
  onRedo,
  onBranch,
  renderBusy,
  renderBlockedByGaps,
  renderError,
  onRender,
}: {
  projectId: string;
  revisions: RevisionView[];
  current: RevisionWithDocument;
  canRedo: boolean;
  branching: boolean;
  onNavigate: (revisionId: string) => void;
  onUndo: () => void;
  onRedo: () => void;
  onBranch: () => void;
  renderBusy: boolean;
  renderBlockedByGaps: boolean;
  renderError: string | null;
  onRender: (allowGaps: boolean) => void;
}) {
  const doc = current.document;
  const gaps = doc.tracks.visual.filter((item) => item.kind === "gap");
  const rendering = current.status === "rendering";

  // UI-001: the current revision's exports, read from the
  // exports route and refreshed when the revision changes or a
  // render completes (status flips to "exported"). Tagged with
  // the revision they belong to; while the tag is stale — or if
  // the route read failed — the document's embedded records are
  // shown instead, so the list never displays another revision's
  // exports.
  const [routeExports, setRouteExports] = useState<{
    revisionId: string;
    items: RevisionExport[];
  } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    listRevisionExports(projectId, current.revision_id, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) {
          setRouteExports({ revisionId: current.revision_id, items: data.exports || [] });
        }
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [projectId, current.revision_id, current.status]);
  const exports =
    routeExports && routeExports.revisionId === current.revision_id
      ? routeExports.items
      : doc.exports;
  const [certificate, setCertificate] = useState<
    | { revisionId: string; status: "loading" }
    | { revisionId: string; status: "ready"; data: RevisionCertificate }
    | { revisionId: string; status: "error"; error: unknown }
    | null
  >(null);

  const byId = new Map(revisions.map((revision) => [revision.revision_id, revision]));
  const numberOf = (revisionId: string | null) =>
    revisionId ? (byId.get(revisionId)?.revision_number ?? "?") : null;

  async function loadCertificate(revisionId: string) {
    setCertificate({ revisionId, status: "loading" });
    try {
      const data = await getRevisionCertificate(projectId, revisionId);
      setCertificate({ revisionId, status: "ready", data });
    } catch (error) {
      setCertificate({ revisionId, status: "error", error });
    }
  }

  return (
    <section className="editor-panel editor-revisions" aria-label="Revisions, render and exports">
      <h2>Revisions</h2>
      <div className="editor-revision-head">
        <span>
          Working on <strong>Revision {current.revision_number}</strong>{" "}
          <StatusBadge status={current.status} />
        </span>
        <span className="editor-revision-actions">
          <button
            type="button"
            className="editor-small-button"
            disabled={!current.parent_revision_id}
            onClick={onUndo}
            title="Step back to the parent revision"
          >
            <Undo2 size={13} aria-hidden /> Undo
          </button>
          <button
            type="button"
            className="editor-small-button"
            disabled={!canRedo}
            onClick={onRedo}
            title="Return to the revision Undo stepped back from"
          >
            <Redo2 size={13} aria-hidden /> Redo
          </button>
          <button
            type="button"
            className="editor-small-button"
            disabled={branching}
            onClick={onBranch}
            title="Copy this revision into a new draft branch"
          >
            <GitBranch size={13} aria-hidden /> {branching ? "Branching…" : "Branch"}
          </button>
        </span>
      </div>
      <p className="editor-panel-note">
        Undo and Redo navigate the saved revision history — nothing is rewritten. Editing after
        an Undo creates a branch from that earlier revision. A branch copies the document and
        starts with no exports of its own.
      </p>

      <ol className="editor-revision-list">
        {[...revisions].reverse().map((revision) => {
          const isCurrent = revision.revision_id === current.revision_id;
          return (
            <li key={revision.revision_id} className={isCurrent ? "editor-revision--current" : ""}>
              <span className="editor-revision-line">
                <strong>Revision {revision.revision_number}</strong>{" "}
                <StatusBadge status={revision.status} />
                {revision.parent_revision_id && (
                  <span className="editor-revision-parent">
                    from Revision {numberOf(revision.parent_revision_id)}
                  </span>
                )}
                <span className="editor-revision-date">
                  {new Date(revision.created_at).toLocaleString()}
                </span>
              </span>
              <span className="editor-revision-row-actions">
                {isCurrent ? (
                  <span className="editor-current-marker">Working revision</span>
                ) : (
                  <button
                    type="button"
                    className="editor-small-button"
                    onClick={() => onNavigate(revision.revision_id)}
                  >
                    Open
                  </button>
                )}
                <button
                  type="button"
                  className="editor-small-button"
                  onClick={() => void loadCertificate(revision.revision_id)}
                >
                  Certificate
                </button>
              </span>
            </li>
          );
        })}
      </ol>

      {certificate && (
        <div className="editor-certificate" aria-label="Revision certificate">
          <h3>
            Certificate — Revision {numberOf(certificate.revisionId)}
          </h3>
          {certificate.status === "loading" && <LoadingState label="Loading certificate…" />}
          {certificate.status === "error" && (
            <ErrorState
              error={certificate.error}
              onRetry={() => void loadCertificate(certificate.revisionId)}
            />
          )}
          {certificate.status === "ready" && (
            <>
              <dl className="editor-detail-grid">
                <div><dt>Decisions in trail</dt><dd>{certificate.data.summary.total}</dd></div>
                <div><dt>Rights recorded</dt><dd>{certificate.data.summary.rights_recorded}</dd></div>
                <div><dt>Rights unknown</dt><dd>{certificate.data.summary.rights_unknown}</dd></div>
                <div><dt>Final assets</dt><dd>{certificate.data.final_revision_assets.length}</dd></div>
                <div><dt>Chain</dt><dd>{certificate.data.revision.chain_revision_ids.length} revision(s) from the original</dd></div>
              </dl>
              <ul className="editor-certificate-assets">
                {certificate.data.final_revision_assets.map((asset) => (
                  <li key={asset.item_id}>
                    {asset.kind} · {asset.classification?.replaceAll("_", " ") || "—"} · sentence{" "}
                    {asset.sentence_number ?? "—"} · {asset.file_name || "no file"} · verification{" "}
                    {asset.verification?.accepted === true
                      ? "accepted"
                      : asset.verification?.accepted === false
                        ? "not accepted"
                        : "—"}{" "}
                    · rights {asset.rights?.license || "unknown"}
                  </li>
                ))}
              </ul>
              <ul className="editor-certificate-disclaimers">
                {certificate.data.disclaimers.map((disclaimer) => (
                  <li key={disclaimer}>{disclaimer}</li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      <h3>Render this revision</h3>
      {doc.last_render_error && current.status === "failed" && (
        <p className="editor-error-text" role="alert">
          The last render failed: {doc.last_render_error}
        </p>
      )}
      {gaps.length > 0 && (
        <p className="editor-warning-text" role="note">
          This revision has {gaps.length} unresolved gap(s) in sentence(s){" "}
          {[...new Set(gaps.map((gap) => gap.sentence_number))].join(", ")}. Exports are blocked
          by default: fill the gaps, or export with each gap covered by a holdover of the nearest
          genuine visual — the export record names every gap and holdover.
        </p>
      )}
      {renderError && <p className="editor-error-text" role="alert">{renderError}</p>}
      <div className="editor-field-row">
        <button
          type="button"
          className="button button-primary"
          disabled={renderBusy || rendering}
          onClick={() => onRender(false)}
        >
          {rendering ? "Rendering…" : renderBusy ? "Submitting…" : "Render revision"}
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
      </div>
      {rendering && (
        <p className="editor-panel-note" role="status">
          A render is running for this revision. This panel refreshes from the server until it
          completes; an interrupted render records no export.
        </p>
      )}
      {doc.render_state && (
        <p className="editor-panel-note">
          Last render requested {new Date(doc.render_state.requested_at).toLocaleString()}
          {doc.render_state.allow_gaps ? " with gap holdovers allowed" : ""}.
        </p>
      )}

      <h3>Exports of this revision</h3>
      {exports.length === 0 ? (
        <p className="editor-panel-note">
          No exports recorded for Revision {current.revision_number} yet. Exports appear here
          only when the server records a completed render.
        </p>
      ) : (
        <ul className="editor-export-list">
          {exports.map((entry) => (
            <ExportCard key={entry.export_id} projectId={projectId} entry={entry} />
          ))}
        </ul>
      )}
    </section>
  );
}

function ExportCard({ projectId, entry }: { projectId: string; entry: RevisionExport }) {
  const [downloadState, setDownloadState] = useState<
    { status: "idle" } | { status: "busy" } | { status: "failed" }
  >({ status: "idle" });

  async function download() {
    setDownloadState({ status: "busy" });
    try {
      // UI-001: the media key is the sha256 of the export's
      // project-relative path. The recorded absolute path does
      // not always carry the /{projectId}/ segment the path
      // derivation looks for, so fall back to the file-name
      // derivation — exports sit at the project root, where the
      // relative path is the file name (the backend's own test
      // derives the key exactly this way). The download itself
      // goes through the shared authenticated helper.
      const key =
        (await mediaKeyForProjectPath(projectId, entry.file)) ??
        (await mediaKeyForFileName(entry.file_name));
      await downloadAuthenticatedMedia({ projectId, mediaKey: key, filename: entry.file_name });
      setDownloadState({ status: "idle" });
    } catch {
      setDownloadState({ status: "failed" });
    }
  }

  return (
    <li className="editor-export-card">
      <span className="editor-export-line">
        <strong>{entry.file_name}</strong>
        <span className={`editor-chip ${entry.audit.passed ? "editor-chip--success" : "editor-chip--danger"}`}>
          Audit {entry.audit.passed ? "passed" : "failed"}
        </span>
      </span>
      <span className="editor-export-meta">
        {new Date(entry.at).toLocaleString()} · sha256 {entry.sha256.slice(0, 12)}… · duration{" "}
        {formatDuration(entry.audit.duration_seconds)}
        {entry.allow_gaps ? " · exported with gap holdovers" : ""}
      </span>
      {entry.gaps_exported.length > 0 && (
        <span className="editor-export-meta">
          Gaps exported:{" "}
          {entry.gaps_exported
            .map((gap) => `sentence ${gap.sentence_number} (${formatDuration(gap.duration)})`)
            .join(", ")}
          {entry.holdovers.length > 0
            ? ` — held over with ${entry.holdovers.map((h) => h.held_file?.split("/").pop() || "the previous visual").join(", ")}`
            : ""}
        </span>
      )}
      {entry.audit.warnings.length > 0 && (
        <span className="editor-export-meta">Warnings: {entry.audit.warnings.join("; ")}</span>
      )}
      {entry.audit.failures.length > 0 && (
        <span className="editor-export-meta">Failures: {entry.audit.failures.join("; ")}</span>
      )}
      <span className="editor-export-actions">
        <button
          type="button"
          className="editor-small-button"
          disabled={downloadState.status === "busy"}
          onClick={() => void download()}
        >
          <Download size={13} aria-hidden />{" "}
          {downloadState.status === "busy" ? "Preparing…" : "Download"}
        </button>
        {downloadState.status === "failed" && (
          <span className="editor-panel-note" role="alert">
            Download failed — the export file could not be fetched from the media endpoint.
            The file is stored on the server as {entry.file_name}; try again, and check the
            API connection if it keeps failing.
          </span>
        )}
      </span>
    </li>
  );
}
