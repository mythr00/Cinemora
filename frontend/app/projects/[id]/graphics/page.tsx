"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { AdminOnlyGate } from "@/components/admin-only-gate";
import { ProjectTabs } from "@/components/project-tabs";
import { ItemThumb } from "@/components/editor/item-thumb";
import { ClassificationChip } from "@/components/editor/classification-chip";
import { ErrorState, LoadingState, classifyApiError } from "@/components/states";
import {
  getEditor,
  type EditorDocument,
  type VisualItem,
} from "@/lib/api";

/**
 * Graphics review (redesign F2, plan §3.8 — phased honesty).
 *
 * Phase 1 data is what the API actually returns today: the
 * current revision's editor document (getEditor). Every visual
 * item of kind "graphic" on its timeline is listed with its
 * recorded template/family, its MG-03 regeneration recipe, its
 * classification and timing. An item's presence on the timeline
 * is itself the delivery fact: production attaches a graphic
 * only once it is delivered and validated (MG-05/MG-07).
 *
 * Fact basis (MG-09) is shown ONLY when the record carries it:
 * the stamp vocabulary is the backend's — VERIFIED is claimed
 * solely for research-supported / verified-asset values WITH a
 * recorded evidence reference; anything else with a recorded
 * basis reads SCRIPT CLAIM (the backend's own downgrade rule);
 * no recorded basis reads "not recorded". Nothing is inferred.
 *
 * Not shown, deliberately: graphics that were generated but
 * never delivered, and their closed-vocabulary skip reasons —
 * no endpoint exposes them to this surface yet (they are
 * recorded in the production's asset report, downloadable from
 * the Delivery tab). The graphics-manifest review (§3.8 phase
 * 2) renders only when the Phase 3 manifest endpoint exists.
 * Regeneration stays in the Editor; this tab is review only.
 */

type FactBasis = { label: string; tone: "verified" | "claim" | "none" };

function factBasisFor(item: VisualItem): FactBasis {
  const graphic = item.graphic;
  const sources: Array<Record<string, unknown> | null | undefined> = [
    graphic?.recipe?.inputs,
    graphic?.inputs,
    graphic?.engine_metadata,
  ];
  let basis: unknown = null;
  let evidenceRef: unknown = null;
  for (const source of sources) {
    if (!source) continue;
    if (basis === null && typeof source.fact_basis === "string") basis = source.fact_basis;
    if (evidenceRef === null && source.evidence_ref) evidenceRef = source.evidence_ref;
  }
  const references = graphic?.recipe?.source_references;
  const hasEvidence = Boolean(evidenceRef) || (Array.isArray(references) && references.length > 0);
  if (basis === "research_supported" && hasEvidence) {
    return { label: "Fact basis: VERIFIED (research-supported)", tone: "verified" };
  }
  if (basis === "verified_asset_metadata" && hasEvidence) {
    return { label: "Fact basis: VERIFIED (asset metadata)", tone: "verified" };
  }
  if (basis === "script_claim" || basis === "research_supported" || basis === "verified_asset_metadata") {
    // A verified-type basis without a recorded evidence
    // reference is downgraded — the backend's MG-09 rule.
    return { label: "Fact basis: SCRIPT CLAIM", tone: "claim" };
  }
  return { label: "Fact basis: not recorded", tone: "none" };
}

function formatTime(seconds: number) {
  return `${seconds.toFixed(1)}s`;
}

