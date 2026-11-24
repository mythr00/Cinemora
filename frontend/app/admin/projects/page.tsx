"use client";

/** Projects — cross-tenant metadata view; content stays in the studio. */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { formatDateTime } from "@/components/admin-format";
import { ErrorState, LoadingState } from "@/components/states";
import { listAdminProjects } from "@/lib/api";

export default function AdminProjectsPage() {
  const [data, setData] = useState<Awaited<
    ReturnType<typeof listAdminProjects>
  > | null>(null);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setData(await listAdminProjects(signal));
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
  if (!data) return <LoadingState label="Loading projects…" />;

  return (
    <section>
      <div className="admin-heading">
        <p className="eyebrow">PROJECT MANAGEMENT</p>
        <h1>All projects, all owners.</h1>
        <p className="admin-intro">
          Metadata only — scripts and footage are reached through
          the studio project page, under the same access rules as
          everywhere else. There is no project deletion in the
          platform, and administration does not invent one: media
          is the sole copy of delivered films.
        </p>
      </div>
      {data.projects.length === 0 ? (
        <p className="admin-empty">No projects yet.</p>
      ) : (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Project</th>
              <th>Category</th>
              <th>Status</th>
              <th>Owner</th>
              <th>Latest run</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {data.projects.map((project) => (
              <tr key={project.id}>
                <td>
                  <Link href={project.studio_path}>
                    {project.title ?? project.id.slice(0, 8) + "…"}
                  </Link>
                </td>
                <td>{project.category}</td>
                <td>{project.status}</td>
                <td>{project.owner_email ?? "—"}</td>
                <td>
                  {project.latest_run
                    ? `${project.latest_run.status}${project.latest_run.stage ? ` · ${project.latest_run.stage}` : ""}`
                    : "—"}
                </td>
                <td>{formatDateTime(project.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
