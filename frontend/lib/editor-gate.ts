/**
 * FS-F (FS-8): the Studio gate, shared by the classic Editor
 * page, the Editor Studio page and the project tab bar.
 *
 * Ordinary users may enter either editing surface ONLY after
 * generation successfully completes and the finished video
 * exists. The predicate mirrors the backend gate in
 * backend/api_v1.py (_require_editor_access, the authority —
 * this module is presentation only):
 *
 * - a completed run: any run from GET /projects/{id}/runs with
 *   status "completed" (NOT the headline project status — a
 *   project whose latest run failed but which has an earlier
 *   completed run with its film stays editable, A-10);
 * - the finished video exists: GET /projects/{id}/outputs
 *   reports film.exists (the backend's media-index source of
 *   truth for a finished film).
 *
 * Admin and super_admin principals are exempt (support access),
 * exactly as on the backend; the operator principal resolves to
 * super_admin there.
 */

import {
  getCurrentUser,
  getOutputs,
  getProjectStatus,
  listRuns,
  type ProjectStatus,
} from "@/lib/api";

export type EditorGate =
  | { kind: "admin" }
  | { kind: "ready"; status: ProjectStatus | null }
  | {
      kind: "not-ready";
      status: ProjectStatus | null;
      /** The latest run (or the project) failed with no
       * completed production behind it — the FS-A simple
       * failure framing applies. */
      failed: boolean;
    };

export async function checkEditorGate(
  projectId: string,
  signal?: AbortSignal,
): Promise<EditorGate> {
  const user = await getCurrentUser(signal).catch(() => null);
  if (user && (user.role === "admin" || user.role === "super_admin")) {
    return { kind: "admin" };
  }
  const [status, outputs, runs] = await Promise.all([
    getProjectStatus(projectId, signal).catch(() => null),
    getOutputs(projectId, signal).catch(() => null),
    listRuns(projectId, signal).catch(() => null),
  ]);
  const hasCompletedRun = (runs?.runs ?? []).some(
    (run) => run.status === "completed",
  );
  const filmExists = outputs?.film.exists === true;
  if (hasCompletedRun && filmExists) {
    return { kind: "ready", status };
  }
  const latestRun = runs?.runs[0] ?? status?.latest_run ?? null;
  const failed =
    !hasCompletedRun &&
    (status?.status === "failed" || latestRun?.status === "failed");
  return { kind: "not-ready", status, failed };
}

/** The honest not-ready copy both editor pages render instead
 * of the editing UI when the gate does not pass. */
export function editorGateMessage(gate: { failed: boolean }): {
  heading: string;
  body: string;
} {
  if (gate.failed) {
    return {
      heading: "Your video isn't ready yet",
      body: "Video generation failed, so there is no finished film to edit. Editing opens when a generation completes successfully — check the project overview for the current status and next steps.",
    };
  }
  return {
    heading: "Your video isn't ready yet",
    body: "Editing opens when generation completes successfully and the finished video exists. Until then there is no finished film to revise — check the project overview for progress.",
  };
}
