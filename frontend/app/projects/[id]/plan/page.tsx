"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { StatusBadge } from "@/components/status-badge";
import { ProjectTabs } from "@/components/project-tabs";
import { MediaThumb } from "@/components/media-thumb";
import { ArrowLeft } from "lucide-react";
import {
  ApiClientError,
  approvePlan,
  generatePlan,
  getPlan,
  getProjectStatus,
  postPlanDecision,
  uploadAsset,
  type PlanDocument,
  type PlanSentence,
  type ProjectStatus,
  type UploadRecord,
} from "@/lib/api";

function coverageLabel(coverage: PlanSentence["coverage"]) {
  return coverage.replaceAll("_", " ");
}

/**
 * Replacement state per sentence (Brief Stage 5, C-04). The old
 * control asked the reviewer to type a file path on the server;
 * the replacement is now an uploaded file: the Stage 6 upload
 * endpoint probes it and stamps it user-supplied with unknown
 * rights, and only then can it be offered to the plan-decision
 * replace, which consumes the upload's stored path exactly as
 * the contract specifies. Outcomes the backend records as
 * failures (swap_failed_*, replacement_probe_failed,
 * replacement_file_missing) are presented as NOT applied — the
 * decision endpoint returns 201 with the outcome recorded, so
 * the outcome string, not the HTTP status, is the truth.
 */
type ReplacementUpload =
  | { status: "uploading"; fileName: string; progress: number }
  | { status: "ready"; upload: UploadRecord }
  | { status: "error"; fileName: string; message: string };

const APPLIED_REPLACE_OUTCOMES = ["swapped", "recorded_pre_run"];

