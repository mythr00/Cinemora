"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { StatusBadge } from "@/components/status-badge";
import { ProjectTabs } from "@/components/project-tabs";
import { VerdictChip } from "@/components/verdict-chip";
import { ErrorState, LoadingState } from "@/components/states";
import { AuthenticatedDownloadLink } from "@/components/authenticated-media";
import { ArrowLeft, Printer } from "lucide-react";
import {
  ApiClientError,
  getProject,
  getQaReport,
  getRunQaReport,
  listRuns,
  qaReportExportPath,
  type QaReport,
  type RunSummary,
} from "@/lib/api";

/**
 * QA Report (Phase F, founder priority #8) — the unified
 * Documentary Quality Report for one production run, assembled by
 * the backend from the artefacts the production itself recorded
 * (production report, visual-quality measurement, asset report,
 * delivery record). Nothing on this page is recomputed: verdicts
 * and metrics render exactly as recorded, with the thresholds
 * they were judged against; a section whose artefact was never
 * written renders as "not recorded", and an artefact that exists
 * but cannot be read renders as "unavailable" — the absence of a
 * report is never shown as a pass. The JSON export is the same
 * recorded report, downloaded.
 */

function BandBadge({ band }: { band?: string | null }) {
  return <StatusBadge status={(band || "unknown").toLowerCase()} />;
}

function pct(value?: number | null): string {
  return value == null ? "—" : `${(value * 100).toFixed(1)}%`;
}

function num(value?: number | null, digits = 2): string {
  return value == null ? "—" : value.toFixed(digits);
}

function NotRecorded({ state, what }: { state: string; what: string }) {
  return (
    <p className="plan-meta">
      {state === "unavailable"
        ? `${what} was recorded for this run but could not be read, so nothing is shown for it.`
        : `${what} was not recorded for this run — the run predates this record or its measurement failed and was recorded elsewhere. Nothing is shown, and the absence is not a pass.`}
    </p>
  );
}

