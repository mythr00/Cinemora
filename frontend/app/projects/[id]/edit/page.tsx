"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { AppNavigation } from "@/components/app-navigation";
import { StatusBadge } from "@/components/status-badge";
import { ProjectTabs } from "@/components/project-tabs";
import { ErrorState, LoadingState } from "@/components/states";
import { AssetPanel, type PendingSpec } from "@/components/editor/asset-panel";
import { PreviewMonitor } from "@/components/editor/preview-monitor";
import { PropertiesPanel } from "@/components/editor/properties-panel";
import { RevisionPanel } from "@/components/editor/revision-panel";
import { EditorTimeline } from "@/components/editor/timeline";
import type { EditorSelection } from "@/components/editor/shared";
import {
  ApiClientError,
  applyEditorOperations,
  createRevision,
  getEditor,
  getOutputs,
  getProjectStatus,
  getRevision,
  listAssets,
  listRevisions,
  requestRevisionRender,
  type AssetsPage,
  type EditorOperation,
  type ProjectOutputs,
  type RevisionView,
  type RevisionWithDocument,
  type UploadRecord,
} from "@/lib/api";

/**
 * The Stage 7 documentary editor (C-03) — a real editing surface
 * over the Stage 6 revision backend, not a mockup:
 *
 * - The page loads the editor document from GET /editor (which
 *   creates revision 0 from the latest completed run) and renders
 *   exactly what the server returns.
 * - Every edit is one atomic operation batch against the current
 *   revision; on success the batch is the NEXT revision and the
 *   page re-reads it before rendering anything. A 422 leaves the
 *   displayed document untouched (and the page re-reads the
 *   current revision to prove it). There is no local timeline
 *   state and no unsaved document: the save indicator reports the
 *   actual round-trips.
 * - Undo steps to the parent revision; Redo returns along the
 *   path Undo took; both are navigation over saved revisions.
 *   Editing from an undone state branches — the UI says so.
 * - Renders go through the real render endpoint (gaps block by
 *   default; the holdover choice is explicit), and exports are
 *   shown only as the server records them.
 */

type SaveState =
  | { kind: "idle"; text: string }
  | { kind: "saving"; text: string }
  | { kind: "saved"; text: string }
  | { kind: "error"; text: string };

const LIBRARY_PAGE_SIZE = 25;

