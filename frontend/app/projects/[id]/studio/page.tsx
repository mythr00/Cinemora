"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, Redo2, Undo2 } from "lucide-react";
import { BrandLockup } from "@/components/brand-lockup";
import { ExportControl } from "@/components/editor/export-control";
import { MediaPool } from "@/components/editor-studio/media-pool";
import { PreviewViewer } from "@/components/editor-studio/preview-viewer";
import { StudioTimeline } from "@/components/editor-studio/timeline";
import { Inspector } from "@/components/editor-studio/inspector";
import { sentenceAt, type StudioSelection } from "@/components/editor-studio/shared";
import { checkEditorGate, editorGateMessage } from "@/lib/editor-gate";
import {
  ApiClientError,
  applyEditorOperations,
  createRevision,
  getEditor,
  getProjectStatus,
  getRevision,
  getStudioPool,
  getStudioView,
  getStudioWaveform,
  listRevisionExports,
  listRevisions,
  requestRevisionRender,
  type EditorOperation,
  type RevisionExport,
  type RevisionView,
  type RevisionWithDocument,
  type StudioItem,
  type StudioPoolEntry,
  type StudioView,
  type StudioWaveform,
} from "@/lib/api";

/**
 * The Editor Studio (founder priority #5): a Resolve Edit-page
 * style workspace over the SAME revision backend as the classic
 * editor — Media Pool, programme monitor showing the unrendered
 * document's real frames, a five-track timeline, and an inspector.
 * Every edit is one atomic operation batch; the response is the
 * next revision and the page re-reads the server before rendering
 * anything. Undo/redo are navigation over the revision chain.
 * The dark theme is scoped to this surface (founder decision
 * 2026-11-24); the rest of Cinemora stays pure white.
 */

const POOL_PAGE = 100;

type SaveState =
  | { kind: "idle"; text: string }
  | { kind: "saving"; text: string }
  | { kind: "saved"; text: string }
  | { kind: "error"; text: string };

