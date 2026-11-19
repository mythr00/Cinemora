"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import {
  ApiClientError,
  approvePlan,
  generatePlan,
  getPlan,
  getProjectStatus,
  postPlanDecision,
  type PlanDocument,
  type PlanSentence,
  type ProjectStatus,
} from "@/lib/api";

function coverageLabel(coverage: PlanSentence["coverage"]) {
  return coverage.replaceAll("_", " ");
}

export default function PlanReviewPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [plan, setPlan] = useState<PlanDocument | null>(null);
  const [status, setStatus] = useState<ProjectStatus | null>(null);
  const [busy, setBusy] = useState(true);
  const [acting, setActing] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [replacements, setReplacements] = useState<Record<number, string>>({});

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

  async function onDecision(sentence: PlanSentence, action: "approve" | "regenerate" | "replace") {
    setActing(`${action}-${sentence.sentence_number}`);
    try {
      const replacementPath = (replacements[sentence.sentence_number] || "").trim();
      const decision = await postPlanDecision(projectId, {
        sentence_number: sentence.sentence_number,
        action,
        replacement: action === "replace" ? { file_path: replacementPath } : undefined,
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

  const awaiting = status?.status === "awaiting_approval";

  return (
    <main className="app-page plan-page">
      <AppNavigation active="/projects" />
      <section className="plan-content">
        <Link href={`/projects/${projectId}`} className="editor-back">← Project</Link>
        <p className="eyebrow">PRODUCTION PLAN</p>
        <h1>{plan?.title || plan?.bible.title || "Production plan"}</h1>
        <p className="plan-intro">
          One row per script sentence: the proposed visual from story analysis, the visual a run
          actually chose, its evidence class, its rights classification, and its coverage state.
          Decisions you record here are kept with the production — replacements are re-probed and
          re-verified, never inherited from the original.
        </p>

        {status && (
          <p className="plan-status-line">
            Project status: <strong>{status.status.replaceAll("_", " ")}</strong>
            {status.current_stage ? ` · Stage: ${status.current_stage.replaceAll("_", " ")}` : ""}
            {status.latest_run ? ` · Latest run: ${status.latest_run.status.replaceAll("_", " ")}` : ""}
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
            <div className="plan-table-wrap">
              <table className="plan-table">
                <thead>
                  <tr>
                    <th scope="col">#</th>
                    <th scope="col">Scene</th>
                    <th scope="col">Sentence</th>
                    <th scope="col">Subject</th>
                    <th scope="col">Proposed visual</th>
                    <th scope="col">Chosen visual</th>
                    <th scope="col">Evidence</th>
                    <th scope="col">Rights</th>
                    <th scope="col">Coverage</th>
                    <th scope="col">Review</th>
                  </tr>
                </thead>
                <tbody>
                  {plan.sentences.map((sentence) => {
                    const lastDecision = sentence.decisions[sentence.decisions.length - 1];
                    return (
                      <tr key={sentence.sentence_number}>
                        <td>{sentence.sentence_number}</td>
                        <td>{sentence.scene_number ?? "—"}</td>
                        <td className="plan-sentence-text">{sentence.text}</td>
                        <td>{sentence.subject || "—"}</td>
                        <td>
                          {sentence.proposed_visual
                            ? `${sentence.proposed_visual.kind || "visual"} — ${sentence.proposed_visual.description}`
                            : "—"}
                        </td>
                        <td>
                          {sentence.chosen_visual
                            ? `${sentence.chosen_visual.type || "visual"} · ${sentence.chosen_visual.provider || "unknown source"}`
                            : "—"}
                        </td>
                        <td>{sentence.evidence_class || "—"}</td>
                        <td>
                          {sentence.rights
                            ? `${sentence.rights.license || "unknown"}${sentence.rights.rights_status ? ` · ${sentence.rights.rights_status}` : ""}`
                            : "—"}
                        </td>
                        <td>
                          <span className={`plan-coverage plan-coverage-${sentence.coverage}`}>
                            {coverageLabel(sentence.coverage)}
                          </span>
                        </td>
                        <td>
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
                            <span className="plan-replace">
                              <input
                                type="text"
                                placeholder="Replacement file path on the server"
                                value={replacements[sentence.sentence_number] || ""}
                                onChange={(event) =>
                                  setReplacements((current) => ({
                                    ...current,
                                    [sentence.sentence_number]: event.target.value,
                                  }))
                                }
                              />
                              <button
                                type="button"
                                disabled={acting !== "" || !(replacements[sentence.sentence_number] || "").trim()}
                                onClick={() => void onDecision(sentence, "replace")}
                              >
                                Replace
                              </button>
                            </span>
                            {lastDecision && (
                              <small className="plan-decision-note">
                                Last: {lastDecision.action} — {lastDecision.outcome || "recorded"}
                              </small>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}
      </section>
    </main>
  );
}
