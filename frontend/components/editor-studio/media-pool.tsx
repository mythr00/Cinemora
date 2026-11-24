"use client";

import { useRef, useState } from "react";
import { Upload } from "lucide-react";
import { uploadAsset, type StudioPool, type StudioPoolEntry } from "@/lib/api";
import { ItemThumb } from "@/components/editor/item-thumb";
import { CLASSIFICATION_LABELS, formatDuration } from "@/components/editor/shared";

/**
 * The Studio Media Pool: the project's real recorded assets —
 * the source run's ledger decisions (verified assets and the
 * alternates the pipeline recorded), revision-chain decisions and
 * the project's uploads (services/editor_studio.media_pool).
 * Entries whose file is gone are shown as unavailable and cannot
 * be selected: the pool never invents media.
 */
export function MediaPool({
  projectId,
  pool,
  selectedPoolId,
  onSelect,
  onLoadMore,
  onUploaded,
  busy,
}: {
  projectId: string;
  pool: StudioPool | null;
  selectedPoolId: string | null;
  onSelect: (entry: StudioPoolEntry | null) => void;
  onLoadMore: () => void;
  onUploaded: () => void;
  busy: boolean;
}) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  async function onFileChosen(file: File | undefined) {
    if (!file) return;
    setUploading(true);
    setUploadError(null);
    try {
      await uploadAsset(projectId, file);
      onUploaded();
    } catch (error) {
      setUploadError(error instanceof Error ? error.message : "Upload failed.");
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  return (
    <section className="studio-pane" aria-label="Media pool">
      <h2>Media Pool</h2>
      <div className="studio-field-row" style={{ marginBottom: 10 }}>
        <button
          type="button"
          className="studio-btn"
          disabled={uploading}
          onClick={() => fileRef.current?.click()}
        >
          <Upload size={14} aria-hidden /> {uploading ? "Uploading…" : "Upload media"}
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="video/*,image/*,audio/*"
          style={{ display: "none" }}
          onChange={(event) => void onFileChosen(event.target.files?.[0])}
        />
      </div>
      {uploadError && <p className="studio-error">{uploadError}</p>}
      {!pool && <p className="studio-note">Loading the project&apos;s recorded assets…</p>}
      {pool && pool.items.length === 0 && (
        <p className="studio-note">
          No assets are recorded for this project&apos;s production yet.
        </p>
      )}
      <div className="studio-pool-list">
        {pool?.items.map((entry) => (
          <button
            key={entry.pool_id}
            type="button"
            className={`studio-pool-card${selectedPoolId === entry.pool_id ? " is-selected" : ""}`}
            disabled={!entry.available || busy}
            onClick={() => onSelect(selectedPoolId === entry.pool_id ? null : entry)}
            title={entry.available ? entry.file_name : `${entry.file_name} — file not on disk`}
          >
            <ItemThumb
              projectId={projectId}
              filePath={entry.file_path}
              mediaType={entry.type}
              label={entry.file_name}
            />
            <span className="studio-pool-meta">
              <span className="studio-pool-name">{entry.file_name}</span>
              <span className="studio-pool-sub">
                {CLASSIFICATION_LABELS[entry.classification] || entry.classification}
                {entry.source_duration ? ` · ${formatDuration(entry.source_duration)}` : ""}
                {entry.sentence_number ? ` · sentence ${entry.sentence_number}` : ""}
              </span>
              <span className="studio-pool-sub">
                {entry.origin === "upload" ? "Your upload" : entry.provider || "Recorded asset"}
                {entry.in_timeline ? " · on timeline" : ""}
                {!entry.available ? " · file missing" : ""}
              </span>
            </span>
          </button>
        ))}
      </div>
      {pool && pool.items.length < pool.total && (
        <button
          type="button"
          className="studio-btn"
          style={{ marginTop: 10 }}
          onClick={onLoadMore}
          disabled={busy}
        >
          Load more ({pool.items.length} of {pool.total})
        </button>
      )}
    </section>
  );
}
