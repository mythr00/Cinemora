"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { StatusBadge } from "@/components/status-badge";
import { ProjectTabs } from "@/components/project-tabs";
import { ErrorState, LoadingState } from "@/components/states";
import {
  ArrowLeft,
  Camera,
  Eye,
  RotateCcw,
  Tag,
  Trash2,
  X,
} from "lucide-react";
import {
  ApiClientError,
  compareVersions,
  createSnapshot,
  deleteSnapshot,
  getScriptVersion,
  getSnapshot,
  getVersions,
  restoreScriptVersion,
  restoreSnapshot,
  setVersionLabel,
  type RestoreOutcome,
  type ScriptVersionDetail,
  type SnapshotDetail,
  type VersionCompare,
  type VersionDiff,
  type VersionEntry,
  type VersionPlanDiff,
  type VersionsView,
} from "@/lib/api";

/**
 * Versions (Phase G, founder priority #9) — the project's unified
 * version story, assembled by the backend from the histories the
 * system itself recorded: production runs (with their recorded
 * options and QA verdicts), editor revisions (with the operations
 * that created each one), script versions, plan decisions and
 * snapshots. A script version's text is shown only where it was
 * recorded — snapshotted at creation/submission, or the project's
 * current script field; a version known only by its run-record
 * hash is labelled "text not recorded", cannot be restored, and
 * never gets a reconstructed diff. Snapshot restore sets the
 * script, writes the recorded plan back and branches the pointed
 * revision — the original chain is never mutated.
 */

const KIND_LABELS: Record<string, string> = {
  run: "Production run",
  revision: "Editor revision",
  script: "Script",
  plan: "Plan",
  plan_decision: "Plan decision",
  snapshot: "Snapshot",
};

function when(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : "time not recorded";
}

function shortHash(hash?: string | null): string {
  return hash ? `${hash.slice(0, 12)}…` : "—";
}

function ChangeSummary({ entry }: { entry: VersionEntry }) {
  const changes = entry.changes;
  if (!changes) return null;
  if (entry.kind === "run") {
    const parts: string[] = [];
    if (changes.script_changed === true) parts.push("script changed");
    if (changes.script_changed === false) parts.push("same script as the previous run");
    if (changes.script_changed == null) parts.push("script change not recorded");
    if (Array.isArray(changes.options_changed)) {
      parts.push(
        changes.options_changed.length > 0
          ? `options changed: ${changes.options_changed.join(", ")}`
          : "options unchanged",
      );
    } else {
      parts.push("no options recorded on either run");
    }
    return <p className="plan-meta">Changes vs the previous run: {parts.join(" · ")}.</p>;
  }
  if (entry.kind === "revision") {
    const ops = changes.operations_applied ?? [];
    if (ops.length === 0) return null;
    return (
      <>
        <p className="plan-meta">
          {changes.vs_entry_id
            ? "Operations that created this revision:"
            : "How this revision began:"}
        </p>
        <ul>
          {ops.map((op) => (
            <li key={op.op_id || op.summary}>
              <strong>{op.type}</strong>
              {op.summary ? ` — ${op.summary}` : ""}
            </li>
          ))}
        </ul>
      </>
    );
  }
  if (entry.kind === "script") {
    const diff = changes.text_diff;
    if (!diff) return null;
    if (diff.state === "recorded") {
      return (
        <p className="plan-meta">
          Text diff vs the previous script version: +{diff.added_lines} / −
          {diff.removed_lines} lines.
        </p>
      );
    }
    return (
      <p className="plan-meta">
        No text diff vs the previous script version: the script text was not
        recorded for one or both versions — only the hashes survive, and a
        diff is never reconstructed.
      </p>
    );
  }
  return null;
}