// Redesign F2 (plan §3.7): presentation restructured — the
// coverage/decision summary header stays, and the sentence
// table becomes a scene-grouped list (script column left,
// visual decision right). Every wiring below — decisions,
// replacement upload, plan approval — is unchanged in
// semantics. Visual Director Pass 1/Pass 2 data is NOT shown:
// visual_plan is not API-exposed yet, and no chrome is faked
// for it (§3.7 programme note).
export default function PlanReviewPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [plan, setPlan] = useState<PlanDocument | null>(null);
  const [status, setStatus] = useState<ProjectStatus | null>(null);
  const [busy, setBusy] = useState(true);
  const [acting, setActing] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [uploads, setUploads] = useState<Record<number, ReplacementUpload>>({});

  const load = useCallback(async () => {
    setError("");
    try {
      const [planResult, statusResult] = await Promise.allSettled([
        getPlan(projectId),
        getProjectStatus(projectId),
      ]);
      if (statusResult.status === "fulfilled") setStatus(statusResult.value);
      if (planResult.status === "fulfilled") {
        setPlan(planResult.value);
      } else {
        setPlan(null);
        const cause = planResult.reason;
        if (!(cause instanceof ApiClientError && cause.status === 404)) {
          setError(cause instanceof Error ? cause.message : "The plan could not be loaded.");
        }
      }
    } finally {
      setBusy(false);
    }
  }, [projectId]);

  useEffect(() => {
    const timer = setTimeout(() => { void load(); }, 0);
    return () => clearTimeout(timer);
  }, [load]);

  async function onGenerate() {
    setActing("generate");
    setMessage("Running story analysis. This produces the plan only — no footage is retrieved.");
    try {
      const generated = await generatePlan(projectId);
      setPlan(generated);
      setMessage("Plan generated from story analysis.");
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : "Plan generation failed.");
    } finally {
      setActing("");
    }
  }

  async function onApprovePlan() {
    setActing("approve-plan");
    try {
      const result = await approvePlan(projectId);
      setMessage(
        result.released
          ? `Plan approved. Production released${result.regenerate_sentences.length ? ` with ${result.regenerate_sentences.length} sentence(s) marked for re-retrieval` : ""}.`
          : "Plan approval recorded.",
      );
      await load();
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : "Plan approval failed.");
    } finally {
      setActing("");
    }
  }

  async function onDecision(sentence: PlanSentence, action: "approve" | "regenerate") {
    setActing(`${action}-${sentence.sentence_number}`);
    try {
      const decision = await postPlanDecision(projectId, {
        sentence_number: sentence.sentence_number,
        action,
      });
      setMessage(
        `Sentence ${sentence.sentence_number}: ${action} recorded (${decision.outcome || "recorded"}).${decision.outcome_note ? ` ${decision.outcome_note}` : ""}`,
      );
      await load();
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : "The decision could not be recorded.");
    } finally {
      setActing("");
    }
  }

  async function onUpload(sentence: PlanSentence, file: File) {
    const sentenceNumber = sentence.sentence_number;
    setUploads((current) => ({
      ...current,
      [sentenceNumber]: { status: "uploading", fileName: file.name, progress: 0 },
    }));
    try {
      const upload = await uploadAsset(projectId, file, (fraction) => {
        setUploads((current) => {
          const existing = current[sentenceNumber];
          if (!existing || existing.status !== "uploading") return current;
          return { ...current, [sentenceNumber]: { ...existing, progress: fraction } };
        });
      });
      setUploads((current) => ({ ...current, [sentenceNumber]: { status: "ready", upload } }));
      setMessage(
        `Uploaded ${upload.file_name} for sentence ${sentenceNumber}. It is recorded as user-supplied with licence unknown — review it, then apply it as the replacement.`,
      );
    } catch (cause) {
      setUploads((current) => ({
        ...current,
        [sentenceNumber]: {
          status: "error",
          fileName: file.name,
          message: cause instanceof Error ? cause.message : "The upload failed.",
        },
      }));
    }
  }

  async function onReplaceWithUpload(sentence: PlanSentence) {
    const state = uploads[sentence.sentence_number];
    if (!state || state.status !== "ready") return;
    setActing(`replace-${sentence.sentence_number}`);
    try {
      const decision = await postPlanDecision(projectId, {
        sentence_number: sentence.sentence_number,
        action: "replace",
        replacement: {
          file_path: state.upload.file_path,
          title: state.upload.title || state.upload.file_name,
        },
      });
      const outcome = decision.outcome || "recorded";
      setMessage(
        APPLIED_REPLACE_OUTCOMES.includes(outcome)
          ? `Sentence ${sentence.sentence_number}: replacement applied (${outcome}). The replacement was freshly probed and its evidence re-earned; nothing was inherited from the original.${decision.outcome_note ? ` ${decision.outcome_note}` : ""}`
          : `Sentence ${sentence.sentence_number}: replacement NOT applied (${outcome}). The current visual is unchanged.${decision.outcome_note ? ` ${decision.outcome_note}` : ""}`,
      );
      if (APPLIED_REPLACE_OUTCOMES.includes(outcome)) {
        setUploads((current) => {
          const next = { ...current };
          delete next[sentence.sentence_number];
          return next;
        });
      }
      await load();
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : "The replacement could not be recorded.");
    } finally {
      setActing("");
    }
  }

  const awaiting = status?.status === "awaiting_approval";

  // Scene groups from the plan's own scene index; any sentence
  // the index does not name keeps its recorded scene number as
  // its group, so no sentence is ever dropped by the grouping.
  const sceneGroups: Array<{ sceneNumber: number | null; sentences: PlanSentence[] }> = [];
  if (plan) {
    const byNumber = new Map(plan.sentences.map((sentence) => [sentence.sentence_number, sentence]));
    const grouped = new Set<number>();
    for (const scene of plan.scenes || []) {
      const sentences = (scene.sentence_numbers || [])
        .map((number) => byNumber.get(number))
        .filter((sentence): sentence is PlanSentence => !!sentence);
      if (sentences.length > 0) {
        sceneGroups.push({ sceneNumber: scene.scene_number, sentences });
        for (const sentence of sentences) grouped.add(sentence.sentence_number);
      }
    }
    const rest = plan.sentences.filter((sentence) => !grouped.has(sentence.sentence_number));
    if (rest.length > 0) {
      const byScene = new Map<number | null, PlanSentence[]>();
      for (const sentence of rest) {
        const key = sentence.scene_number ?? null;
        byScene.set(key, [...(byScene.get(key) || []), sentence]);
      }
      for (const [sceneNumber, sentences] of byScene) sceneGroups.push({ sceneNumber, sentences });
    }
  }

  function sentenceCard(sentence: PlanSentence) {
    const lastDecision = sentence.decisions[sentence.decisions.length - 1];
    const uploadState = uploads[sentence.sentence_number];
    return (
      <article className="plan-sentence" key={sentence.sentence_number}>
        <div className="plan-sentence-script">
          <p className="plan-sentence-index">Sentence {sentence.sentence_number}{sentence.subject ? ` · ${sentence.subject}` : ""}</p>
          <p>{sentence.text}</p>
          {sentence.entities.length > 0 && <p className="plan-decision-note">Entities: {sentence.entities.join(", ")}</p>}
        </div>
        <div className="plan-sentence-decision">
          <p className="overview-run-line">
            <span className={`plan-coverage plan-coverage-${sentence.coverage}`}>
              {coverageLabel(sentence.coverage)}
            </span>
            <span className="plan-decision-note">
              Evidence: {sentence.evidence_class || "—"} · Rights:{" "}
              {sentence.rights
                ? `${sentence.rights.license || "unknown"}${sentence.rights.rights_status ? ` · ${sentence.rights.rights_status}` : ""}`
                : "—"}
            </span>
          </p>
          <p className="plan-decision-note">
            Proposed:{" "}
            {sentence.proposed_visual
              ? `${sentence.proposed_visual.kind || "visual"} — ${sentence.proposed_visual.description}`
              : "—"}
          </p>
          {sentence.fallback_concepts &&
            (sentence.fallback_concepts.activities.length > 0 ||
              sentence.fallback_concepts.objects.length > 0 ||
              sentence.fallback_concepts.settings.length > 0 ||
              sentence.fallback_concepts.place_types.length > 0) && (
              <p className="plan-decision-note">
                B-roll intent:{" "}
                {[
                  ...sentence.fallback_concepts.activities,
                  ...sentence.fallback_concepts.objects,
                  ...sentence.fallback_concepts.settings,
                  ...sentence.fallback_concepts.place_types,
                ].join(" · ")}
              </p>
            )}
          {sentence.chosen_visual ? (
            <span className="plan-chosen-visual">
              {sentence.chosen_visual.media_key && (
                <MediaThumb
                  projectId={projectId}
                  mediaKey={sentence.chosen_visual.media_key}
                  alt={sentence.chosen_visual.title || `Chosen visual for sentence ${sentence.sentence_number}`}
                />
              )}
              <span className="plan-decision-note">Chosen: {`${sentence.chosen_visual.type || "visual"} · ${sentence.chosen_visual.provider || "unknown source"}`}</span>
            </span>
          ) : (
            <p className="plan-decision-note">Chosen: —</p>
          )}
          <div className="plan-row-actions">
            <button
              type="button"
              disabled={acting !== ""}
              onClick={() => void onDecision(sentence, "approve")}
            >
              Approve
            </button>
            <button
              type="button"
              disabled={acting !== ""}
              onClick={() => void onDecision(sentence, "regenerate")}
            >
              Regenerate
            </button>
            <span className="plan-upload">
              <label className="plan-upload-pick">
                <input
                  type="file"
                  accept="video/*,image/*,audio/*"
                  aria-label={`Upload a replacement file for sentence ${sentence.sentence_number}`}
                  disabled={acting !== "" || uploadState?.status === "uploading"}
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    event.target.value = "";
                    if (file) void onUpload(sentence, file);
                  }}
                />
                {uploadState?.status === "ready" ? "Choose a different file" : "Upload replacement…"}
              </label>
              {uploadState?.status === "uploading" && (
                <small className="plan-upload-note" role="status">
                  Uploading {uploadState.fileName} — {Math.round(uploadState.progress * 100)}%
                </small>
              )}
              {uploadState?.status === "error" && (
                <small className="plan-upload-note plan-upload-error" role="alert">
                  Upload of {uploadState.fileName} failed: {uploadState.message} The current visual is unchanged.
                </small>
              )}
              {uploadState?.status === "ready" && (
                <>
                  <small className="plan-upload-note">
                    {uploadState.upload.file_name} · {uploadState.upload.type}
                    {uploadState.upload.source_duration != null
                      ? ` · ${Math.round(uploadState.upload.source_duration)}s`
                      : ""}{" "}
                    · user-supplied · licence unknown — verify before publish
                  </small>
                  <button
                    type="button"
                    disabled={acting !== ""}
                    onClick={() => void onReplaceWithUpload(sentence)}
                  >
                    {acting === `replace-${sentence.sentence_number}` ? "Replacing…" : "Replace with this upload"}
                  </button>
                </>
              )}
            </span>
            {lastDecision && (
              <small className="plan-decision-note">
                Last: {lastDecision.action} — {lastDecision.outcome || "recorded"}
              </small>
            )}
          </div>
        </div>
      </article>
    );
  }

  return (
    <main className="app-page plan-page">
      <AppNavigation />
      <ProjectTabs projectId={projectId} />
      <section className="plan-content">
        <Link href={`/projects/${projectId}`} className="editor-back"><ArrowLeft size={14} strokeWidth={2} aria-hidden /> Project</Link>
        <p className="eyebrow">VISUAL PLAN REVIEW</p>
        <h1>{plan?.title || plan?.bible.title || "Production plan"}</h1>
        <p className="plan-intro">
          One entry per script sentence, grouped by scene: the script on the left; on the
          right, the proposed visual from story analysis, the visual a run actually chose,
          its evidence class, its rights classification, and its coverage state. Decisions
          you record here are kept with the production — replacements are re-probed and
          re-verified, never inherited from the original.
        </p>

        {status && (
          <p className="plan-status-line">
            Project status: <StatusBadge status={status.status} />
            {status.current_stage ? ` · Stage: ${status.current_stage.replaceAll("_", " ")}` : ""}
            {status.latest_run ? (
              <>
                {" "}· Latest run: <StatusBadge status={status.latest_run.status} />
              </>
            ) : (
              ""
            )}
          </p>
        )}

        <div className="plan-actions">
          {!plan && (
            <button type="button" className="button button-primary" disabled={acting === "generate"} onClick={() => void onGenerate()}>
              {acting === "generate" ? "Analysing…" : "Generate plan"}
            </button>
          )}
          {plan && (
            <button type="button" className="button button-secondary" disabled={busy} onClick={() => { setBusy(true); void load(); }}>
              Refresh
            </button>
          )}
          {awaiting && (
            <button type="button" className="button button-primary" disabled={acting === "approve-plan"} onClick={() => void onApprovePlan()}>
              {acting === "approve-plan" ? "Releasing…" : "Approve plan and start production"}
            </button>
          )}
          {plan && !awaiting && status?.latest_run?.status !== "completed" && (
            <span className="plan-actions-note">Plan approval applies when a run is waiting for approval.</span>
          )}
        </div>

        {message && <p className="plan-message" role="status">{message}</p>}
        {error && <p className="plan-message plan-error" role="alert">{error}</p>}
        {busy && <p className="plan-message" role="status">Loading the production plan…</p>}

        {!busy && !plan && !error && (
          <div className="plan-empty">
            <h2>No plan yet</h2>
            <p>
              Generate the plan to run story analysis for this project. Analysis reads the script
              and plans a visual for every sentence; it does not retrieve footage or render
              anything, and it is the same analysis a production run performs first.
            </p>
          </div>
        )}

        {plan && (
          <>
            <div className="plan-summary" aria-label="Plan summary">
              <div><span>SENTENCES</span><strong>{plan.summary.sentences}</strong></div>
              <div><span>VERIFIED</span><strong>{plan.summary.verified}</strong></div>
              <div><span>ROTATION</span><strong>{plan.summary.rotation}</strong></div>
              <div><span>GRAPHIC</span><strong>{plan.summary.graphic}</strong></div>
              <div><span>MISSING</span><strong>{plan.summary.missing}</strong></div>
              <div><span>UNCOVERED</span><strong>{plan.summary.uncovered}</strong></div>
              <div><span>DECISIONS</span><strong>{plan.summary.decisions_recorded}</strong></div>
            </div>
            <p className="plan-meta">
              {plan.bible.story_type ? `${plan.bible.story_type} · ` : ""}
              {plan.bible.time_period ? `${plan.bible.time_period} · ` : ""}
              {plan.bible.entity_count} entities tracked
              {plan.analysis_mode ? ` · analysis mode: ${plan.analysis_mode}` : ""}
              {plan.plan_source === "story_cache"
                ? " · assembled from the story-understanding cache (this production predates the plan document)"
                : ""}
            </p>
            {sceneGroups.map((group) => (
              <section key={group.sceneNumber ?? "none"} aria-label={group.sceneNumber ? `Scene ${group.sceneNumber}` : "Sentences"}>
                <h2>{group.sceneNumber ? `Scene ${group.sceneNumber}` : "Sentences"}</h2>
                <div className="plan-sentences">{group.sentences.map(sentenceCard)}</div>
              </section>
            ))}
          </>
        )}
      </section>
    </main>
  );
}
