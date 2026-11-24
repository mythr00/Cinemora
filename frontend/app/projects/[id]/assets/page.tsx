"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { ProjectTabs } from "@/components/project-tabs";
import { MediaThumb } from "@/components/media-thumb";
import { ErrorState, LoadingState } from "@/components/states";
import { Film } from "lucide-react";
import {
  getCertificate,
  listAssets,
  mediaKeyForProjectPath,
  type AssetDecisionRow,
  type AssetsPage,
  type CertificateQuality,
} from "@/lib/api";

/**
 * Asset review (redesign F2, plan §3.6) — the paged asset
 * decision ledger (listAssets), presented as a review surface.
 * This is the same capability that used to sit at the bottom of
 * the project overview (Brief Stage 5, C-07); the data, paging
 * and honesty rules are unchanged:
 *
 * - The ledger is append-only evidence. There are deliberately
 *   NO approval/rejection controls here — the backend has no
 *   override action, and the UI must not invent one. The real
 *   intervention point is plan-level replacement (Visual Plan
 *   tab).
 * - A licence recorded as "unknown" stays "unknown" — never
 *   presented as cleared.
 * - The coverage header reads the certificate's recorded
 *   sentence-outcome accounting (visual_quality buckets). When
 *   no report was recorded, the section names that state
 *   (not_recorded / unavailable) instead of estimating coverage
 *   from the ledger page in view.
 * - The diagnostics funnel section is ABSENT until the R5
 *   diagnostics artefact exists (§3.6) — not greyed out, not
 *   mocked.
 * - Row thumbnails: a decision's asset_key is heterogeneous
 *   (candidate id, content hash, or file path). A preview is
 *   derived only when the key is a path under the project (the
 *   backend's documented media-key derivation); otherwise the
 *   row shows the neutral "No preview" tile — never a fake
 *   thumbnail.
 */

const ASSETS_PAGE_SIZE = 50;

function verificationLabel(verification: AssetDecisionRow["verification"]): string {
  if (!verification) return "—";
  if (verification.accepted === true) return "accepted";
  if (verification.accepted === false) {
    return `rejected${verification.reason ? ` (${String(verification.reason)})` : ""}`;
  }
  if (typeof verification.reason === "string" && verification.reason) return verification.reason;
  return "recorded";
}

function rightsLabel(rights: AssetDecisionRow["rights"]): string {
  if (!rights) return "—";
  const license = typeof rights.license === "string" && rights.license ? rights.license : "unknown";
  const status = typeof rights.rights_status === "string" && rights.rights_status ? rights.rights_status : null;
  return status ? `${license} · ${status}` : license;
}

function LedgerThumb({ projectId, row }: { projectId: string; row: AssetDecisionRow }) {
  const [mediaKey, setMediaKey] = useState<string | null | undefined>(undefined);
  useEffect(() => {
    let cancelled = false;
    const key = row.asset_key;
    if (!key || !key.includes("/")) {
      setMediaKey(null);
      return;
    }
    void mediaKeyForProjectPath(projectId, key).then((derived) => {
      if (!cancelled) setMediaKey(derived);
    });
    return () => { cancelled = true; };
  }, [projectId, row.asset_key]);

  if (mediaKey) return <MediaThumb projectId={projectId} mediaKey={mediaKey} alt={`Preview for sentence ${row.sentence_number ?? "—"}`} />;
  return (
    <span className="media-thumb media-thumb-fallback" role="img" aria-label="No preview recorded for this decision">
      <Film size={16} strokeWidth={1.8} aria-hidden />
      <small>No preview</small>
    </span>
  );
}

// The recorded sentence-outcome buckets (backend
// services/visual_quality.py): every sentence of the production
// lands in exactly one bucket, and the buckets reconcile to the
// sentence count.
const COVERAGE_SEGMENTS = [
  { key: "direct", label: "Direct visual", className: "coverage-seg-footage" },
  { key: "approved_graphic", label: "Approved graphic", className: "coverage-seg-graphic" },
  { key: "contextual", label: "Contextual (rotation)", className: "coverage-seg-still" },
  { key: "editorial_holdover", label: "Editorial holdover", className: "coverage-seg-still" },
  { key: "uncovered", label: "Uncovered", className: "coverage-seg-uncovered" },
] as const;

