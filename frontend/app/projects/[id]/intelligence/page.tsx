"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { StatusBadge } from "@/components/status-badge";
import { ProjectTabs } from "@/components/project-tabs";
import { VerdictChip } from "@/components/verdict-chip";
import { ErrorState, LoadingState } from "@/components/states";
import { ArrowLeft } from "lucide-react";
import {
  getProjectIntelligence,
  type IntelligenceFinding,
  type ProjectIntelligence,
} from "@/lib/api";

/**
 * Project Intelligence (founder priority #10) — the project's
 * persistent record assembled into one view: what the system
 * recorded about this film (runs, Case Bible, research), the
 * latest production's recorded QA verdicts, the entity
 * connections the records support, and deterministic
 * recommendations that carry the figures they rest on. Recorded
 * data is shown as recorded; anything derived (entities shared
 * with your other projects, entities mentioned in timeline
 * events) is printed with its derivation. Nothing here is a
 * score, a trend or a prediction.
 */

function Evidence({ evidence }: { evidence: Record<string, unknown> }) {
  const entries = Object.entries(evidence).filter(
    ([key, value]) => key !== "derivation" && value != null,
  );
  if (!entries.length) return null;
  return (
    <ul>
      {entries.map(([key, value]) => (
        <li key={key}>
          <strong>{key.replace(/_/g, " ")}</strong>:{" "}
          {typeof value === "object" ? JSON.stringify(value) : String(value)}
        </li>
      ))}
    </ul>
  );
}

function Finding({ finding }: { finding: IntelligenceFinding }) {
  return (
    <li>
      <strong>{finding.finding}</strong>
      <p className="plan-meta">{finding.suggestion}</p>
      <Evidence evidence={finding.evidence} />
      {typeof finding.evidence.derivation === "string" && (
        <p className="plan-meta">{finding.evidence.derivation}</p>
      )}
      {finding.link && <Link href={finding.link}>Open the relevant surface</Link>}
    </li>
  );
}