function ProjectGraphicsPageContent() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [document, setDocument] = useState<EditorDocument | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setError(null);
    try {
      const data = await getEditor(projectId, signal);
      if (!signal?.aborted) setDocument(data.document);
    } catch (cause) {
      if (!signal?.aborted) setError(cause);
    } finally {
      if (!signal?.aborted) setBusy(false);
    }
  }, [projectId]);

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => { void load(controller.signal); }, 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [load]);

  const graphics = (document?.tracks.visual ?? []).filter((item) => item.kind === "graphic");
  const errorKind = error ? classifyApiError(error).kind : null;

  return <main className="app-page"><AppNavigation /><ProjectTabs projectId={projectId} /><section className="plan-content">
    <p className="eyebrow">GRAPHICS REVIEW</p>
    <h1>Motion graphics</h1>
    <p className="asset-evidence-intro">
      The graphics in the cut, each with its recorded template and recipe. Undelivered graphics live in the asset report on the{" "}
      <Link className="text-link" href={`/projects/${projectId}/certificate`}>Delivery tab</Link>;
      regenerate from the{" "}
      <Link className="text-link" href={`/projects/${projectId}/edit`}>Editor</Link>.
    </p>

    {busy && <LoadingState className="projects-state" label="Loading graphics…" />}
    {!busy && error != null && errorKind !== "not-found" && (
      <ErrorState error={error} onRetry={() => { setBusy(true); void load(); }} />
    )}
    {!busy && (errorKind === "not-found" || (!error && graphics.length === 0)) && (
      <div className="not-recorded" role="status">
        <strong>No graphics recorded.</strong>{" "}
        {errorKind === "not-found"
          ? "This project has no editor document yet — graphics are recorded when a production completes and its timeline is built."
          : "The current revision's timeline carries no graphic items. When the production generates and delivers a motion graphic, it is listed here with its recorded recipe."}
      </div>
    )}
    {!busy && !error && graphics.length > 0 && <>
      <p className="plan-meta">{graphics.length} graphic{graphics.length === 1 ? "" : "s"} on Revision {document?.revision_number}.</p>
      <div className="graphics-list">
        {graphics.map((item) => {
          const basis = factBasisFor(item);
          const recipe = item.graphic?.recipe;
          return (
            <article className="graphics-card" key={item.item_id}>
              <ItemThumb projectId={projectId} filePath={item.asset?.file_path ?? null} mediaType={item.asset?.type || "video"} label={item.graphic?.template || "graphic"} />
              <div>
                <p className="overview-run-line">
                  <strong>{item.graphic?.template || "Graphic"}</strong>
                  <ClassificationChip classification={item.classification} />
                  <span className={`fact-basis fact-basis--${basis.tone}`}>{basis.label}</span>
                </p>
                <dl className="graphics-meta">
                  <div><dt>Family</dt><dd>{item.graphic?.family || "—"}</dd></div>
                  <div><dt>Style</dt><dd>{item.graphic?.style || "—"}</dd></div>
                  <div><dt>Sentence</dt><dd>{item.sentence_number} · scene {item.scene_number}</dd></div>
                  <div><dt>Timeline</dt><dd>{formatTime(item.start)} – {formatTime(item.end)} ({formatTime(item.duration)})</dd></div>
                  <div><dt>Assignment</dt><dd>{item.assignment ? item.assignment.replaceAll("_", " ") : "—"}</dd></div>
                  <div><dt>Recipe</dt><dd>{recipe ? (recipe.regenerable === false ? `Not regenerable${recipe.non_regenerable_reason ? ` — ${recipe.non_regenerable_reason}` : ""}` : "Recorded — regenerable in the Editor") : "Not recorded (pre-recipe production)"}</dd></div>
                  {recipe?.renderer && <div><dt>Renderer</dt><dd>{recipe.renderer}</dd></div>}
                  {recipe?.purpose && <div><dt>Purpose</dt><dd>{recipe.purpose}</dd></div>}
                  {item.flags.length > 0 && <div><dt>Flags</dt><dd>{item.flags.join(", ")}</dd></div>}
                </dl>
              </div>
            </article>
          );
        })}
      </div>
    </>}
  </section></main>;
}

/**
 * FS-13: the Graphics review tab is an admin-only presentational surface (FS-C). The editor's own graphics features are unaffected — this page only reads the shared editor document.
 * The page renders only behind AdminOnlyGate (role from
 * /auth/me, the AdminShell pattern); a non-admin direct URL gets
 * the plain not-found view and the content never mounts. The
 * backend endpoints enforce the same rule server-side.
 */
export default function ProjectGraphicsPage() {
  return (
    <AdminOnlyGate>
      <ProjectGraphicsPageContent />
    </AdminOnlyGate>
  );
}