function CoverageHeader({ quality }: { quality: CertificateQuality | null }) {
  const outcomes = quality?.metrics?.sentence_outcomes;
  const buckets = outcomes?.buckets || {};
  const total = outcomes?.sentence_count ?? null;
  if (!quality || quality.state !== "recorded" || !outcomes || !total) {
    return (
      <div className="not-recorded" role="status">
        <strong>Coverage not recorded.</strong>{" "}
        {quality
          ? `The production's quality report state is “${quality.state}”, so no sentence-coverage accounting exists to show. Coverage is never estimated from the ledger page in view.`
          : "No quality report is available for this production yet, so no sentence-coverage accounting exists to show. Coverage is never estimated from the ledger page in view."}
      </div>
    );
  }
  return (
    <div className="coverage-meter">
      <div className="coverage-meter-bar" role="img" aria-label={`Sentence coverage across ${total} sentences`}>
        {COVERAGE_SEGMENTS.map(({ key, className }) => {
          const count = buckets[key] || 0;
          if (!count) return null;
          return <span key={key} className={className} style={{ width: `${(count / total) * 100}%` }} />;
        })}
      </div>
      <ul className="coverage-meter-legend">
        {COVERAGE_SEGMENTS.map(({ key, label }) => (
          <li key={key}>{label}: <b>{buckets[key] || 0}</b> of {total}</li>
        ))}
        {outcomes.reconciles === false && <li>Buckets do not reconcile to the sentence count — see the Delivery tab.</li>}
      </ul>
    </div>
  );
}