export default function ProjectIntelligencePage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [view, setView] = useState<ProjectIntelligence | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setView(await getProjectIntelligence(projectId));
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }, [projectId]);

  useEffect(() => {
    const timer = setTimeout(() => { void load(); }, 0);
    return () => clearTimeout(timer);
  }, [load]);

  return (
    <main className="app-page intelligence-page">
      <AppNavigation />
      <ProjectTabs projectId={projectId} />
      <section className="plan-content">
        <Link href={`/projects/${projectId}`} className="editor-back">
          <ArrowLeft size={14} strokeWidth={2} aria-hidden /> Project
        </Link>
        <p className="eyebrow">PROJECT INTELLIGENCE</p>
        <h1>Intelligence</h1>
        <p className="plan-intro">
          What Cinemora has recorded about this project, assembled — nothing here is estimated.
        </p>

        {busy && <LoadingState className="plan-message" label="Assembling the project record…" />}
        {!busy && error != null && (
          <ErrorState
            error={error}
            onRetry={() => {
              setBusy(true);
              void load();
            }}
          />
        )}

        {!busy && !error && view && (
          <>
            <section>
              <h2>The record</h2>
              <ul>
                <li>
                  <strong>Production runs</strong>: {view.record.runs.production} production,{" "}
                  {view.record.runs.total} total
                  {Object.entries(view.record.runs.by_status).map(([status, count]) => (
                    <span key={status}>
                      {" · "}
                      {count} {status}
                    </span>
                  ))}
                </li>
                <li>
                  <strong>Case Bible</strong>:{" "}
                  {view.record.bible_recorded
                    ? `recorded${view.record.story_type ? ` (${view.record.story_type})` : ""}, ${view.entities.length} entities`
                    : "not recorded yet — story analysis during plan generation or a production run produces it"}
                </li>
                {view.record.research_counts && (
                  <li>
                    <strong>Research</strong>: {view.record.research_counts.sessions ?? 0} sessions,{" "}
                    {view.record.research_counts.sources ?? 0} sources (
                    {view.record.research_counts.sources_kept ?? 0} kept),{" "}
                    {view.record.research_counts.citations ?? 0} citations
                  </li>
                )}
                <li>
                  <strong>Latest production quality</strong>:{" "}
                  {view.quality.state === "recorded" ? (
                    <>
                      <VerdictChip label="Integrity" verdict={view.quality.integrity} />{" "}
                      <VerdictChip label="Quality" verdict={view.quality.quality} />{" "}
                      <VerdictChip label="Reliability" verdict={view.quality.reliability} />{" "}
                      — full detail on the{" "}
                      <Link href={`/projects/${projectId}/qa`}>QA Report tab</Link>
                    </>
                  ) : (
                    "not recorded for the latest production run"
                  )}
                </li>
              </ul>
            </section>

            <section>
              <h2>Entities</h2>
              {!view.record.bible_recorded && (
                <p className="plan-meta">
                  No Case Bible is recorded for this project, so no entities are known.
                </p>
              )}
              {view.entities.length > 0 && (
                <ul>
                  {view.entities.map((entity) => (
                    <li key={entity.entity_id || entity.canonical || ""}>
                      <strong>{entity.effective_canonical || entity.canonical}</strong>
                      {entity.effective_type ? ` — ${entity.effective_type}` : ""}
                      {entity.is_primary_subject && (
                        <>
                          {" "}
                          <StatusBadge status="primary subject" />
                        </>
                      )}
                      {entity.role ? <p className="plan-meta">{entity.role}</p> : null}
                      {entity.corrections > 0 && (
                        <p className="plan-meta">
                          {entity.corrections} user correction(s) recorded — the recorded
                          name/type and the correction are both kept (Research tab).
                        </p>
                      )}
                      {entity.kb_check?.conflict && (
                        <p className="plan-meta">
                          Knowledge-base check recorded a type conflict: the bible records{" "}
                          {entity.type || "no type"}, the knowledge base records{" "}
                          {entity.kb_check.kb_type || "no type"}. Both sides are shown in
                          the Research tab; the conflict is never adjudicated here.
                        </p>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section>
              <h2>Connections</h2>
              <h3>Shared with your other projects</h3>
              {view.connections.shared_entities.length === 0 && (
                <p className="plan-meta">
                  No entity in this project&apos;s Case Bible is recorded in any of your
                  other projects&apos; Case Bibles under the same name and type.
                </p>
              )}
              <ul>
                {view.connections.shared_entities.map((shared) => (
                  <li key={`${shared.canonical}-${shared.type}`}>
                    <strong>{shared.canonical}</strong>
                    {shared.type ? ` (${shared.type})` : ""} — recorded in{" "}
                    {shared.project_count} of your projects:{" "}
                    {(shared.other_projects || []).map((other) => (
                      <span key={other.project_id}>
                        <Link href={`/projects/${other.project_id}/intelligence`}>
                          {other.project_title || other.project_id}
                        </Link>{" "}
                      </span>
                    ))}
                    <p className="plan-meta">{shared.derivation}</p>
                  </li>
                ))}
              </ul>

              <h3>Entities in the recorded timeline</h3>
              {view.connections.timeline_mentions.length === 0 && (
                <p className="plan-meta">
                  No bible timeline event&apos;s recorded text mentions an entity name or
                  alias.
                </p>
              )}
              <ul>
                {view.connections.timeline_mentions.map((mention) => (
                  <li key={mention.entity_id || mention.canonical || ""}>
                    <strong>{mention.canonical}</strong>
                    <ul>
                      {mention.events.map((event, index) => (
                        <li key={index}>
                          {event.when ? `${event.when}: ` : ""}
                          {event.what}
                          {event.matched_name ? (
                            <span className="plan-meta"> (matched on “{event.matched_name}”)</span>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                    <p className="plan-meta">{mention.derivation}</p>
                  </li>
                ))}
              </ul>
            </section>

            <section>
              <h2>Recommendations</h2>
              {view.recommendations.findings_total === 0 && (
                <p className="plan-meta">
                  No rule found recorded evidence for a recommendation on this project.
                  That is a finding in itself — the rules below only speak when the
                  records do.
                </p>
              )}
              {view.recommendations.rules.map((rule) => (
                <div key={rule.rule}>
                  {rule.findings.length > 0 && (
                    <ul>
                      {rule.findings.map((finding) => (
                        <Finding key={finding.finding} finding={finding} />
                      ))}
                    </ul>
                  )}
                  {rule.state !== "recorded" && (
                    <p className="plan-meta">
                      {rule.rule.replace(/_/g, " ")}: {rule.note}
                    </p>
                  )}
                </div>
              ))}
            </section>
          </>
        )}
      </section>
    </main>
  );
}
