"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { StatusBadge } from "@/components/status-badge";
import { ProjectTabs } from "@/components/project-tabs";
import { ErrorState, LoadingState } from "@/components/states";
import { ArrowLeft } from "lucide-react";
import {
  ApiClientError,
  getCertificate,
  getProject,
  type Certificate,
} from "@/lib/api";

/**
 * Brief Stage 5 (C-06): this page previously presented EVERY load
 * failure — 401, 503, network down — under the heading "No
 * certificate yet" (audit D-06). The states are now distinguished
 * from the API's actual responses: the backend's certificate
 * endpoint returns 404 with code "no_runs" only when the project
 * exists but has no production runs, and 404 "project_not_found"
 * for a missing project; every other failure renders as the error
 * it is (via classifyApiError, with retry). A code-less 404 is
 * disambiguated with a project fetch before either state is shown.
 */
/**
 * Quality Recovery Phase 5: one recorded quality verdict as a
 * badge. The backend's values are PASS / QUALITY_WARNING / FAIL /
 * UNKNOWN; a null verdict (nothing evaluable was recorded) renders
 * as the unknown state — never as a pass.
 */
function VerdictBadge({ verdict }: { verdict?: string | null }) {
  return <StatusBadge status={(verdict || "unknown").toLowerCase()} />;
}

const OUTCOME_LABELS: Array<[string, string]> = [
  ["direct", "direct"],
  ["approved_graphic", "approved graphic"],
  ["contextual", "contextual"],
  ["editorial_holdover", "editorial holdover"],
  ["uncovered", "uncovered"],
];