export default function StudioPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;

  const [projectTitle, setProjectTitle] = useState<string | null>(null);
  const [current, setCurrent] = useState<RevisionWithDocument | null>(null);
  const [studio, setStudio] = useState<StudioView | null>(null);
  const [revisions, setRevisions] = useState<RevisionView[]>([]);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [noProduction, setNoProduction] = useState(false);
  const [gateFailed, setGateFailed] = useState(false);

  const [pool, setPool] = useState<Parameters<typeof MediaPool>[0]["pool"]>(null);
  const [poolSelection, setPoolSelection] = useState<StudioPoolEntry | null>(null);
  const [selection, setSelection] = useState<StudioSelection>(null);
  const [waves, setWaves] = useState<{
    narration: StudioWaveform | null;
    music: StudioWaveform | null;
  }>({ narration: null, music: null });

  const [playhead, setPlayhead] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [pxPerSec, setPxPerSec] = useState(14);
  const [locks, setLocks] = useState<ReadonlySet<string>>(new Set());
  const preMuteRef = useRef<{ narration?: number; music?: number }>({});

  const [busyOps, setBusyOps] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [saveState, setSaveState] = useState<SaveState>({
    kind: "idle",
    text: "Every edit is saved on the server as a new revision — there is no unsaved local document.",
  });
  const [redoStack, setRedoStack] = useState<string[]>([]);
  const [branching, setBranching] = useState(false);

  const [renderBusy, setRenderBusy] = useState(false);
  const [renderBlockedByGaps, setRenderBlockedByGaps] = useState(false);
  const [renderError, setRenderError] = useState<string | null>(null);
  const [producedExport, setProducedExport] = useState<{
    revisionId: string;
    entry: RevisionExport;
  } | null>(null);
  const previousStatusRef = useRef<string | null>(null);

  const refreshStudio = useCallback(
    async (revisionId: string) => {
      const [view, poolPage, narrationWave, musicWave] = await Promise.all([
        getStudioView(projectId, revisionId),
        getStudioPool(projectId, revisionId, POOL_PAGE, 0).catch(() => null),
        getStudioWaveform(projectId, revisionId, "narration").catch(() => null),
        getStudioWaveform(projectId, revisionId, "music").catch(() => null),
      ]);
      setStudio(view);
      setPool(poolPage);
      setWaves({ narration: narrationWave, music: musicWave });
      return view;
    },
    [projectId],
  );

  const refreshRevision = useCallback(
    async (revisionId: string) => {
      const rev = await getRevision(projectId, revisionId);
      setCurrent(rev);
      previousStatusRef.current = rev.status;
      await refreshStudio(revisionId);
      return rev;
    },
    [projectId, refreshStudio],
  );

  const loadInitial = useCallback(async () => {
    setLoadError(null);
    setNoProduction(false);
    setGateFailed(false);
    // FS-F (FS-8): the Studio gate, same predicate as the
    // classic Editor and the backend — no completed run +
    // finished video, no editing UI for ordinary users.
    const gate = await checkEditorGate(projectId);
    if (gate.kind === "not-ready") {
      setProjectTitle(gate.status?.title ?? null);
      setGateFailed(gate.failed);
      setNoProduction(true);
      return;
    }
    const editor = await getEditor(projectId);
    setCurrent(editor);
    previousStatusRef.current = editor.status;
    const [list, statusResult] = await Promise.all([
      listRevisions(projectId).catch(() => ({ revisions: [] as RevisionView[] })),
      getProjectStatus(projectId).catch(() => null),
    ]);
    setRevisions(list.revisions);
    setProjectTitle(statusResult?.title ?? null);
    await refreshStudio(editor.revision_id);
  }, [projectId, refreshStudio]);

  useEffect(() => {
    const timer = setTimeout(() => {
      void loadInitial().catch((error) => {
        if (
          error instanceof ApiClientError &&
          (error.code === "no_completed_run" ||
            error.code === "no_finished_video")
        ) {
          setNoProduction(true);
        } else {
          setLoadError(error);
        }
      });
    }, 0);
    return () => clearTimeout(timer);
  }, [loadInitial]);

  // Poll while a render runs (same discipline as the classic
  // editor): the outcome is read from the server, never assumed.
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
            await refreshStudio(currentRevisionId);
            if (rev.status === "exported") {
              setNotice("Render complete — the export is ready to download.");
              const data = await listRevisionExports(
                projectId,
                currentRevisionId,
              ).catch(() => null);
              const items = data?.exports?.length ? data.exports : rev.document.exports;
              if (items.length) {
                setProducedExport({
                  revisionId: currentRevisionId,
                  entry: items[items.length - 1],
                });
              }
            }
          }
        } catch {
          // A failed poll is not a failed render; the next tick retries.
        }
      })();
    }, 2500);
    return () => clearInterval(timer);
  }, [currentStatus, currentRevisionId, projectId, refreshStudio]);

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
      const message =
        cause instanceof Error ? cause.message : "The operation batch failed.";
      setOpError(
        `${intent} was not applied — the batch was rejected and nothing was saved. ${message}`,
      );
      setSaveState({
        kind: "error",
        text: "Save failed. The timeline below is the server's unchanged document.",
      });
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
    setProducedExport(null);
    setPlaying(false);
    try {
      const rev = await refreshRevision(revisionId);
      setSelection(null);
      setPoolSelection(null);
      setSaveState({
        kind: "saved",
        text: `Viewing Revision ${rev.revision_number} — saved on the server.`,
      });
    } catch (cause) {
      setOpError(
        cause instanceof Error ? cause.message : "That revision could not be loaded.",
      );
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
      const view = await createRevision(projectId, {
        from_revision_id: current.revision_id,
      });
      setRedoStack([]);
      await refreshRevision(view.revision_id);
      const list = await listRevisions(projectId).catch(() => null);
      if (list) setRevisions(list.revisions);
      setSelection(null);
      setNotice(
        `Branched Revision ${view.revision_number} from Revision ${current.revision_number}.`,
      );
    } catch (cause) {
      setOpError(
        cause instanceof Error ? cause.message : "The branch could not be created.",
      );
    } finally {
      setBranching(false);
    }
  }

  async function onRender(allowGaps: boolean) {
    if (!current || renderBusy) return;
    setRenderBusy(true);
    setRenderError(null);
    setProducedExport(null);
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
        setRenderError(
          cause instanceof Error ? cause.message : "The render could not be started.",
        );
      }
    } finally {
      setRenderBusy(false);
    }
  }

  function onToggleLock(trackId: string) {
    setLocks((previous) => {
      const next = new Set(previous);
      if (next.has(trackId)) next.delete(trackId);
      else next.add(trackId);
      return next;
    });
  }

  function onToggleMute(trackId: "narration" | "music") {
    if (!studio) return;
    const currentVolume = studio.volumes[trackId];
    if (currentVolume > 0) {
      preMuteRef.current[trackId] = currentVolume;
      void applyOps(
        [{ type: "set_volumes", [trackId]: 0 }],
        `${trackId === "narration" ? "Narration" : "Music"} muted`,
      );
    } else {
      const restore = preMuteRef.current[trackId] ?? 1.0;
      void applyOps(
        [{ type: "set_volumes", [trackId]: restore }],
        `${trackId === "narration" ? "Narration" : "Music"} unmuted`,
      );
    }
  }

  async function onLoadMorePool() {
    if (!current || !pool) return;
    const next = await getStudioPool(
      projectId,
      current.revision_id,
      POOL_PAGE,
      pool.items.length,
    ).catch(() => null);
    if (next) {
      setPool({ ...next, items: [...pool.items, ...next.items] });
    }
  }

  const duration = studio?.duration_seconds ?? current?.document.duration_seconds ?? 0;
  const canRedo =
    redoStack.length > 0 &&
    revisions.some(
      (revision) =>
        revision.revision_id === redoStack[0] &&
        revision.parent_revision_id === current?.revision_id,
    );

  const narrationTrack = studio?.tracks.find((t) => t.id === "narration");
  const captionsTrack = studio?.tracks.find((t) => t.id === "captions");
  const activeCaption =
    captionsTrack?.id === "captions"
      ? (sentenceAt(captionsTrack.segments, playhead)?.text ?? null)
      : null;
  const activeItem: StudioItem | null = studio
    ? (sentenceAt(
        studio.tracks
          .filter((t) => t.id === "video" || t.id === "graphics")
          .flatMap((t) => (t.id === "video" || t.id === "graphics" ? t.items : [])),
        playhead,
      ) ?? null)
    : null;
  const activeLabel = activeItem
    ? activeItem.kind === "gap"
      ? "Gap — covered by holdover at render"
      : activeItem.asset.file_name || activeItem.item_id
    : null;

  return (
    <main className="editor-studio">
      <header className="studio-topbar">
        <span className="studio-brand"><BrandLockup /></span>
        <h1>Editor Studio{projectTitle ? ` — ${projectTitle}` : ""}</h1>
        <Link href={`/projects/${projectId}/edit`} className="studio-btn">
          Classic editor
        </Link>
        <Link href={`/projects/${projectId}`} className="studio-btn">
          <ArrowLeft size={14} aria-hidden /> Project
        </Link>
        <span className="studio-spacer" />
        {current && (
          <>
            <label className="studio-note" htmlFor="studio-revision-select">
              Revision
            </label>
            <select
              id="studio-revision-select"
              className="studio-select"
              value={current.revision_id}
              onChange={(event) => void navigateTo(event.target.value)}
            >
              {revisions.map((revision) => (
                <option key={revision.revision_id} value={revision.revision_id}>
                  {revision.revision_number} — {revision.status}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="studio-btn"
              disabled={!current.parent_revision_id || busyOps}
              onClick={() => void onUndo()}
            >
              <Undo2 size={14} aria-hidden /> Undo
            </button>
            <button
              type="button"
              className="studio-btn"
              disabled={!canRedo || busyOps}
              onClick={() => void onRedo()}
            >
              <Redo2 size={14} aria-hidden /> Redo
            </button>
            <button
              type="button"
              className="studio-btn"
              disabled={branching || busyOps}
              onClick={() => void onBranch()}
            >
              Branch
            </button>
            <ExportControl
              projectId={projectId}
              status={current.status}
              renderBusy={renderBusy}
              renderBlockedByGaps={renderBlockedByGaps}
              renderError={renderError}
              lastRenderError={current.document.last_render_error}
              producedExport={
                producedExport && producedExport.revisionId === current.revision_id
                  ? producedExport.entry
                  : null
              }
              onRender={(allowGaps) => void onRender(allowGaps)}
            />
          </>
        )}
      </header>

      {!current && loadError ? (
        <div className="studio-pane">
          <h2>Studio unavailable</h2>
          <p className="studio-error">
            {loadError instanceof Error ? loadError.message : "The studio could not be loaded."}
          </p>
          <button
            type="button"
            className="studio-btn"
            onClick={() => void loadInitial().catch((error) => setLoadError(error))}
          >
            Retry
          </button>
        </div>
      ) : !current && noProduction ? (
        <div className="studio-pane">
          <h2>{editorGateMessage({ failed: gateFailed }).heading}</h2>
          <p className="studio-note">
            {editorGateMessage({ failed: gateFailed }).body}
          </p>
          <Link className="studio-btn is-primary" href={`/projects/${projectId}`}>
            Back to overview
          </Link>
        </div>
      ) : !current || !studio ? (
        <div className="studio-pane">
          <p className="studio-note">Loading the studio…</p>
        </div>
      ) : (
        <>
          <p className="studio-note" style={{ padding: "8px 16px 0" }}>
            {saveState.text}
            {notice ? ` ${notice}` : ""}
          </p>
          {opError && (
            <p className="studio-error" style={{ padding: "4px 16px 0" }}>
              {opError}
            </p>
          )}
          <div className="studio-main">
            <MediaPool
              projectId={projectId}
              pool={pool}
              selectedPoolId={poolSelection?.pool_id ?? null}
              onSelect={setPoolSelection}
              onLoadMore={() => void onLoadMorePool()}
              onUploaded={() => void refreshStudio(current.revision_id)}
              busy={busyOps}
            />
            <PreviewViewer
              projectId={projectId}
              revisionId={current.revision_id}
              duration={duration}
              playhead={playhead}
              playing={playing}
              activeLabel={activeLabel}
              captionText={activeCaption}
              onPlayheadChange={setPlayhead}
              onPlayingChange={setPlaying}
            />
            <Inspector
              studio={studio}
              selection={selection}
              poolSelection={poolSelection}
              busy={busyOps}
              applyOps={(operations, note) => void applyOps(operations, note)}
            />
          </div>
          <StudioTimeline
            projectId={projectId}
            studio={studio}
            playhead={playhead}
            pxPerSec={pxPerSec}
            selection={selection}
            locks={locks}
            waves={waves}
            busy={busyOps}
            onZoom={setPxPerSec}
            onSeek={(t) => {
              setPlaying(false);
              setPlayhead(t);
            }}
            onSelect={setSelection}
            onToggleLock={onToggleLock}
            onToggleMute={onToggleMute}
            onItemMove={(item, toSentenceNumber) =>
              void applyOps(
                [
                  {
                    type: "move_clip",
                    item_id: item.item_id,
                    to_sentence_number: toSentenceNumber,
                  },
                ],
                `Move to sentence ${toSentenceNumber}`,
              )
            }
            onItemTrim={(item, trim) =>
              void applyOps(
                [{ type: "trim_clip", item_id: item.item_id, ...trim }],
                "Trim clip",
              )
            }
            onGraphicResize={(item, nextDuration) =>
              void applyOps(
                [
                  {
                    type: "set_graphic_duration",
                    item_id: item.item_id,
                    duration: Math.round(nextDuration * 1000) / 1000,
                  },
                ],
                "Graphic duration",
              )
            }
          />
          {narrationTrack?.id === "narration" && narrationTrack.voice_id && (
            <p className="studio-note" style={{ padding: "0 16px 12px" }}>
              Narration voice: {narrationTrack.voice_id}
            </p>
          )}
        </>
      )}
    </main>
  );
}
