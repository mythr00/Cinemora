"use client";

import { useState } from "react";
import { Link2, Upload } from "lucide-react";
import {
  uploadAsset,
  type AssetSpec,
  type AssetsPage,
  type EditorDocument,
  type UploadRecord,
} from "@/lib/api";
import { ErrorState, LoadingState } from "@/components/states";
import { ClassificationChip } from "./classification-chip";
import { ItemThumb } from "./item-thumb";
import { formatDuration, itemTitle, verificationLabel } from "./shared";

/**
 * The editor's asset library (C-03): only real assets, from three
 * real sources —
 * 1. the current revision's own timeline items;
 * 2. the project's decision ledger (listAssets): every asset the
 *    production recorded, with its own evidence, verification and
 *    rights, reusable via its ledger decision id (the asset keeps
 *    ITS verdict; the editor never re-verifies it for a new
 *    sentence and flags cross-sentence placement);
 * 3. uploads from this session (the Stage 6 upload endpoint:
 *    probed, user-supplied, licence unknown) and media URLs the
 *    backend downloads and probes itself.
 * Choosing an asset sets the pending source the properties panel's
 * Add / Replace actions consume. Nothing here invents an asset,
 * a thumbnail, or a rights status.
 */

export type PendingSpec = { spec: AssetSpec; label: string };

const LIBRARY_PAGE_SIZE = 25;

function ledgerRightsLabel(rights: Record<string, unknown> | null | undefined): string {
  if (!rights) return "—";
  const license =
    typeof rights.license === "string" && rights.license ? rights.license : "unknown";
  const status =
    typeof rights.rights_status === "string" && rights.rights_status
      ? rights.rights_status.replaceAll("_", " ")
      : null;
  return status ? `${license} · ${status}` : license;
}