function EntryDetail({ entry }: { entry: VersionEntry }) {
  const detail = entry.detail || {};
  if (entry.kind === "run") {
    const verdicts = detail.verdicts;
    return (
      <p className="plan-meta">
        Status {detail.status || "—"}
        {detail.stage ? ` · stage ${detail.stage}` : ""} · script{" "}
        {shortHash(detail.script_hash)}
        {verdicts?.state === "recorded"
          ? ` · verdicts — integrity ${verdicts.integrity || "—"}, quality ${verdicts.quality || "—"}, reliability ${verdicts.reliability || "—"}`
          : " · no production report was recorded for this run, so no verdicts are shown"}
        {detail.error ? ` · error recorded: ${detail.error}` : ""}
      </p>
    );
  }
  if (entry.kind === "revision") {
    return (
      <p className="plan-meta">
        Revision {detail.revision_number} · status {detail.status || "—"} ·
        document hash {shortHash(detail.document_hash)} ·{" "}
        {detail.visual_items ?? "—"} visual items ·{" "}
        {detail.export_count ?? 0} exports
        {detail.parent_revision_id
          ? ` · branched from revision ${String(detail.parent_revision_id).slice(0, 8)}…`
          : " · chain root (imported from its production run)"}
      </p>
    );
  }
  if (entry.kind === "script") {
    return (
      <p className="plan-meta">
        Hash {shortHash(detail.script_hash)} · used by {detail.run_count ?? 0}{" "}
        run(s) · text{" "}
        {detail.text_state === "recorded"
          ? `recorded (${detail.char_count ?? "—"} characters, source: ${String(detail.text_source || "recorded").replaceAll("_", " ")})`
          : "not recorded — only the hash survives in the run records"}
        {detail.is_current ? " · this is the project's current script" : ""}
      </p>
    );
  }
  if (entry.kind === "plan_decision") {
    const decision = detail.decision || {};
    return (
      <p className="plan-meta">
        Sentence {decision.sentence_number} · action {decision.action}
        {decision.outcome ? ` · outcome ${String(decision.outcome).replaceAll("_", " ")}` : ""}
      </p>
    );
  }
  if (entry.kind === "plan") {
    return (
      <p className="plan-meta">
        {detail.sentence_count ?? "—"} sentences · {detail.scene_count ?? "—"}{" "}
        scenes · source {String(detail.plan_source || "not recorded").replaceAll("_", " ")}
        {detail.bible_title ? ` · ${detail.bible_title}` : ""}
      </p>
    );
  }
  if (entry.kind === "snapshot") {
    return (
      <p className="plan-meta">
        Script {shortHash(detail.script?.script_hash)} · plan{" "}
        {detail.plan?.state || "not recorded"} · revision pointer{" "}
        {detail.revision?.state === "recorded"
          ? `revision ${detail.revision.revision_number}`
          : "not recorded"}
        {detail.note ? ` · ${detail.note}` : ""}
      </p>
    );
  }
  return null;
}