export default function EditorPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;

  const [projectTitle, setProjectTitle] = useState<string | null>(null);
  const [current, setCurrent] = useState<RevisionWithDocument | null>(null);
  const [revisions, setRevisions] = useState<RevisionView[]>([]);
  const [outputs, setOutputs] = useState<ProjectOutputs | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [noProduction, setNoProduction] = useState(false);

  const [selection, setSelection] = useState<EditorSelection>(null);
  const [pendingSpec, setPendingSpec] = useState<PendingSpec | null>(null);
  const [sessionUploads, setSessionUploads] = useState<UploadRecord[]>([]);

  const [assets, setAssets] = useState<AssetsPage | null>(null);
  const [assetsBusy, setAssetsBusy] = useState(true);
  const [assetsError, setAssetsError] = useState<unknown>(null);

  const [playhead, setPlayhead] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [seekRequest, setSeekRequest] = useState<{ time: number; nonce: number } | null>(null);

  const [saveState, setSaveState] = useState<SaveState>({
    kind: "idle",
    text: "Every applied edit is saved on the server as a new revision — there is no unsaved local document.",
  });
  const [opError, setOpError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [busyOps, setBusyOps] = useState(false);
  const [branching, setBranching] = useState(false);
  const [redoStack, setRedoStack] = useState<string[]>([]);

  const [renderBusy, setRenderBusy] = useState(false);
  const [renderBlockedByGaps, setRenderBlockedByGaps] = useState(false);
  const [renderError, setRenderError] = useState<string | null>(null);
  const previousStatusRef = useRef<string | null>(null);

  const loadAssetsPage = useCallback(
    async (offset: number) => {
      setAssetsBusy(true);
      setAssetsError(null);
      try {
        setAssets(await listAssets(projectId, LIBRARY_PAGE_SIZE, offset));
      } catch (cause) {
        setAssetsError(cause);
      } finally {
        setAssetsBusy(false);
      }
    },
    [projectId],
  );

  const loadInitial = useCallback(async () => {
    setLoadError(null);
    setNoProduction(false);
    const editor = await getEditor(projectId);
    setCurrent(editor);
    previousStatusRef.current = editor.status;
    const [list, outputsResult, statusResult] = await Promise.all([
      listRevisions(projectId).catch(() => ({ revisions: [] as RevisionView[] })),
      getOutputs(projectId).catch(() => null),
      getProjectStatus(projectId).catch(() => null),
    ]);
    setRevisions(list.revisions);
    setOutputs(outputsResult);
    setProjectTitle(statusResult?.title ?? null);
    void loadAssetsPage(0);
  }, [projectId, loadAssetsPage]);

  useEffect(() => {
    const timer = setTimeout(() => {
      void loadInitial().catch((error) => {
        if (error instanceof ApiClientError && error.code === "no_completed_run") {
          setNoProduction(true);
        } else {
          setLoadError(error);
        }
      });
    }, 0);
    return () => clearTimeout(timer);
  }, [loadInitial]);

  // While a render runs, poll the revision until the server moves
  // it out of "rendering" — the exports and the failure state are
  // read from the server, never assumed.
  const currentStatus = current?.status;
  const currentRevisionId = current?.revision_id;
  useEffect(() => {
    if (currentStatus !== "rendering" || !currentRevisionId) return;
    const timer = setInterval(() => {
      void (async () => {
        try {
          const rev = await getRevision(projectId, currentRevisionId);
          setCurrent(rev);
          if (rev.status !== "rendering") {
            const list = await listRevisions(projectId).catch(() => null);
            if (list) setRevisions(list.revisions);
            if (rev.status === "exported") {
              setNotice("Render complete — the export is recorded below.");
            }
          }
        } catch {
          // A failed poll is not a failed render; the next tick retries.
        }
      })();
    }, 2500);
    return () => clearInterval(timer);
  }, [currentStatus, currentRevisionId, projectId]);

  const refreshRevision = useCallback(
    async (revisionId: string) => {
      const rev = await getRevision(projectId, revisionId);
      setCurrent(rev);
      previousStatusRef.current = rev.status;
      return rev;
    },
    [projectId],
  );

  async function applyOps(operations: EditorOperation[], intent: string) {
    if (!current || busyOps) return;
    const from = current;
    setBusyOps(true);
    setOpError(null);
    setNotice("");
    setSaveState({ kind: "saving", text: `Saving — ${intent}…` });
    try {
      const view = await applyEditorOperations(projectId, from.revision_id, operations);
      setRedoStack([]);
      const rev = await refreshRevision(view.revision_id);
      const list = await listRevisions(projectId).catch(() => null);
      if (list) setRevisions(list.revisions);
      const newEntries = rev.document.op_log.slice(from.document.op_log.length);
      setNotice(
        newEntries.length
          ? newEntries.map((entry) => entry.summary).join(" ")
          : `${intent} applied.`,
      );
      setSaveState({
        kind: "saved",
        text: `Saved as Revision ${view.revision_number} · ${new Date().toLocaleTimeString()}`,
      });
      setSelection(null);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "The operation batch failed.";
      setOpError(
        `${intent} was not applied — the batch was rejected and nothing was saved. ${message}`,
      );
      setSaveState({
        kind: "error",
        text: "Save failed. The timeline below is the server's unchanged document.",
      });
      // Prove the displayed document is still the server's truth.
      void refreshRevision(from.revision_id).catch(() => undefined);
    } finally {
      setBusyOps(false);
    }
  }

  async function navigateTo(revisionId: string) {
    setOpError(null);
    setNotice("");
    setRenderBlockedByGaps(false);
    setRenderError(null);
    try {
      const rev = await refreshRevision(revisionId);
      setSelection(null);
      setSaveState({
        kind: "saved",
        text: `Viewing Revision ${rev.revision_number} — saved on the server.`,
      });
    } catch (cause) {
      setOpError(cause instanceof Error ? cause.message : "That revision could not be loaded.");
    }
  }

  async function onUndo() {
    if (!current?.parent_revision_id) return;
    setRedoStack((stack) => [current.revision_id, ...stack]);
    await navigateTo(current.parent_revision_id);
  }

  async function onRedo() {
    if (!current) return;
    const [next, ...rest] = redoStack;
    if (!next) return;
    const target = revisions.find((revision) => revision.revision_id === next);
    if (!target || target.parent_revision_id !== current.revision_id) {
      setRedoStack([]);
      return;
    }
    setRedoStack(rest);
    await navigateTo(next);
  }

  async function onBranch() {
    if (!current || branching) return;
    setBranching(true);
    setOpError(null);
    try {
      const view = await createRevision(projectId, { from_revision_id: current.revision_id });
      setRedoStack([]);
      await refreshRevision(view.revision_id);
      const list = await listRevisions(projectId).catch(() => null);
      if (list) setRevisions(list.revisions);
      setSelection(null);
      setNotice(
        `Branched Revision ${view.revision_number} from Revision ${current.revision_number}. The branch starts with no exports of its own.`,
      );
      setSaveState({
        kind: "saved",
        text: `Saved as Revision ${view.revision_number} · ${new Date().toLocaleTimeString()}`,
      });
    } catch (cause) {
      setOpError(cause instanceof Error ? cause.message : "The branch could not be created.");
    } finally {
      setBranching(false);
    }
  }

  async function onRender(allowGaps: boolean) {
    if (!current || renderBusy) return;
    setRenderBusy(true);
    setRenderError(null);
    try {
      await requestRevisionRender(projectId, current.revision_id, allowGaps);
      setRenderBlockedByGaps(false);
      await refreshRevision(current.revision_id);
    } catch (cause) {
      if (cause instanceof ApiClientError && cause.code === "unresolved_gaps") {
        setRenderBlockedByGaps(true);
        setRenderError(
          "The server refused the export: unresolved gaps block rendering by default. Fill the gaps, or export anyway with each gap covered by a recorded holdover.",
        );
      } else if (cause instanceof ApiClientError && cause.code === "render_in_progress") {
        await refreshRevision(current.revision_id).catch(() => undefined);
      } else {
        setRenderError(cause instanceof Error ? cause.message : "The render could not be started.");
      }
    } finally {
      setRenderBusy(false);
    }
  }

  function onSeek(time: number) {
    setPlayhead(time);
    setSeekRequest({ time, nonce: Date.now() });
  }

  const selectionKey = !selection
    ? "none"
    : selection.kind === "item"
      ? `item:${selection.itemId}`
      : selection.kind === "caption"
        ? `caption:${selection.segmentIndex}`
        : selection.kind === "sentence"
          ? `sentence:${selection.sentenceNumber}`
          : "music";

  const canRedo =
    redoStack.length > 0 &&
    revisions.some(
      (revision) =>
        revision.revision_id === redoStack[0] &&
        revision.parent_revision_id === current?.revision_id,
    );

  return (
    <main className="project-editor-page editor-page">
      <AppNavigation />
      <ProjectTabs projectId={projectId} />
      <section className="projects-content editor-content">
        <Link href={`/projects/${projectId}`} className="editor-back">
          <ArrowLeft size={14} strokeWidth={2} aria-hidden /> Project overview
        </Link>
        <p className="eyebrow">VIDEO EDITOR</p>
        <h1>{projectTitle || "Documentary editor"}</h1>

        {!current && loadError ? (
          <>
            <h2 className="editor-state-heading">Editor unavailable</h2>
            <ErrorState
              error={loadError}
              onRetry={() => {
                void loadInitial().catch((error) => setLoadError(error));
              }}
            />
          </>
        ) : !current && noProduction ? (
          <div className="editor-empty">
            <h2>Nothing to edit yet</h2>
            <p>
              This project has no completed production run, so there is no finished film to
              revise. Complete a production first — revision 0 is created from it automatically
              when you open this editor.
            </p>
            <p>
              <Link className="button button-primary" href={`/projects/${projectId}`}>
                Open the project
              </Link>
            </p>
          </div>
        ) : !current ? (
          <LoadingState className="projects-state" label="Loading editor…" />
        ) : (
          <>
            <div className="editor-statusbar">
              <span className="editor-statusbar-revision">
                Revision {current.revision_number} <StatusBadge status={current.status} />
                {current.parent_revision_id && (
                  <span className="editor-statusbar-parent">
                    · from Revision{" "}
                    {revisions.find((r) => r.revision_id === current.parent_revision_id)
                      ?.revision_number ?? "?"}
                  </span>
                )}
              </span>
              <span
                className={`editor-save-state editor-save-state--${saveState.kind}`}
                role="status"
              >
                {saveState.text}
              </span>
            </div>
            {opError && (
              <p className="editor-error-text editor-op-error" role="alert">
                {opError}
              </p>
            )}
            {notice && (
              <p className="editor-notice" role="status">
                {notice}
              </p>
            )}

            {/* Redesign F2 (plan §3.9): layout re-housed only —
                library LEFT, monitor + timeline CENTRE, inspector
                RIGHT, revisions/exports below, revision bar (the
                statusbar above) on top. Every component, prop and
                handler is unchanged; op semantics, atomic batches,
                the gaps gate and render polling are untouched. */}
            <div className="editor-workspace">
              <AssetPanel
                projectId={projectId}
                doc={current.document}
                selectedItemId={selection?.kind === "item" ? selection.itemId : null}
                onSelectItem={(itemId) => setSelection({ kind: "item", itemId })}
                assets={assets}
                assetsBusy={assetsBusy}
                assetsError={assetsError}
                onLoadAssets={(offset) => void loadAssetsPage(offset)}
                pendingSpec={pendingSpec}
                onChooseSpec={setPendingSpec}
                sessionUploads={sessionUploads}
                onUploaded={(upload) =>
                  setSessionUploads((existing) => [upload, ...existing])
                }
              />
              <div className="editor-centre">
                <PreviewMonitor
                  projectId={projectId}
                  revisionNumber={current.revision_number}
                  filmMediaKey={outputs?.film.media_key ?? null}
                  exports={current.document.exports}
                  documentDuration={current.document.duration_seconds}
                  playhead={playhead}
                  playing={playing}
                  onPlayheadChange={setPlayhead}
                  onPlayingChange={setPlaying}
                  seekRequest={seekRequest}
                />
                <EditorTimeline
                  doc={current.document}
                  selection={selection}
                  onSelect={setSelection}
                  playhead={playhead}
                  onSeek={onSeek}
                />
              </div>
              <PropertiesPanel
                key={`${current.revision_id}:${selectionKey}`}
                projectId={projectId}
                doc={current.document}
                selection={selection}
                pendingSpec={pendingSpec}
                sessionUploads={sessionUploads}
                busy={busyOps}
                onApply={(operations, intent) => void applyOps(operations, intent)}
              />
            </div>

            <div className="editor-lower">
              <RevisionPanel
                projectId={projectId}
                revisions={revisions}
                current={current}
                canRedo={canRedo}
                branching={branching}
                onNavigate={(revisionId) => {
                  setRedoStack([]);
                  void navigateTo(revisionId);
                }}
                onUndo={() => void onUndo()}
                onRedo={() => void onRedo()}
                onBranch={() => void onBranch()}
                renderBusy={renderBusy}
                renderBlockedByGaps={renderBlockedByGaps}
                renderError={renderError}
                onRender={(allowGaps) => void onRender(allowGaps)}
              />
            </div>
          </>
        )}
      </section>
    </main>
  );
}