export default function ProjectAssetsPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [assets, setAssets] = useState<AssetsPage | null>(null);
  const [assetsBusy, setAssetsBusy] = useState(true);
  const [assetsError, setAssetsError] = useState<unknown>(null);
  const [quality, setQuality] = useState<CertificateQuality | null>(null);
  const [decisionFilter, setDecisionFilter] = useState("all");
  const [providerFilter, setProviderFilter] = useState("all");

  const loadAssets = useCallback(async (offset: number) => {
    setAssetsBusy(true);
    setAssetsError(null);
    try {
      setAssets(await listAssets(projectId, ASSETS_PAGE_SIZE, offset));
    } catch (cause) {
      setAssetsError(cause);
    } finally {
      setAssetsBusy(false);
    }
  }, [projectId]);

  useEffect(() => {
    const timer = setTimeout(() => { void loadAssets(0); }, 0);
    return () => clearTimeout(timer);
  }, [loadAssets]);

  // Coverage comes from the certificate's recorded quality
  // section. A missing certificate (no runs yet) is a
  // not-recorded state, not an error on this surface.
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      getCertificate(projectId, controller.signal)
        .then((certificate) => setQuality(certificate.quality ?? null))
        .catch(() => undefined);
    }, 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [projectId]);

  const assetsOffset = assets?.offset ?? 0;
  const assetsTotal = assets?.total ?? 0;
  const pageRows = useMemo(() => assets?.items ?? [], [assets]);
  const decisionOptions = useMemo(
    () => [...new Set(pageRows.map((row) => row.decision_type))].sort(),
    [pageRows],
  );
  const providerOptions = useMemo(
    () => [...new Set(pageRows.map((row) => row.provider).filter((value): value is string => !!value))].sort(),
    [pageRows],
  );
  const visibleRows = pageRows.filter(
    (row) =>
      (decisionFilter === "all" || row.decision_type === decisionFilter) &&
      (providerFilter === "all" || row.provider === providerFilter),
  );

  return <main className="app-page"><AppNavigation /><ProjectTabs projectId={projectId} /><section className="plan-content">
    <p className="eyebrow">ASSET REVIEW</p>
    <h1>Asset decisions</h1>
    <p className="asset-evidence-intro">
      The latest run&apos;s decision ledger — a licence recorded as <strong>unknown</strong> is not cleared for publication.
    </p>

    <h2>Sentence coverage</h2>
    <CoverageHeader quality={quality} />

    <h2>Decision ledger</h2>
    {assetsBusy && <LoadingState className="projects-state" label="Loading asset decisions…" />}
    {!assetsBusy && assetsError != null && <ErrorState error={assetsError} onRetry={() => void loadAssets(assetsOffset)} />}
    {!assetsBusy && !assetsError && assets && assets.total === 0 && (
      <p className="asset-evidence-empty">
        No asset decisions are recorded for this project yet. The ledger fills as a
        production run selects and verifies its visuals.
      </p>
    )}
    {!assetsBusy && !assetsError && assets && assets.total > 0 && <>
      <div className="assets-filters">
        <label>Decision
          <select value={decisionFilter} onChange={(event) => setDecisionFilter(event.target.value)} aria-label="Filter this page by decision">
            <option value="all">All decisions on this page</option>
            {decisionOptions.map((option) => <option key={option} value={option}>{option.replaceAll("_", " ")}</option>)}
          </select>
        </label>
        <label>Provider
          <select value={providerFilter} onChange={(event) => setProviderFilter(event.target.value)} aria-label="Filter this page by provider">
            <option value="all">All providers on this page</option>
            {providerOptions.map((option) => <option key={option} value={option}>{option}</option>)}
          </select>
        </label>
        <span>Filters apply to the loaded page of the ledger.</span>
      </div>
      {visibleRows.length === 0 ? (
        <p className="asset-evidence-empty">No decisions on this page match the current filters.</p>
      ) : (
        <div className="plan-table-wrap">
          <table className="plan-table">
            <thead><tr>
              <th scope="col">Preview</th>
              <th scope="col">Sentence</th>
              <th scope="col">Scene</th>
              <th scope="col">Decision</th>
              <th scope="col">Asset</th>
              <th scope="col">Provider / source</th>
              <th scope="col">Evidence</th>
              <th scope="col">Verification</th>
              <th scope="col">Rights</th>
              <th scope="col">Recorded</th>
            </tr></thead>
            <tbody>
              {visibleRows.map((row) => (
                <tr key={row.id}>
                  <td><LedgerThumb projectId={projectId} row={row} /></td>
                  <td>{row.sentence_number ?? "—"}</td>
                  <td>{row.scene_number ?? "—"}</td>
                  <td>{row.decision_type.replaceAll("_", " ")}</td>
                  <td>{row.asset_key ? <code title={row.asset_key}>{row.asset_key.slice(0, 16)}…</code> : "—"}</td>
                  <td>
                    {row.provider || "—"}
                    {row.source_url ? <> · <a href={row.source_url} target="_blank" rel="noreferrer">view source</a></> : null}
                  </td>
                  <td>{row.evidence_class || "—"}</td>
                  <td>{verificationLabel(row.verification)}</td>
                  <td>{rightsLabel(row.rights)}</td>
                  <td>{row.created_at ? new Date(row.created_at).toLocaleString() : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="asset-evidence-pager">
        <span>Showing {assetsOffset + 1}–{assetsOffset + assets.items.length} of {assetsTotal} decisions{assets.run_id ? <> for run <code>{assets.run_id.slice(0, 8)}…</code></> : null}</span>
        <button type="button" disabled={assetsOffset === 0} onClick={() => void loadAssets(Math.max(0, assetsOffset - ASSETS_PAGE_SIZE))}>Previous</button>
        <button type="button" disabled={assetsOffset + assets.items.length >= assetsTotal} onClick={() => void loadAssets(assetsOffset + ASSETS_PAGE_SIZE)}>Next</button>
      </p>
    </>}
  </section></main>;
}