function PlanDiff({ diff }: { diff: VersionPlanDiff | VersionDiff }) {
  if (!diff) return null;
  if (diff.state !== "recorded") {
    return (
      <p className="plan-meta">
        Plan documents: {diff.reason || "not recorded for one or both sides."}
      </p>
    );
  }
  return (
    <>
      <p className="plan-meta">
        Plan documents: {diff.sentence_count?.a ?? "—"} sentences (A) vs{" "}
        {diff.sentence_count?.b ?? "—"} (B) · {diff.scene_count?.a ?? "—"}{" "}
        scenes (A) vs {diff.scene_count?.b ?? "—"} (B).
      </p>
      {Array.isArray(diff.sentence_text_changes) &&
      diff.sentence_text_changes.length > 0 ? (
        <div className="plan-table-wrap">
          <table className="plan-table">
            <thead>
              <tr>
                <th scope="col">Sentence</th>
                <th scope="col">Text in A</th>
                <th scope="col">Text in B</th>
              </tr>
            </thead>
            <tbody>
              {diff.sentence_text_changes.map((change) => (
                <tr key={change.sentence_number}>
                  <td>{change.sentence_number}</td>
                  <td>{change.a_text}</td>
                  <td>{change.b_text}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="plan-meta">
          {diff.sentence_text_changes
            ? "No sentence text differs between the two recorded plan documents."
            : "Sentence texts are not recorded in one or both plan documents, so no text comparison is shown."}
        </p>
      )}
    </>
  );
}

function ComparePanel({ result }: { result: VersionCompare }) {
  const diff = result.diff || {};
  return (
    <div className="version-compare">
      <h3>
        {result.a.entry.title} <span className="plan-meta">vs</span>{" "}
        {result.b.entry.title}
      </h3>
      {diff.kind === "script_text" &&
        (diff.state === "recorded" ? (
          <>
            <p className="plan-meta">
              +{diff.added_lines} / −{diff.removed_lines} lines
              {diff.diff_truncated
                ? ` · the diff is longer than the display limit and was truncated at ${diff.diff_line_count} recorded lines`
                : ""}
              .
            </p>
            <pre className="version-diff">{diff.diff}</pre>
          </>
        ) : (
          <p className="plan-meta">{diff.reason}</p>
        ))}
      {diff.kind === "revision_ops" && (
        <>
          <p className="plan-meta">
            Common ancestor:{" "}
            {diff.common_ancestor_revision_id
              ? `revision ${String(diff.common_ancestor_revision_id).slice(0, 8)}…`
              : "none recorded"}{" "}
            · document hashes {diff.document_hash?.equal ? "identical" : "differ"}.
          </p>
          {(["ops_only_in_a", "ops_only_in_b"] as const).map((key) => (
            <div key={key}>
              <p className="plan-meta">
                Operations only in {key === "ops_only_in_a" ? "A" : "B"}:
              </p>
              {(diff[key] ?? []).length > 0 ? (
                <ul>
                  {(diff[key] ?? []).map((op) => (
                    <li key={op.op_id || op.summary}>
                      <strong>{op.type}</strong>
                      {op.summary ? ` — ${op.summary}` : ""}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="plan-meta">None.</p>
              )}
            </div>
          ))}
        </>
      )}
      {diff.kind === "run_fields" && (
        <>
          <div className="plan-table-wrap">
            <table className="plan-table">
              <thead>
                <tr>
                  <th scope="col">Recorded field</th>
                  <th scope="col">A</th>
                  <th scope="col">B</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(diff.fields ?? {}).map(([field, values]) => (
                  <tr key={field}>
                    <td>{field.replaceAll("_", " ")}</td>
                    <td>{values.a == null ? "—" : String(values.a)}</td>
                    <td>{values.b == null ? "—" : String(values.b)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="plan-meta">
            {Array.isArray(diff.options_changed)
              ? diff.options_changed.length > 0
                ? `Options changed: ${diff.options_changed.join(", ")}.`
                : "Recorded options are identical."
              : "Neither run recorded options, so no option comparison exists."}{" "}
            Quality verdicts — A: {diff.verdicts?.a?.quality || "not recorded"},
            B: {diff.verdicts?.b?.quality || "not recorded"}.
          </p>
        </>
      )}
      {diff.kind === "snapshot_components" && (
        <>
          <p className="plan-meta">
            Scripts: hashes {diff.script?.hash_equal ? "identical" : "differ"}
            {diff.script?.text_diff?.state === "recorded"
              ? ` · text diff +${diff.script.text_diff.added_lines} / −${diff.script.text_diff.removed_lines} lines`
              : " · script text not recorded for one or both snapshots, so no text diff is shown"}
            . Revision pointers — A:{" "}
            {diff.revision?.a?.revision_id
              ? `revision ${diff.revision.a.revision_number}`
              : "not recorded"}
            , B:{" "}
            {diff.revision?.b?.revision_id
              ? `revision ${diff.revision.b.revision_number}`
              : "not recorded"}
            .
          </p>
          {diff.plan && <PlanDiff diff={diff.plan} />}
        </>
      )}
      {diff.kind === "plan_document" && <PlanDiff diff={diff} />}
      {diff.kind === "side_by_side" && (
        <p className="plan-meta">
          {diff.reason}
          {diff.script_hash
            ? ` Both entries record a script hash: A ${shortHash(diff.script_hash.a)}, B ${shortHash(diff.script_hash.b)} — ${diff.script_hash.equal ? "the same script." : "different scripts."}`
            : ""}
        </p>
      )}
    </div>
  );
}


export default function VersionsPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [view, setView] = useState<VersionsView | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [selected, setSelected] = useState<string[]>([]);
  const [comparison, setComparison] = useState<VersionCompare | null>(null);
  const [comparing, setComparing] = useState(false);

  const [labelEdit, setLabelEdit] = useState<string | null>(null);
  const [labelName, setLabelName] = useState("");
  const [labelNote, setLabelNote] = useState("");

  const [scriptDetail, setScriptDetail] = useState<ScriptVersionDetail | null>(null);
  const [confirmScriptRestore, setConfirmScriptRestore] = useState<string | null>(null);

  const [snapName, setSnapName] = useState("");
  const [snapNote, setSnapNote] = useState("");
  const [snapshotDetail, setSnapshotDetail] = useState<SnapshotDetail | null>(null);
  const [confirmSnapshotAction, setConfirmSnapshotAction] = useState<string | null>(null);
  const [restoreOutcome, setRestoreOutcome] = useState<RestoreOutcome | null>(null);
  const [acting, setActing] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      setView(await getVersions(projectId));
    } catch (cause) {
      setView(null);
      setError(cause);
    } finally {
      setBusy(false);
    }
  }, [projectId]);

  useEffect(() => {
    const timer = setTimeout(() => { void load(); }, 0);
    return () => clearTimeout(timer);
  }, [load]);

  const runAction = useCallback(
    async (action: () => Promise<unknown>, after?: () => void) => {
      setActing(true);
      setActionError(null);
      try {
        await action();
        after?.();
        await load();
      } catch (cause) {
        setActionError(
          cause instanceof ApiClientError
            ? cause.message
            : "The action failed. Nothing was changed by this attempt.",
        );
      } finally {
        setActing(false);
      }
    },
    [load],
  );

  const toggleSelected = (entryId: string) => {
    setComparison(null);
    setSelected((current) => {
      if (current.includes(entryId)) return current.filter((id) => id !== entryId);
      return [...current.slice(-1), entryId];
    });
  };

  const doCompare = async () => {
    if (selected.length !== 2) return;
    setComparing(true);
    setActionError(null);
    try {
      setComparison(await compareVersions(projectId, selected[0], selected[1]));
    } catch (cause) {
      setActionError(
        cause instanceof ApiClientError ? cause.message : "Comparison failed.",
      );
    } finally {
      setComparing(false);
    }
  };

  const entries = view ? [...view.entries].reverse() : [];

  return (
    <main className="app-page versions-page">
      <AppNavigation />
      <ProjectTabs projectId={projectId} />
      <section className="plan-content">
        <Link href={`/projects/${projectId}`} className="editor-back">
          <ArrowLeft size={14} strokeWidth={2} aria-hidden /> Project
        </Link>
        <p className="eyebrow">VERSIONING &amp; RECOVERY</p>
        <h1>Versions</h1>
        <p className="plan-intro">
          Every recorded version of this film in one story — nothing here is reconstructed.
        </p>

        {busy && <LoadingState className="plan-message" label="Loading the version history…" />}
        {!busy && error != null && (
          <ErrorState error={error} onRetry={() => { setBusy(true); void load(); }} />
        )}
        {actionError && <p className="plan-meta version-action-error">{actionError}</p>}

        {view && (
          <>
            <section aria-label="Snapshots">
              <h2>Snapshots</h2>
              <p className="plan-meta">
                A snapshot names a moment: the current script (its text is
                recorded), the plan document exactly as written, and the
                editor&apos;s current revision. Restoring one sets the script
                back, writes the plan back and branches the recorded revision
                into a new working revision — the original chain is never
                mutated, and the branch renders like any other revision.
              </p>
              <div className="versions-controls">
                <label>
                  Name{" "}
                  <input
                    value={snapName}
                    onChange={(event) => setSnapName(event.target.value)}
                    placeholder="Before the rework"
                    maxLength={200}
                  />
                </label>
                <label>
                  Note{" "}
                  <input
                    value={snapNote}
                    onChange={(event) => setSnapNote(event.target.value)}
                    placeholder="Optional"
                    maxLength={2000}
                  />
                </label>
                <button
                  type="button"
                  disabled={acting || !snapName.trim()}
                  onClick={() =>
                    void runAction(
                      () => createSnapshot(projectId, snapName.trim(), snapNote.trim() || null),
                      () => { setSnapName(""); setSnapNote(""); },
                    )
                  }
                >
                  <Camera size={14} strokeWidth={2} aria-hidden /> Take snapshot
                </button>
              </div>
              {view.snapshots.length === 0 ? (
                <p className="plan-meta">No snapshots yet.</p>
              ) : (
                <ul className="version-list">
                  {[...view.snapshots].reverse().map((snapshot) => {
                    const confirmKey = `snapshot:${snapshot.id}`;
                    return (
                      <li key={snapshot.id} className="version-entry">
                        <div className="version-entry-head">
                          <span className="version-kind">{KIND_LABELS.snapshot}</span>
                          <strong>{snapshot.name}</strong>
                          <span className="plan-meta">{when(snapshot.created_at)}</span>
                        </div>
                        <p className="plan-meta">
                          Script {shortHash(snapshot.script.script_hash)} · plan{" "}
                          {snapshot.plan.state || "not recorded"} · revision pointer{" "}
                          {snapshot.revision.state === "recorded"
                            ? `revision ${snapshot.revision.revision_number}`
                            : "not recorded"}
                          {snapshot.note ? ` · ${snapshot.note}` : ""}
                        </p>
                        <div className="versions-controls">
                          <button
                            type="button"
                            disabled={acting}
                            onClick={() => {
                              setActionError(null);
                              getSnapshot(projectId, snapshot.id)
                                .then(setSnapshotDetail)
                                .catch((cause) =>
                                  setActionError(
                                    cause instanceof ApiClientError
                                      ? cause.message
                                      : "The snapshot could not be loaded.",
                                  ),
                                );
                            }}
                          >
                            <Eye size={14} strokeWidth={2} aria-hidden /> View plan
                          </button>
                          {confirmSnapshotAction === `restore:${confirmKey}` ? (
                            <>
                              <button
                                type="button"
                                disabled={acting}
                                onClick={() =>
                                  void runAction(
                                    async () => {
                                      const outcome = await restoreSnapshot(projectId, snapshot.id);
                                      setRestoreOutcome(outcome);
                                    },
                                    () => setConfirmSnapshotAction(null),
                                  )
                                }
                              >
                                <RotateCcw size={14} strokeWidth={2} aria-hidden /> Confirm restore
                              </button>
                              <button type="button" onClick={() => setConfirmSnapshotAction(null)}>
                                <X size={14} strokeWidth={2} aria-hidden /> Cancel
                              </button>
                            </>
                          ) : (
                            <button
                              type="button"
                              disabled={acting}
                              onClick={() => setConfirmSnapshotAction(`restore:${confirmKey}`)}
                            >
                              <RotateCcw size={14} strokeWidth={2} aria-hidden /> Restore
                            </button>
                          )}
                          {confirmSnapshotAction === `delete:${confirmKey}` ? (
                            <>
                              <button
                                type="button"
                                disabled={acting}
                                onClick={() =>
                                  void runAction(
                                    () => deleteSnapshot(projectId, snapshot.id),
                                    () => setConfirmSnapshotAction(null),
                                  )
                                }
                              >
                                <Trash2 size={14} strokeWidth={2} aria-hidden /> Confirm delete
                              </button>
                              <button type="button" onClick={() => setConfirmSnapshotAction(null)}>
                                <X size={14} strokeWidth={2} aria-hidden /> Cancel
                              </button>
                            </>
                          ) : (
                            <button
                              type="button"
                              disabled={acting}
                              onClick={() => setConfirmSnapshotAction(`delete:${confirmKey}`)}
                            >
                              <Trash2 size={14} strokeWidth={2} aria-hidden /> Delete
                            </button>
                          )}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
              {snapshotDetail && (
                <div className="version-compare">
                  <h3>Snapshot — {snapshotDetail.name}</h3>
                  {snapshotDetail.plan_document ? (
                    <>
                      <p className="plan-meta">
                        Recorded plan document:{" "}
                        {snapshotDetail.plan_document.sentences?.length ?? "—"}{" "}
                        sentences ·{" "}
                        {snapshotDetail.plan_document.scenes?.length ?? "—"}{" "}
                        scenes
                        {snapshotDetail.plan_document.bible?.title
                          ? ` · ${snapshotDetail.plan_document.bible.title}`
                          : ""}
                        {snapshotDetail.plan_document.generated_at
                          ? ` · generated ${when(snapshotDetail.plan_document.generated_at)}`
                          : ""}
                        .
                      </p>
                      <details>
                        <summary>Recorded plan document (JSON)</summary>
                        <pre className="version-diff">
                          {JSON.stringify(snapshotDetail.plan_document, null, 2)}
                        </pre>
                      </details>
                    </>
                  ) : (
                    <p className="plan-meta">
                      This snapshot holds no plan document — none was recorded
                      when it was taken.
                    </p>
                  )}
                </div>
              )}
              {restoreOutcome && (
                <div className="version-compare">
                  <h3>Restored — {restoreOutcome.snapshot_name}</h3>
                  <p className="plan-meta">
                    Script: {restoreOutcome.script.state}
                    {restoreOutcome.script.script_hash
                      ? ` (${shortHash(restoreOutcome.script.script_hash)})`
                      : ""}
                    {restoreOutcome.script.reason
                      ? ` — ${restoreOutcome.script.reason}`
                      : ""}{" "}
                    · Plan document: {restoreOutcome.plan.state} · Timeline:{" "}
                    {restoreOutcome.revision.state === "branched"
                      ? `branched revision ${restoreOutcome.revision.new_revision_number} from the snapshot's revision — render it from the Editor or Editor Studio`
                      : restoreOutcome.revision.state}
                    .
                  </p>
                  <ul>
                    {restoreOutcome.not_restored.map((note) => (
                      <li key={note}>{note}</li>
                    ))}
                  </ul>
                  {restoreOutcome.revision.new_revision_id && (
                    <p className="plan-meta">
                      <Link href={`/projects/${projectId}/studio`}>
                        Open the Editor Studio
                      </Link>{" "}
                      ·{" "}
                      <Link href={`/projects/${projectId}/edit`}>
                        Open the Editor
                      </Link>
                    </p>
                  )}
                </div>
              )}
            </section>

            <section aria-label="Script versions">
              <h2>Script versions</h2>
              {view.script_versions.length === 0 ? (
                <p className="plan-meta">No script versions are recorded.</p>
              ) : (
                <div className="plan-table-wrap">
                  <table className="plan-table">
                    <thead>
                      <tr>
                        <th scope="col">Script hash</th>
                        <th scope="col">First submitted</th>
                        <th scope="col">Runs</th>
                        <th scope="col">Text</th>
                        <th scope="col">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {view.script_versions.map((version) => (
                        <tr key={version.script_hash}>
                          <td>
                            {shortHash(version.script_hash)}
                            {version.is_current ? " · current" : ""}
                          </td>
                          <td>{when(version.first_submitted_at || version.first_seen_at)}</td>
                          <td>{version.run_count}</td>
                          <td>
                            {version.text_state === "recorded"
                              ? `recorded (${version.char_count ?? "—"} chars)`
                              : "not recorded"}
                          </td>
                          <td>
                            <div className="versions-controls">
                              <button
                                type="button"
                                onClick={() => {
                                  setActionError(null);
                                  getScriptVersion(projectId, version.script_hash)
                                    .then(setScriptDetail)
                                    .catch((cause) =>
                                      setActionError(
                                        cause instanceof ApiClientError
                                          ? cause.message
                                          : "The script version could not be loaded.",
                                      ),
                                    );
                                }}
                              >
                                <Eye size={14} strokeWidth={2} aria-hidden /> View
                              </button>
                              {version.text_state === "recorded" &&
                                !version.is_current &&
                                (confirmScriptRestore === version.script_hash ? (
                                  <>
                                    <button
                                      type="button"
                                      disabled={acting}
                                      onClick={() =>
                                        void runAction(
                                          () => restoreScriptVersion(projectId, version.script_hash),
                                          () => setConfirmScriptRestore(null),
                                        )
                                      }
                                    >
                                      <RotateCcw size={14} strokeWidth={2} aria-hidden /> Confirm restore
                                    </button>
                                    <button type="button" onClick={() => setConfirmScriptRestore(null)}>
                                      <X size={14} strokeWidth={2} aria-hidden /> Cancel
                                    </button>
                                  </>
                                ) : (
                                  <button
                                    type="button"
                                    disabled={acting}
                                    onClick={() => setConfirmScriptRestore(version.script_hash)}
                                  >
                                    <RotateCcw size={14} strokeWidth={2} aria-hidden /> Restore script
                                  </button>
                                ))}
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {scriptDetail && (
                <div className="version-compare">
                  <h3>Script {shortHash(scriptDetail.script_hash)}</h3>
                  {scriptDetail.text != null ? (
                    <pre className="version-diff">{scriptDetail.text}</pre>
                  ) : (
                    <p className="plan-meta">
                      The text of this version was not recorded — only its
                      hash survives in the run records, so there is no text
                      to show and nothing to restore.
                    </p>
                  )}
                </div>
              )}
            </section>

            <section aria-label="Version timeline">
              <h2>Timeline</h2>
              <div className="versions-controls">
                <span>
                  {selected.length === 2
                    ? "Two entries selected."
                    : "Select two entries to compare them."}
                </span>
                <button
                  type="button"
                  disabled={selected.length !== 2 || comparing}
                  onClick={() => void doCompare()}
                >
                  Compare selected
                </button>
                {selected.length > 0 && (
                  <button
                    type="button"
                    onClick={() => { setSelected([]); setComparison(null); }}
                  >
                    Clear selection
                  </button>
                )}
              </div>
              {comparison && <ComparePanel result={comparison} />}
              {entries.length === 0 ? (
                <div className="plan-empty">
                  <h2>No version history yet</h2>
                  <p>
                    Nothing has been recorded for this project beyond its
                    creation. Runs, revisions, plan decisions and snapshots
                    will appear here as they are recorded.
                  </p>
                </div>
              ) : (
                <ul className="version-list">
                  {entries.map((entry) => (
                    <li key={entry.entry_id} className="version-entry">
                      <div className="version-entry-head">
                        <label className="version-select">
                          <input
                            type="checkbox"
                            checked={selected.includes(entry.entry_id)}
                            onChange={() => toggleSelected(entry.entry_id)}
                            aria-label={`Select ${entry.title} for comparison`}
                          />
                        </label>
                        <span className="version-kind">
                          {KIND_LABELS[entry.kind] || entry.kind}
                        </span>
                        <strong>{entry.title}</strong>
                        <span className="plan-meta">{when(entry.at)}</span>
                        {entry.kind === "run" && entry.detail.status ? (
                          <StatusBadge status={String(entry.detail.status).toLowerCase()} />
                        ) : null}
                      </div>
                      <EntryDetail entry={entry} />
                      <ChangeSummary entry={entry} />
                      {entry.label ? (
                        <p className="plan-meta">
                          Label: <strong>{entry.label.name || "—"}</strong>
                          {entry.label.note ? ` · ${entry.label.note}` : ""}
                        </p>
                      ) : null}
                      {labelEdit === entry.entry_id ? (
                        <div className="versions-controls">
                          <label>
                            Name{" "}
                            <input
                              value={labelName}
                              onChange={(event) => setLabelName(event.target.value)}
                              maxLength={200}
                            />
                          </label>
                          <label>
                            Note{" "}
                            <input
                              value={labelNote}
                              onChange={(event) => setLabelNote(event.target.value)}
                              maxLength={2000}
                            />
                          </label>
                          <button
                            type="button"
                            disabled={acting}
                            onClick={() =>
                              void runAction(
                                () =>
                                  setVersionLabel(
                                    projectId,
                                    entry.entry_id,
                                    labelName.trim() || null,
                                    labelNote.trim() || null,
                                  ),
                                () => setLabelEdit(null),
                              )
                            }
                          >
                            <Tag size={14} strokeWidth={2} aria-hidden /> Save label
                          </button>
                          <button type="button" onClick={() => setLabelEdit(null)}>
                            <X size={14} strokeWidth={2} aria-hidden /> Cancel
                          </button>
                        </div>
                      ) : (
                        <div className="versions-controls">
                          <button
                            type="button"
                            onClick={() => {
                              setLabelEdit(entry.entry_id);
                              setLabelName(entry.label?.name || "");
                              setLabelNote(entry.label?.note || "");
                            }}
                          >
                            <Tag size={14} strokeWidth={2} aria-hidden />{" "}
                            {entry.label ? "Edit label" : "Add label"}
                          </button>
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </section>

            {view.restores.length > 0 && (
              <section aria-label="Restore history">
                <h2>Restores</h2>
                <ul>
                  {[...view.restores].reverse().map((restore, index) => (
                    <li key={`${restore.at || "restore"}-${index}`}>
                      {when(restore.at)} —{" "}
                      {restore.kind === "snapshot"
                        ? `snapshot ${String(restore.snapshot_id || "").slice(0, 13)}… restored`
                        : `script version ${shortHash(restore.script_hash)} restored`}
                      {restore.new_revision_id
                        ? ` · branched revision ${String(restore.new_revision_id).slice(0, 8)}…`
                        : ""}
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </>
        )}
      </section>
    </main>
  );
}