export function AssetPanel({
  projectId,
  doc,
  selectedItemId,
  onSelectItem,
  assets,
  assetsBusy,
  assetsError,
  onLoadAssets,
  pendingSpec,
  onChooseSpec,
  sessionUploads,
  onUploaded,
}: {
  projectId: string;
  doc: EditorDocument;
  selectedItemId: string | null;
  onSelectItem: (itemId: string) => void;
  assets: AssetsPage | null;
  assetsBusy: boolean;
  assetsError: unknown;
  onLoadAssets: (offset: number) => void;
  pendingSpec: PendingSpec | null;
  onChooseSpec: (pending: PendingSpec | null) => void;
  sessionUploads: UploadRecord[];
  onUploaded: (upload: UploadRecord) => void;
}) {
  const [url, setUrl] = useState("");
  const [uploadState, setUploadState] = useState<
    | { status: "idle" }
    | { status: "uploading"; fileName: string; progress: number }
    | { status: "error"; message: string }
  >({ status: "idle" });

  async function onPickFile(file: File | undefined) {
    if (!file) return;
    setUploadState({ status: "uploading", fileName: file.name, progress: 0 });
    try {
      const upload = await uploadAsset(projectId, file, (fraction) => {
        setUploadState({ status: "uploading", fileName: file.name, progress: fraction });
      });
      onUploaded(upload);
      onChooseSpec({
        spec: { upload_id: upload.upload_id },
        label: `Upload “${upload.file_name}” (${upload.type}, licence unknown)`,
      });
      setUploadState({ status: "idle" });
    } catch (cause) {
      setUploadState({
        status: "error",
        message: cause instanceof Error ? cause.message : "The upload failed.",
      });
    }
  }

  const assetsOffset = assets?.offset ?? 0;

  return (
    <section className="editor-panel editor-assets" aria-label="Asset library">
      <h2>Assets</h2>

      <h3>On this timeline</h3>
      <ul className="editor-asset-list">
        {doc.tracks.visual.map((item) => (
          <li key={item.item_id}>
            <button
              type="button"
              className={`editor-asset-row${selectedItemId === item.item_id ? " editor-asset-row--selected" : ""}`}
              onClick={() => onSelectItem(item.item_id)}
              aria-pressed={selectedItemId === item.item_id}
            >
              {item.kind === "gap" ? (
                <span className="editor-thumb editor-thumb-fallback" aria-hidden>
                  <small>Gap</small>
                </span>
              ) : (
                <ItemThumb
                  key={`${item.item_id}:${item.asset?.file_path ?? ""}`}
                  projectId={projectId}
                  filePath={item.asset?.file_path ?? null}
                  mediaType={item.asset?.type ?? "video"}
                  label={itemTitle(item)}
                />
              )}
              <span className="editor-asset-copy">
                <span className="editor-asset-title">{itemTitle(item)}</span>
                <span className="editor-asset-meta">
                  Sentence {item.sentence_number} · {formatDuration(item.duration)}
                </span>
                <ClassificationChip classification={item.classification} />
              </span>
            </button>
          </li>
        ))}
      </ul>

      <h3>Project asset library</h3>
      <p className="editor-panel-note">
        Assets the production recorded in its decision ledger, with the evidence and rights
        recorded for each. Adding one to a different sentence keeps its own verdict and flags
        the placement as not verified for that sentence.
      </p>
      {assetsBusy && <LoadingState label="Loading the asset library…" />}
      {!assetsBusy && assetsError != null && (
        <ErrorState error={assetsError} onRetry={() => onLoadAssets(assetsOffset)} />
      )}
      {!assetsBusy && !assetsError && assets && assets.total === 0 && (
        <p className="editor-panel-note">No ledger assets are recorded for this project yet.</p>
      )}
      {!assetsBusy && !assetsError && assets && assets.total > 0 && (
        <>
          <ul className="editor-asset-list">
            {assets.items.map((row) => (
              <li key={row.id} className="editor-library-row">
                <span className="editor-asset-copy">
                  <span className="editor-asset-title">
                    {row.decision_type.replaceAll("_", " ")} · {row.provider || "unknown provider"}
                  </span>
                  <span className="editor-asset-meta">
                    Sentence {row.sentence_number ?? "—"} · evidence {row.evidence_class || "—"} ·{" "}
                    {verificationLabel(row.verification ?? null)} · rights {ledgerRightsLabel(row.rights)}
                  </span>
                </span>
                <button
                  type="button"
                  className="editor-small-button"
                  onClick={() =>
                    onChooseSpec({
                      spec: { library_decision_id: row.id },
                      label: `Ledger asset (${row.provider || "unknown provider"}, sentence ${row.sentence_number ?? "—"})`,
                    })
                  }
                >
                  Use
                </button>
              </li>
            ))}
          </ul>
          <p className="editor-pager">
            <span>
              {assetsOffset + 1}–{assetsOffset + assets.items.length} of {assets.total}
            </span>
            <button
              type="button"
              disabled={assetsOffset === 0}
              onClick={() => onLoadAssets(Math.max(0, assetsOffset - LIBRARY_PAGE_SIZE))}
            >
              Previous
            </button>
            <button
              type="button"
              disabled={assetsOffset + assets.items.length >= assets.total}
              onClick={() => onLoadAssets(assetsOffset + LIBRARY_PAGE_SIZE)}
            >
              Next
            </button>
          </p>
        </>
      )}

      <h3>Upload a file</h3>
      <p className="editor-panel-note">
        Uploads are probed by the backend and recorded as user-supplied with licence{" "}
        <strong>unknown</strong> — never invented. Video, image and audio files are accepted.
      </p>
      <label className="editor-upload-pick">
        <Upload size={14} aria-hidden /> Choose a file
        <input
          type="file"
          accept="video/*,image/*,audio/*"
          onChange={(event) => {
            void onPickFile(event.target.files?.[0]);
            event.target.value = "";
          }}
        />
      </label>
      {uploadState.status === "uploading" && (
        <p className="editor-panel-note" role="status">
          Uploading {uploadState.fileName} — {Math.round(uploadState.progress * 100)}%
        </p>
      )}
      {uploadState.status === "error" && (
        <p className="editor-error-text" role="alert">{uploadState.message}</p>
      )}
      {sessionUploads.length > 0 && (
        <ul className="editor-asset-list">
          {sessionUploads.map((upload) => (
            <li key={upload.upload_id} className="editor-library-row">
              <span className="editor-asset-copy">
                <span className="editor-asset-title">{upload.file_name}</span>
                <span className="editor-asset-meta">
                  {upload.type} · {formatDuration(upload.source_duration)} · user supplied · licence unknown
                </span>
              </span>
              <button
                type="button"
                className="editor-small-button"
                onClick={() =>
                  onChooseSpec({
                    spec: { upload_id: upload.upload_id },
                    label: `Upload “${upload.file_name}” (${upload.type}, licence unknown)`,
                  })
                }
              >
                Use
              </button>
            </li>
          ))}
        </ul>
      )}

      <h3>From a URL</h3>
      <p className="editor-panel-note">
        The backend downloads and probes the file itself; its rights are recorded as unknown.
      </p>
      <div className="editor-url-row">
        <input
          type="url"
          value={url}
          placeholder="https://…/clip.mp4"
          aria-label="Media URL"
          onChange={(event) => setUrl(event.target.value)}
        />
        <button
          type="button"
          className="editor-small-button"
          disabled={!url.trim()}
          onClick={() => {
            onChooseSpec({ spec: { source_url: url.trim() }, label: `URL ${url.trim()}` });
            setUrl("");
          }}
        >
          <Link2 size={13} aria-hidden /> Use URL
        </button>
      </div>

      <div className="editor-pending" aria-live="polite">
        {pendingSpec ? (
          <>
            <p>
              <strong>Selected source:</strong> {pendingSpec.label}
            </p>
            <p className="editor-panel-note">
              Use it from the properties panel: Replace the selected clip, or Add it into the
              gap time of a sentence.
            </p>
            <button type="button" className="editor-small-button" onClick={() => onChooseSpec(null)}>
              Clear selection
            </button>
          </>
        ) : (
          <p className="editor-panel-note">
            No source selected. Choose a ledger asset, an upload, or a URL to add or replace
            footage.
          </p>
        )}
      </div>
    </section>
  );
}
