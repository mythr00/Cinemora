"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { VerdictChip } from "@/components/verdict-chip";
import { ErrorState, LoadingState } from "@/components/states";
import {
  getAccountIntelligence,
  type AccountIntelligence,
} from "@/lib/api";

/**
 * Account Intelligence (founder priority #10) — the patterns
 * across your own catalogue, computed only from recorded data:
 * projects by category, the latest productions' recorded quality
 * verdicts grouped by category, the provider funnels your
 * productions recorded (who actually delivered verified assets,
 * and how much failed), and the entities your Case Bibles share
 * across projects. Every figure is a sum or a count of records
 * and can be recounted from them; derivations are printed with
 * the tables they describe. Only your own projects are read —
 * nothing of another user's enters even a count.
 */
export default function AccountIntelligencePage() {
  const [view, setView] = useState<AccountIntelligence | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setView(await getAccountIntelligence());
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => { void load(); }, 0);
    return () => clearTimeout(timer);
  }, [load]);

  return (
    <main className="app-page intelligence-page">
      <AppNavigation />
      <section className="plan-content">
        <p className="eyebrow">PROJECT INTELLIGENCE</p>
        <h1>Intelligence</h1>
        <p className="plan-intro">
          Patterns across your productions, computed from the records themselves:
          what you have made by category, how the latest productions scored on
          their recorded quality verdicts, which sources actually delivered
          verified assets, and which entities connect your films. Every number
          is a count of records — nothing is estimated, trended or predicted.
        </p>

        {busy && <LoadingState className="plan-message" label="Assembling your catalogue…" />}
        {!busy && error != null && (
          <ErrorState
            error={error}
            onRetry={() => {
              setBusy(true);
              void load();
            }}
          />
        )}

        {!busy && !error && view && view.totals.projects === 0 && (
          <div className="plan-empty">
            <h2>No projects yet</h2>
            <p className="plan-meta">
              Intelligence is assembled from your projects&apos; recorded bibles,
              runs and reports. Once a production exists, its patterns appear here.
            </p>
          </div>
        )}

        {!busy && !error && view && view.totals.projects > 0 && (
          <>
            <section>
              <h2>Catalogue</h2>
              <ul>
                <li>
                  <strong>{view.totals.projects}</strong> projects,{" "}
                  <strong>{view.totals.runs}</strong> production runs,{" "}
                  <strong>{view.totals.projects_with_bible}</strong> with a recorded
                  Case Bible, <strong>{view.totals.distinct_entities}</strong> distinct
                  entities, <strong>{view.totals.shared_entities}</strong> shared
                  across projects
                </li>
                {view.projects_by_category.map((row) => (
                  <li key={row.category}>
                    <strong>{row.category}</strong>: {row.projects} project
                    {row.projects === 1 ? "" : "s"}
                  </li>
                ))}
              </ul>
            </section>

            <section>
              <h2>Quality outcomes by category</h2>
              <p className="plan-meta">
                The recorded quality verdict of each project&apos;s latest production
                run, grouped by the project&apos;s category. A project whose latest
                run recorded no verdict is counted as a project, never as a pass.
              </p>
              <ul>
                {view.quality_by_category.map((row) => (
                  <li key={row.category}>
                    <strong>{row.category}</strong>: {row.projects} projects,{" "}
                    {row.with_recorded_verdict} with a recorded verdict
                    {Object.entries(row.quality_verdicts).map(([verdict, count]) => (
                      <span key={verdict}>
                        {" · "}
                        {count} × <VerdictChip label="Quality" verdict={verdict} />
                      </span>
                    ))}
                  </li>
                ))}
              </ul>
            </section>

            <section>
              <h2>Sources: delivered vs failed</h2>
              {view.providers.state !== "recorded" && (
                <p className="plan-meta">
                  No production run has recorded a provider funnel yet, so there is
                  nothing to aggregate.
                </p>
              )}
              {view.providers.state === "recorded" && (
                <>
                  <div className="plan-table-wrap">
                    <table className="plan-table">
                    <thead>
                      <tr>
                        <th>Source (as recorded)</th>
                        <th>Discovered</th>
                        <th>Verified</th>
                        <th>Rejected</th>
                        <th>Downloaded</th>
                        <th>Download failed</th>
                        <th>Projects</th>
                      </tr>
                    </thead>
                    <tbody>
                      {view.providers.providers.map((provider) => (
                        <tr key={provider.provider}>
                          <td>{provider.provider}</td>
                          <td>{provider.discovered}</td>
                          <td>{provider.verified}</td>
                          <td>{provider.rejected}</td>
                          <td>{provider.downloaded}</td>
                          <td>{provider.download_failed}</td>
                          <td>{provider.project_count}</td>
                        </tr>
                      ))}
                    </tbody>
                    </table>
                  </div>
                  <p className="plan-meta">
                    Base: the latest production run of {view.providers.projects_with_funnel}{" "}
                    project(s). {view.providers.derivation}
                  </p>
                </>
              )}
            </section>

            <section>
              <h2>Entities across your projects</h2>
              {view.entity_connections.length === 0 && (
                <p className="plan-meta">
                  No entity is recorded in two or more of your projects&apos; Case
                  Bibles under the same name and type.
                </p>
              )}
              <ul>
                {view.entity_connections.map((shared) => (
                  <li key={`${shared.canonical}-${shared.type}`}>
                    <strong>{shared.canonical}</strong>
                    {shared.type ? ` (${shared.type})` : ""} — in {shared.project_count}{" "}
                    projects:{" "}
                    {(shared.appearances || []).map((appearance) => {
                      const projectId = String(appearance.project_id || "");
                      const title = String(appearance.project_title || projectId);
                      return (
                        <span key={projectId}>
                          <Link href={`/projects/${projectId}/intelligence`}>{title}</Link>{" "}
                        </span>
                      );
                    })}
                    <p className="plan-meta">{shared.derivation}</p>
                  </li>
                ))}
              </ul>
            </section>

            <section>
              <h2>Projects</h2>
              <ul>
                {view.projects.map((project) => (
                  <li key={project.project_id}>
                    <Link href={`/projects/${project.project_id}/intelligence`}>
                      <strong>{project.title || project.project_id}</strong>
                    </Link>{" "}
                    — {project.category || "Documentary"} · {project.run_count} runs ·{" "}
                    {project.bible_recorded
                      ? `${project.entity_count} entities`
                      : "no Case Bible recorded"}
                    {project.quality?.state === "recorded" && (
                      <>
                        {" · "}
                        <VerdictChip label="Quality" verdict={project.quality.quality} />
                      </>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          </>
        )}
      </section>
    </main>
  );
}