export default function CertificatePage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [certificate, setCertificate] = useState<Certificate | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [noCertificate, setNoCertificate] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    setNoCertificate(false);
    try {
      setCertificate(await getCertificate(projectId));
    } catch (cause) {
      setCertificate(null);
      if (cause instanceof ApiClientError && cause.status === 404) {
        if (cause.code === "no_runs") {
          setNoCertificate(true);
          return;
        }
        if (!cause.code) {
          // A 404 without a machine code: ask for the project
          // itself. If the project exists, the 404 can only be
          // the no-certificate case; if it does not, surface the
          // original error as the not-found state it is.
          try {
            await getProject(projectId);
            setNoCertificate(true);
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
  }, [projectId]);

  useEffect(() => {
    const timer = setTimeout(() => { void load(); }, 0);
    return () => clearTimeout(timer);
  }, [load]);

  return (
    <main className="app-page certificate-page">
      <AppNavigation />
      <ProjectTabs projectId={projectId} />
      <section className="plan-content">
        <Link href={`/projects/${projectId}`} className="editor-back"><ArrowLeft size={14} strokeWidth={2} aria-hidden /> Project</Link>
        <p className="eyebrow">PROVENANCE CERTIFICATE</p>
        <h1>{certificate?.project.title || "Provenance certificate"}</h1>
        <p className="plan-intro">
          Generated from the production run&apos;s decision ledger — the authoritative record of
          what was selected, verified, generated, rotated, replaced, or left missing. It records
          what the system recorded; it is not a licence and not a legal opinion.
        </p>

        {busy && <LoadingState className="plan-message" label="Loading the certificate…" />}
        {!busy && error != null && (
          <ErrorState error={error} onRetry={() => { setBusy(true); void load(); }} />
        )}
        {!busy && noCertificate && (
          <div className="plan-empty">
            <h2>No certificate yet</h2>
            <p>
              This project has no production runs, so no provenance certificate exists.
              A certificate is generated from a run&apos;s decision ledger once a
              production has run.
            </p>
          </div>
        )}

        {certificate && (
          <>
            <div className="plan-summary" aria-label="Certificate summary">
              <div><span>DECISIONS</span><strong>{certificate.summary.total}</strong></div>
              <div><span>RIGHTS RECORDED</span><strong>{certificate.summary.rights_recorded}</strong></div>
              <div><span>RIGHTS UNKNOWN</span><strong>{certificate.summary.rights_unknown}</strong></div>
              {Object.entries(certificate.summary.by_type).map(([type, count]) => (
                <div key={type}><span>{type.replaceAll("_", " ").toUpperCase()}</span><strong>{count}</strong></div>
              ))}
            </div>
            <p className="plan-meta">
              Run {certificate.run.run_id || "—"} · status{" "}
              {certificate.run.status ? <StatusBadge status={certificate.run.status} /> : "—"}
              {certificate.run.created_at ? ` · started ${new Date(certificate.run.created_at).toLocaleString()}` : ""}
              {certificate.run.finished_at ? ` · finished ${new Date(certificate.run.finished_at).toLocaleString()}` : ""}
              {certificate.run.script_hash ? ` · script ${certificate.run.script_hash.slice(0, 12)}…` : ""}
            </p>

            {certificate.quality && (
              <section className="certificate-quality" aria-label="Production quality">
                <h2>Production quality</h2>
                {certificate.quality.state === "recorded" && certificate.quality.verdicts ? (
                  <>
                    <p className="plan-meta">
                      Integrity <VerdictBadge verdict={certificate.quality.verdicts.integrity?.verdict} />
                      {" · "}Quality <VerdictBadge verdict={certificate.quality.verdicts.quality?.verdict} />
                      {" · "}Reliability <VerdictBadge verdict={certificate.quality.verdicts.reliability?.verdict} />
                    </p>
                    {certificate.quality.metrics && (
                      <div className="plan-summary" aria-label="Quality metrics">
                        {certificate.quality.metrics.diversity?.d1_unique_assets != null && (
                          <div><span>UNIQUE VISUALS (D1)</span><strong>{certificate.quality.metrics.diversity.d1_unique_assets}</strong></div>
                        )}
                        {certificate.quality.metrics.diversity?.d1_unique_assets_per_minute != null && (
                          <div><span>D1 PER MINUTE</span><strong>{certificate.quality.metrics.diversity.d1_unique_assets_per_minute.toFixed(2)}</strong></div>
                        )}
                        {certificate.quality.metrics.diversity?.d2_repeated_item_rate != null && (
                          <div><span>REPEATED-ITEM RATE (D2)</span><strong>{certificate.quality.metrics.diversity.d2_repeated_item_rate.toFixed(3)}</strong></div>
                        )}
                        {certificate.quality.metrics.timeline_item_count != null && (
                          <div><span>TIMELINE ITEMS</span><strong>{certificate.quality.metrics.timeline_item_count}</strong></div>
                        )}
                        {certificate.quality.metrics.max_reuse_count != null && (
                          <div><span>MAX REUSE OF ONE VISUAL</span><strong>{certificate.quality.metrics.max_reuse_count}×</strong></div>
                        )}
                      </div>
                    )}
                    {certificate.quality.metrics?.sentence_outcomes && (
                      <p className="plan-meta">
                        Sentence outcomes
                        {certificate.quality.metrics.sentence_outcomes.sentence_count != null
                          ? ` (${certificate.quality.metrics.sentence_outcomes.sentence_count} sentences)`
                          : ""}
                        :{" "}
                        {OUTCOME_LABELS
                          .filter(([key]) => certificate.quality?.metrics?.sentence_outcomes?.buckets?.[key] != null)
                          .map(([key, label]) => `${label} ${certificate.quality?.metrics?.sentence_outcomes?.buckets?.[key]}`)
                          .join(" · ")}
                        {certificate.quality.metrics.sentence_outcomes.reconciles === true
                          ? " Outcome counts reconcile to the sentence total."
                          : certificate.quality.metrics.sentence_outcomes.reconciles === false
                            ? " Outcome counts do NOT reconcile to the sentence total."
                            : ""}
                        {certificate.quality.metrics.sentence_outcomes.sentences_without_verified_visual != null
                          ? ` ${certificate.quality.metrics.sentence_outcomes.sentences_without_verified_visual} sentence(s) had no verified visual.`
                          : ""}
                      </p>
                    )}
                    {(() => {
                      const reasons = [
                        ...(certificate.quality.verdicts.integrity?.failures ?? []),
                        ...(certificate.quality.verdicts.quality?.fail_reasons ?? []),
                        ...(certificate.quality.verdicts.quality?.warning_reasons ?? []),
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
                    {certificate.quality.reconciliation &&
                      (certificate.quality.reconciliation.checks?.length ?? 0) > 0 && (
                        <p className="plan-meta">
                          Cross-artefact reconciliation:{" "}
                          {certificate.quality.reconciliation.all_reconciled
                            ? `all ${certificate.quality.reconciliation.checks?.length} checks matched.`
                            : `${certificate.quality.reconciliation.checks?.filter((check) => check.status === "match").length} of ${certificate.quality.reconciliation.checks?.length} checks matched` +
                              ((certificate.quality.reconciliation.failures?.length ?? 0) > 0
                                ? `; mismatches: ${certificate.quality.reconciliation.failures?.join(", ")}.`
                                : ".")}
                        </p>
                      )}
                  </>
                ) : (
                  <p className="plan-meta">
                    {certificate.quality.state === "unavailable"
                      ? "A production quality report exists for this run but could not be read, so no verdicts are shown."
                      : "No production quality report was recorded for this run — the run predates quality reporting, or its measurement failed and was recorded in the asset report instead. No verdict is shown, and the absence of a report is not a pass."}
                  </p>
                )}
              </section>
            )}

            <div className="plan-table-wrap">
              <table className="plan-table">
                <thead>
                  <tr>
                    <th scope="col">Sentence</th>
                    <th scope="col">Decision</th>
                    <th scope="col">Provider</th>
                    <th scope="col">Source</th>
                    <th scope="col">Interval</th>
                    <th scope="col">Evidence</th>
                    <th scope="col">Verification</th>
                    <th scope="col">Rights</th>
                  </tr>
                </thead>
                <tbody>
                  {certificate.assets.map((asset, index) => (
                    <tr key={`${asset.asset_key || "asset"}-${index}`}>
                      <td>{asset.sentence_number ?? "—"}</td>
                      <td>{asset.decision_type.replaceAll("_", " ")}</td>
                      <td>{asset.provider || "—"}</td>
                      <td>
                        {asset.source_url ? (
                          <a href={asset.source_url} target="_blank" rel="noreferrer">view source</a>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td>
                        {asset.interval.start != null
                          ? `${asset.interval.start}s · ${asset.interval.duration ?? "?"}s`
                          : "—"}
                      </td>
                      <td>{asset.evidence_class || "—"}</td>
                      <td>
                        {asset.verification
                          ? asset.verification.accepted != null
                            ? asset.verification.accepted
                              ? "accepted"
                              : `rejected${asset.verification.reason ? ` (${asset.verification.reason})` : ""}`
                            : "recorded"
                          : "—"}
                      </td>
                      <td>
                        {asset.rights
                          ? `${asset.rights.license}${asset.rights.rights_status ? ` · ${asset.rights.rights_status}` : ""}`
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <section className="certificate-disclaimers" aria-label="Standing disclaimers">
              <h2>Standing disclaimers</h2>
              <ul>
                {certificate.disclaimers.map((disclaimer) => (
                  <li key={disclaimer}>{disclaimer}</li>
                ))}
              </ul>
            </section>
          </>
        )}
      </section>
    </main>
  );
}