export default function QaReportPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [report, setReport] = useState<QaReport | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selectedRun, setSelectedRun] = useState<string>("");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [noRuns, setNoRuns] = useState(false);

  const load = useCallback(
    async (runId?: string) => {
      setError(null);
      setNoRuns(false);
      try {
        const loaded = runId
          ? await getRunQaReport(projectId, runId)
          : await getQaReport(projectId);
        setReport(loaded);
        setSelectedRun(loaded.run.run_id || runId || "");
        // The run list is best-effort beside the report itself.
        listRuns(projectId)
          .then((result) => setRuns(result.runs))
          .catch(() => undefined);
      } catch (cause) {
        setReport(null);
        if (cause instanceof ApiClientError && cause.status === 404) {
          if (cause.code === "no_runs") {
            setNoRuns(true);
            return;
          }
          if (!cause.code) {
            try {
              await getProject(projectId);
              setNoRuns(true);
              return;
            } catch {
              // fall through to the error state
            }
          }
        }
        setError(cause);
      } finally {
        setBusy(false);
      }
    },
    [projectId],
  );

  useEffect(() => {
    const timer = setTimeout(() => { void load(); }, 0);
    return () => clearTimeout(timer);
  }, [load]);

  const verdicts = report?.verdicts.state === "recorded" ? report.verdicts.verdicts : null;
  const qualityMetrics = verdicts?.quality?.metrics ?? null;

  return (
    <main className="app-page qa-page">
      <AppNavigation />
      <ProjectTabs projectId={projectId} />
      <section className="plan-content">
        <Link href={`/projects/${projectId}`} className="editor-back"><ArrowLeft size={14} strokeWidth={2} aria-hidden /> Project</Link>
        <p className="eyebrow">QUALITY ASSURANCE</p>
        <h1>QA report</h1>
        <p className="plan-intro">
          Every check the production recorded for this run — verdicts, quality metrics with the
          thresholds they were judged against, coverage, frame verification, defects, audio,
          captions and technical delivery — assembled from the run&apos;s own records. Nothing
          here is recomputed, and a check that did not run shows as not recorded.
        </p>

        {busy && <LoadingState className="plan-message" label="Loading the QA report…" />}
        {!busy && error != null && (
          <ErrorState error={error} onRetry={() => { setBusy(true); void load(selectedRun || undefined); }} />
        )}
        {!busy && noRuns && (
          <div className="plan-empty">
            <h2>No QA report yet</h2>
            <p>
              This project has no production runs, so there are no recorded checks to report.
              A QA report is assembled from a run&apos;s production records once a production
              has run.
            </p>
          </div>
        )}

        {report && (
          <>
            <div className="qa-controls">
              <label>
                Run{" "}
                <select
                  value={selectedRun}
                  onChange={(event) => {
                    const runId = event.target.value;
                    setBusy(true);
                    void load(runId);
                  }}
                >
                  {(runs.length > 0
                    ? runs
                    : [{
                        run_id: report.run.run_id || "",
                        project_id: projectId,
                        status: report.run.status || "",
                        created_at: report.run.created_at ?? null,
                      } satisfies RunSummary]
                  ).map((run) => (
                    <option key={run.run_id} value={run.run_id}>
                      {run.run_id.slice(0, 8)}… · {run.status}
                      {run.created_at ? ` · ${new Date(run.created_at).toLocaleString()}` : ""}
                    </option>
                  ))}
                </select>
              </label>
              {report.run.run_id && (
                <AuthenticatedDownloadLink
                  path={qaReportExportPath(projectId, report.run.run_id)}
                  filename={`qa-report-${projectId}-${report.run.run_id}.json`}
                >
                  Download JSON
                </AuthenticatedDownloadLink>
              )}
              <button type="button" onClick={() => window.print()}>
                <Printer size={14} strokeWidth={2} aria-hidden /> Print
              </button>
            </div>

            <div className="readiness-header" aria-label="Recorded verdicts">
              <VerdictChip label="Integrity" verdict={verdicts?.integrity?.verdict} />
              <VerdictChip label="Quality" verdict={verdicts?.quality?.verdict} />
              <VerdictChip label="Reliability" verdict={verdicts?.reliability?.verdict} />
              <p className="readiness-rule">
                The three verdicts are recorded separately and read separately — a completed
                render is not a quality pass, and neither is a delivery approval.
                {report.verdicts.state !== "recorded"
                  ? " No production report was recorded for this run, so the verdicts read as not recorded."
                  : ""}
              </p>
            </div>
            <p className="plan-meta">
              Run {report.run.run_id || "—"} · status{" "}
              {report.run.status ? <StatusBadge status={report.run.status} /> : "—"}
              {report.run.finished_at ? ` · finished ${new Date(report.run.finished_at).toLocaleString()}` : ""}
              {" · "}Records:{" "}
              {Object.entries(report.artefacts)
                .map(([name, state]) => `${name.replace(".json", "")} ${state.replace("_", " ")}`)
                .join(" · ")}
            </p>

            <section aria-label="Quality metrics">
              <h2>Quality metrics</h2>
              {report.verdicts.state !== "recorded" || !qualityMetrics ? (
                <NotRecorded state={report.verdicts.state} what="The quality verdict" />
              ) : (
                <>
                  <div className="plan-table-wrap">
                    <table className="plan-table">
                      <thead>
                        <tr>
                          <th scope="col">Metric</th>
                          <th scope="col">Recorded value</th>
                          <th scope="col">Band</th>
                          <th scope="col">Basis</th>
                          <th scope="col">Thresholds it was judged against</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(qualityMetrics).map(([name, metric]) => (
                          <tr key={name}>
                            <td>{name.replaceAll("_", " ")}</td>
                            <td>{metric.value == null ? "not recorded" : num(metric.value, 3)}</td>
                            <td><BandBadge band={metric.band} /></td>
                            <td>{metric.basis || "—"}</td>
                            <td>{metric.thresholds || "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {(() => {
                    const reasons = [
                      ...(verdicts?.integrity?.failures ?? []),
                      ...(verdicts?.quality?.fail_reasons ?? []),
                      ...(verdicts?.quality?.warning_reasons ?? []),
                      ...(verdicts?.integrity?.warnings ?? []),
                    ];
                    return reasons.length > 0 ? (
                      <>
                        <p className="plan-meta">Reasons recorded with the verdicts:</p>
                        <ul>
                          {reasons.map((reason) => (
                            <li key={reason}>{reason}</li>
                          ))}
                        </ul>
                      </>
                    ) : null;
                  })()}
                  {(verdicts?.quality?.unknown_metrics?.length ?? 0) > 0 && (
                    <p className="plan-meta">
                      Metrics that could not be evaluated from the records:{" "}
                      {verdicts?.quality?.unknown_metrics?.join(", ")}.
                    </p>
                  )}
                </>
              )}
            </section>

            <section aria-label="Coverage and tiers">
              <h2>Coverage &amp; visual tiers</h2>
              {report.coverage.state !== "recorded" ? (
                <NotRecorded state={report.coverage.state} what="Coverage" />
              ) : (
                <>
                  <div className="plan-summary" aria-label="Coverage summary">
                    {report.coverage.sentence_outcomes?.sentence_count != null && (
                      <div><span>SENTENCES</span><strong>{report.coverage.sentence_outcomes.sentence_count}</strong></div>
                    )}
                    {report.coverage.asset_report_coverage?.covered_sentences != null && (
                      <div><span>COVERED</span><strong>{report.coverage.asset_report_coverage.covered_sentences}</strong></div>
                    )}
                    {report.coverage.sentence_outcomes?.sentences_without_verified_visual != null && (
                      <div><span>NO VERIFIED VISUAL</span><strong>{report.coverage.sentence_outcomes.sentences_without_verified_visual}</strong></div>
                    )}
                    {report.diversity.diversity?.d1_unique_assets != null && (
                      <div><span>UNIQUE VISUALS (D1)</span><strong>{String(report.diversity.diversity.d1_unique_assets)}</strong></div>
                    )}
                    {report.diversity.diversity?.d2_repeated_item_rate != null && (
                      <div><span>REPEATED-ITEM RATE (D2)</span><strong>{num(Number(report.diversity.diversity.d2_repeated_item_rate), 3)}</strong></div>
                    )}
                    {report.diversity.diversity?.variety_ratio != null && (
                      <div><span>VARIETY RATIO</span><strong>{num(Number(report.diversity.diversity.variety_ratio), 3)}</strong></div>
                    )}
                  </div>
                  {report.coverage.sentence_outcomes?.buckets && (
                    <p className="plan-meta">
                      Sentence outcomes:{" "}
                      {Object.entries(report.coverage.sentence_outcomes.buckets)
                        .map(([key, value]) => `${key.replaceAll("_", " ")} ${value}`)
                        .join(" · ")}
                      {report.coverage.sentence_outcomes.reconciles === true
                        ? " Outcome counts reconcile to the sentence total."
                        : report.coverage.sentence_outcomes.reconciles === false
                          ? " Outcome counts do NOT reconcile to the sentence total."
                          : ""}
                    </p>
                  )}
                  {report.tiers.tier_accounting?.by_tier && (
                    <div className="plan-table-wrap">
                      <table className="plan-table">
                        <thead>
                          <tr>
                            <th scope="col">Tier</th>
                            <th scope="col">Items</th>
                            <th scope="col">Runtime</th>
                            <th scope="col">Runtime share</th>
                          </tr>
                        </thead>
                        <tbody>
                          {Object.entries(report.tiers.tier_accounting.by_tier).map(([tier, block]) => (
                            <tr key={tier}>
                              <td>{tier}</td>
                              <td>{block.count ?? "—"}</td>
                              <td>{block.runtime_seconds == null ? "—" : `${num(block.runtime_seconds, 1)}s`}</td>
                              <td>{pct(block.runtime_share)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                  {report.tiers.tier_accounting?.band && (
                    <p className="plan-meta">
                      B-roll share {pct(report.tiers.tier_accounting.broll_share)} against the{" "}
                      {report.tiers.tier_accounting.band.genre_pack || "genre"} band maximum{" "}
                      {pct(report.tiers.tier_accounting.band.max)} — recorded evaluation:{" "}
                      {report.tiers.tier_accounting.band.evaluation?.replaceAll("_", " ") || "not recorded"}.
                    </p>
                  )}
                </>
              )}
            </section>

            <section aria-label="Per-sentence findings">
              <h2>Per-sentence findings</h2>
              {report.sentences.state !== "recorded" || report.sentences.sentences.length === 0 ? (
                <NotRecorded state={report.sentences.state} what="Per-sentence outcomes" />
              ) : (
                <div className="plan-table-wrap">
                  <table className="plan-table">
                    <thead>
                      <tr>
                        <th scope="col">Sentence</th>
                        <th scope="col">Scene</th>
                        <th scope="col">Outcome</th>
                        <th scope="col">Tier</th>
                        <th scope="col">Missing visual record</th>
                      </tr>
                    </thead>
                    <tbody>
                      {report.sentences.sentences.map((sentence) => (
                        <tr key={sentence.sentence_number}>
                          <td>{sentence.sentence_number}</td>
                          <td>{sentence.scene_number ?? "—"}</td>
                          <td>{sentence.outcome?.replaceAll("_", " ") || "not recorded"}</td>
                          <td>{sentence.tier || "not recorded"}</td>
                          <td>
                            {sentence.missing_visual
                              ? `${sentence.missing_visual.reason || "recorded"}${
                                  sentence.missing_visual.graphics_outcome?.outcome
                                    ? ` · graphic ${String(sentence.missing_visual.graphics_outcome.outcome)}`
                                    : ""
                                }`
                              : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section aria-label="Frame verification">
              <h2>Frame verification</h2>
              {report.frame_verification.state !== "recorded" || !report.frame_verification.summary ? (
                <NotRecorded state={report.frame_verification.state} what="Frame verification" />
              ) : (
                <>
                  <div className="plan-summary" aria-label="Frame verification summary">
                    <div><span>ASSETS EVALUATED</span><strong>{report.frame_verification.summary.evaluated_assets ?? "—"}</strong></div>
                    <div><span>REJECTION EVENTS</span><strong>{report.frame_verification.summary.rejected_events ?? "—"}</strong></div>
                    <div><span>WATERMARKED PREVIEWS</span><strong>{report.frame_verification.summary.watermarked_preview_detected ?? "—"}</strong></div>
                    <div><span>OCR ENGINE</span><strong>{report.frame_verification.summary.ocr_engine || "not recorded"}</strong></div>
                  </div>
                  {(report.frame_verification.rejections?.length ?? 0) > 0 ? (
                    <div className="plan-table-wrap">
                      <table className="plan-table">
                        <thead>
                          <tr>
                            <th scope="col">Candidate</th>
                            <th scope="col">Scene</th>
                            <th scope="col">Provider</th>
                            <th scope="col">Reason recorded</th>
                            <th scope="col">Source</th>
                          </tr>
                        </thead>
                        <tbody>
                          {report.frame_verification.rejections?.map((rejection, index) => (
                            <tr key={`${rejection.candidate_id || "candidate"}-${index}`}>
                              <td>{rejection.candidate_id || "—"}</td>
                              <td>{rejection.scene ?? "—"}</td>
                              <td>{rejection.provider || "—"}</td>
                              <td>
                                {rejection.reason || rejection.primary_rejection_reason || "—"}
                                {(rejection.verdict_reasons?.length ?? 0) > 0
                                  ? ` (${rejection.verdict_reasons?.join("; ")})`
                                  : ""}
                              </td>
                              <td>
                                {rejection.url ? (
                                  <a href={rejection.url} target="_blank" rel="noreferrer">view source</a>
                                ) : (
                                  "—"
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <p className="plan-meta">No frame-verification rejections were recorded for this run.</p>
                  )}
                </>
              )}
            </section>

            <section aria-label="Candidates and defects">
              <h2>Candidates, substitutions &amp; defects</h2>
              {report.candidates.state !== "recorded" ? (
                <NotRecorded state={report.candidates.state} what="The candidate funnel" />
              ) : (
                <>
                  <div className="plan-summary" aria-label="Candidate totals">
                    {report.candidates.totals &&
                      Object.entries(report.candidates.totals)
                        .filter(([, value]) => value != null)
                        .map(([key, value]) => (
                          <div key={key}>
                            <span>{key.replaceAll("_", " ").toUpperCase()}</span>
                            <strong>{value}</strong>
                          </div>
                        ))}
                  </div>
                  {report.candidates.funnel?.by_stage && (
                    <p className="plan-meta">
                      Funnel by stage:{" "}
                      {Object.entries(report.candidates.funnel.by_stage)
                        .map(([stage, count]) => `${stage.replaceAll("_", " ")} ${count}`)
                        .join(" · ")}
                    </p>
                  )}
                  {report.candidates.rejection_reasons?.distribution && (
                    <p className="plan-meta">
                      Recorded rejection reasons:{" "}
                      {Object.entries(report.candidates.rejection_reasons.distribution)
                        .map(([reason, count]) => `${reason.replaceAll("_", " ")} ${count}`)
                        .join(" · ")}
                    </p>
                  )}
                  {report.defects.substitutions && (
                    <p className="plan-meta">
                      Substitutions recorded:{" "}
                      {Object.entries(report.defects.substitutions)
                        .map(([key, value]) => `${key.replaceAll("_", " ")} ${value}`)
                        .join(" · ")}
                      .
                    </p>
                  )}
                  {report.defects.visual_quality_error != null && (
                    <p className="plan-meta">
                      The quality measurement itself failed for this run and that failure was
                      recorded in the asset report — the film completed without verdicts.
                    </p>
                  )}
                  {report.run.error && (
                    <p className="plan-meta">Run error recorded: {report.run.error}</p>
                  )}
                </>
              )}
            </section>

            <section aria-label="Audio captions and delivery">
              <h2>Audio, captions &amp; technical delivery</h2>
              <div className="plan-summary" aria-label="Audio and delivery summary">
                {report.audio_captions.final_audio?.integrated_lufs != null && (
                  <div><span>LOUDNESS (LUFS)</span><strong>{num(report.audio_captions.final_audio.integrated_lufs)}</strong></div>
                )}
                {report.audio_captions.final_audio?.target_lufs != null && (
                  <div><span>LOUDNESS TARGET</span><strong>{num(report.audio_captions.final_audio.target_lufs)}</strong></div>
                )}
                {report.audio_captions.timing_consistency?.divergence_seconds != null && (
                  <div><span>TIMING DIVERGENCE</span><strong>{num(report.audio_captions.timing_consistency.divergence_seconds, 3)}s</strong></div>
                )}
                {report.delivery.duration_seconds != null && (
                  <div><span>DELIVERY DURATION</span><strong>{num(report.delivery.duration_seconds, 1)}s</strong></div>
                )}
                {report.delivery.delivery_bitrate_kbps != null && (
                  <div><span>DELIVERY BITRATE</span><strong>{num(report.delivery.delivery_bitrate_kbps, 0)} kbps</strong></div>
                )}
              </div>
              <p className="plan-meta">
                {report.audio_captions.state !== "recorded"
                  ? "Audio and caption records were not recorded for this run. "
                  : report.audio_captions.timing_consistency
                    ? `Timing consistency: ${report.audio_captions.timing_consistency.within_tolerance ? "within" : "outside"} the recorded ${num(report.audio_captions.timing_consistency.tolerance_seconds, 2)}s tolerance (source: ${report.audio_captions.timing_consistency.timing_source || "not recorded"}). `
                    : ""}
                {report.audio_captions.captions_alignment
                  ? `Captions alignment recorded: aligned ${report.audio_captions.captions_alignment.aligned ?? "—"}, low alignment ${report.audio_captions.captions_alignment.low_alignment ?? "—"}, failed ${report.audio_captions.captions_alignment.alignment_failed ?? "—"}. `
                  : ""}
                {report.delivery.state === "recorded"
                  ? `Delivery: ${report.delivery.status || "not recorded"}${report.delivery.decode_validated != null ? `, decode ${report.delivery.decode_validated ? "validated" : "not validated"}` : ""}, profile ${report.delivery.profile || "not recorded"}.`
                  : "No delivery record was recorded for this run."}
              </p>
              {report.rights.state === "recorded" && (
                <p className="plan-meta">
                  Rights recorded for {report.rights.recorded?.recorded_licences ?? "—"} assets;{" "}
                  {report.rights.recorded?.unknown_licences ?? "—"} with unknown licence.{" "}
                  {report.rights.note || ""}
                </p>
              )}
            </section>

            <section aria-label="Reconciliation and recorded questions">
              <h2>Reconciliation &amp; recorded questions</h2>
              {report.reconciliation.state !== "recorded" ? (
                <NotRecorded state={report.reconciliation.state} what="Cross-artefact reconciliation" />
              ) : (
                <>
                  <p className="plan-meta">
                    Cross-artefact reconciliation:{" "}
                    {report.reconciliation.all_reconciled
                      ? `all ${report.reconciliation.checks?.length ?? 0} checks matched.`
                      : `${report.reconciliation.checks?.filter((check) => check.status === "match").length ?? 0} of ${report.reconciliation.checks?.length ?? 0} checks matched${
                          (report.reconciliation.failures?.length ?? 0) > 0
                            ? `; mismatches: ${report.reconciliation.failures?.join(", ")}`
                            : ""
                        }.`}
                  </p>
                  {(report.reconciliation.checks?.length ?? 0) > 0 && (
                    <div className="plan-table-wrap">
                      <table className="plan-table">
                        <thead>
                          <tr>
                            <th scope="col">Check</th>
                            <th scope="col">Status</th>
                            <th scope="col">Recorded detail</th>
                          </tr>
                        </thead>
                        <tbody>
                          {report.reconciliation.checks?.map((check) => (
                            <tr key={check.name || "check"}>
                              <td>{check.name?.replaceAll("_", " ") || "—"}</td>
                              <td>{check.status || "—"}</td>
                              <td>{check.detail || "—"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                  {(report.questions.questions?.length ?? 0) > 0 && (
                    <>
                      <p className="plan-meta">Questions the production report recorded answers to:</p>
                      <ul>
                        {report.questions.questions?.map((question) => (
                          <li key={question.id || question.question || "q"}>
                            {question.question}{" "}
                            <strong>
                              {question.answer == null
                                ? question.state === "answered"
                                  ? "—"
                                  : "not answered"
                                : String(question.answer)}
                            </strong>
                          </li>
                        ))}
                      </ul>
                    </>
                  )}
                </>
              )}
            </section>
          </>
        )}
      </section>
    </main>
  );
}
