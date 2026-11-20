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
