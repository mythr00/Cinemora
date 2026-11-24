"use client";

/** M6 — storage & media (read-only inspection in v1). */

import { useCallback, useEffect, useState } from "react";
import { formatBytes, formatDateTime } from "@/components/admin-format";
import { ErrorState, LoadingState } from "@/components/states";
import {
  getAdminOrphans,
  getAdminStorage,
  type AdminOrphans,
  type AdminStorage,
} from "@/lib/api";

export default function AdminStoragePage() {
  const [storage, setStorage] = useState<AdminStorage | null>(null);
  const [orphans, setOrphans] = useState<AdminOrphans | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const [st, or] = await Promise.all([
        getAdminStorage(signal),
        getAdminOrphans(signal),
      ]);
      setStorage(st);
      setOrphans(or);
    } catch (cause) {
      if (!signal?.aborted) setError(cause);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => { void load(controller.signal); }, 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [load]);

  if (error) return <ErrorState error={error} onRetry={() => void load()} />;
  if (!storage || !orphans) {
    return <LoadingState label="Walking the media tree…" />;
  }

  return (
    <section>
      <div className="admin-heading">
        <p className="eyebrow">STORAGE & MEDIA</p>
        <h1>{formatBytes(storage.total_bytes)} across {storage.total_files} files.</h1>
        <p className="admin-intro">
          Measured by walking {storage.media_root}. Inspection only
          in this version — nothing here deletes or moves media.
          Failed runs keep their artefacts: they are what makes
          stage-resume adoption and post-mortems possible.
        </p>
      </div>
      <h2 className="admin-section-title">Per project</h2>
      {storage.projects.length === 0 ? (
        <p className="admin-empty">No media directories found.</p>
      ) : (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Project</th>
              <th>Status</th>
              <th>Size</th>
              <th>Files</th>
              <th>Runs on disk</th>
            </tr>
          </thead>
          <tbody>
            {storage.projects.map((project) => (
              <tr key={project.project_id}>
                <td>
                  {project.title ?? project.project_id.slice(0, 8) + "…"}
                  {!project.in_database && " (not in database)"}
                </td>
                <td>{project.status ?? "—"}</td>
                <td>{formatBytes(project.bytes)}</td>
                <td>{project.files}</td>
                <td>{project.runs.length}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h2 className="admin-section-title">Largest artefacts</h2>
      <table className="admin-table">
        <thead>
          <tr><th>Path</th><th>Size</th></tr>
        </thead>
        <tbody>
          {storage.largest_artefacts.map((artefact) => (
            <tr key={artefact.path}>
              <td className="admin-cell-path">{artefact.path}</td>
              <td>{formatBytes(artefact.bytes)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h2 className="admin-section-title">Orphan report</h2>
      <p className="admin-note">
        Every entry is verifiable by hand from the path it names.
        Cleanup is a later, confirmation-gated operation; this
        report only observes.
      </p>
      <h3 className="admin-subtitle">Media directories with no database project</h3>
      {orphans.media_directories_without_project.length === 0 ? (
        <p className="admin-empty">None.</p>
      ) : (
        <ul className="admin-list">
          {orphans.media_directories_without_project.map((entry) => (
            <li key={entry.project_id}>
              {entry.path} — {formatBytes(entry.bytes)}
            </li>
          ))}
        </ul>
      )}
      <h3 className="admin-subtitle">Projects with no media directory</h3>
      {orphans.projects_without_media_directory.length === 0 ? (
        <p className="admin-empty">None.</p>
      ) : (
        <ul className="admin-list">
          {orphans.projects_without_media_directory.map((projectId) => (
            <li key={projectId}>{projectId}</li>
          ))}
        </ul>
      )}
      <h3 className="admin-subtitle">
        Run directories of runs terminal for over{" "}
        {orphans.old_terminal_threshold_days} days
      </h3>
      {orphans.old_terminal_run_directories.length === 0 ? (
        <p className="admin-empty">None.</p>
      ) : (
        <ul className="admin-list">
          {orphans.old_terminal_run_directories.map((entry) => (
            <li key={entry.run_id}>
              {entry.path} — {formatBytes(entry.bytes)} · finished{" "}
              {formatDateTime(entry.finished_at)}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
