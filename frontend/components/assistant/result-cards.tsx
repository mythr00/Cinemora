"use client";

import Link from "next/link";
import { ExternalLink } from "lucide-react";
import { StatusBadge } from "@/components/status-badge";

// Phase 6 (design assistant/07 §3): cards render ONLY what a tool
// actually returned in this turn — titles, statuses and URLs come
// from the tool payload, never from the model's text. Unknown
// shapes fall back to a plain key/value rendering of the real
// payload rather than being dropped or invented around.

type Dict = Record<string, unknown>;

function asDict(value: unknown): Dict | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Dict)
    : null;
}

function asList(value: unknown): Dict[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => asDict(item) !== null) as Dict[];
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function ProjectRows({ projects }: { projects: Dict[] }) {
  if (projects.length === 0) return null;
  return (
    <ul className="assistant-card-list">
      {projects.map((project, index) => {
        const id = text(project.id);
        return (
          <li key={id || index} className="assistant-card-row">
            <div>
              <strong>{text(project.title) || "Untitled project"}</strong>
              <span className="assistant-card-meta">
                {text(project.category)}
                {project.created_at
                  ? ` · ${new Date(String(project.created_at)).toLocaleDateString()}`
                  : ""}
              </span>
            </div>
            {typeof project.status === "string" && (
              <StatusBadge status={project.status} />
            )}
            {id && (
              <Link className="text-link" href={`/projects/${id}`}>
                Open project
              </Link>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function RunCard({ title, data }: { title: string; data: Dict }) {
  const run = asDict(data.run) || data;
  const status = text(run.status);
  const projectId = text(run.project_id) || text(data.project_id);
  return (
    <div className="assistant-card">
      <p className="assistant-card-title">{title}</p>
      <p className="assistant-card-meta">
        {status && <StatusBadge status={status} />}
        {text(run.stage) ? ` · Stage: ${text(run.stage).replaceAll("_", " ")}` : ""}
        {text(run.run_id) ? ` · Run ${text(run.run_id)}` : ""}
      </p>
      {text(run.error) && (
        <p className="assistant-card-error">{text(run.error)}</p>
      )}
      {projectId && (
        <Link className="text-link" href={`/projects/${projectId}`}>
          Open project
        </Link>
      )}
    </div>
  );
}

function SearchCard({ title, data }: { title: string; data: Dict }) {
  const results = asList(data.results);
  if (results.length === 0) {
    return (
      <div className="assistant-card">
        <p className="assistant-card-title">{title}</p>
        <p className="assistant-card-meta">
          {text(data.note) || "No results were returned for this search."}
        </p>
      </div>
    );
  }
  return (
    <div className="assistant-card">
      <p className="assistant-card-title">{title}</p>
      <ul className="assistant-card-list">
        {results.map((result, index) => {
          const url = text(result.url) || text(result.link);
          return (
            <li key={index} className="assistant-card-row">
              <div>
                <strong>{text(result.title) || url || "Result"}</strong>
                {(text(result.snippet) || text(result.description)) && (
                  <span className="assistant-card-meta">
                    {text(result.snippet) || text(result.description)}
                  </span>
                )}
                {(text(result.channel) || text(result.source)) && (
                  <span className="assistant-card-meta">
                    {text(result.channel) || text(result.source)}
                  </span>
                )}
              </div>
              {url && (
                <a
                  className="text-link"
                  href={url}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open <ExternalLink size={12} strokeWidth={2} aria-hidden />
                </a>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function RecommendationsCard({ data }: { data: Dict }) {
  const items = asList(data.recommendations);
  return (
    <div className="assistant-card">
      <p className="assistant-card-title">Recommendations from the asset report</p>
      {items.length === 0 ? (
        <p className="assistant-card-meta">
          {text(data.note) || "No recommendations were recorded."}
        </p>
      ) : (
        <ul className="assistant-card-list">
          {items.map((item, index) => {
            const evidence = asDict(item.evidence);
            const figures = evidence
              ? Object.entries(evidence)
                  .filter(([, value]) => typeof value === "number")
                  .map(([key, value]) => `${key.replaceAll("_", " ")}: ${value}`)
                  .join(" · ")
              : "";
            return (
              <li key={index} className="assistant-card-row">
                <div>
                  <strong>{text(item.finding) || "Finding"}</strong>
                  {text(item.suggestion) && (
                    <span className="assistant-card-meta">{text(item.suggestion)}</span>
                  )}
                  {figures && <span className="assistant-card-meta">{figures}</span>}
                </div>
                {text(item.link) && (
                  <Link className="text-link" href={text(item.link)}>
                    Review
                  </Link>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function DiagnosisCard({ data }: { data: Dict }) {
  // The evidence pack is flat (run_status / run_stage / run_error),
  // assembled verbatim from the run's recorded failure signals.
  return (
    <div className="assistant-card">
      <p className="assistant-card-title">Production diagnosis</p>
      <p className="assistant-card-meta">
        {text(data.run_status) && <StatusBadge status={text(data.run_status)} />}
        {text(data.run_stage)
          ? ` · Stage: ${text(data.run_stage).replaceAll("_", " ")}`
          : ""}
        {text(data.run_id) ? ` · Run ${text(data.run_id)}` : ""}
      </p>
      {text(data.run_error) && (
        <p className="assistant-card-error">{text(data.run_error)}</p>
      )}
      {text(data.note) && <p className="assistant-card-meta">{text(data.note)}</p>}
      {text(data.project_id) && (
        <Link className="text-link" href={`/projects/${text(data.project_id)}`}>
          Open project
        </Link>
      )}
    </div>
  );
}

const TITLES: Record<string, string> = {
  get_job_status: "Run status",
  get_pipeline_status: "Pipeline status",
  get_render_status: "Render status",
  search_web: "Web results",
  search_youtube: "YouTube results",
  search_research_sources: "Research sources",
  get_project_details: "Project",
};

export function ToolResultCards({
  toolResults,
}: {
  toolResults: Record<string, unknown>;
}) {
  const cards: React.ReactNode[] = [];
  for (const [tool, raw] of Object.entries(toolResults || {})) {
    const data = asDict(raw);
    if (!data) continue;
    if (data.error) continue; // failures are narrated in the answer text
    if (tool === "list_projects" || tool === "search_projects") {
      cards.push(
        <ProjectRows key={tool} projects={asList(data.projects)} />,
      );
    } else if (
      tool === "get_job_status" ||
      tool === "get_pipeline_status" ||
      tool === "get_render_status"
    ) {
      cards.push(<RunCard key={tool} title={TITLES[tool]} data={data} />);
    } else if (
      tool === "search_web" ||
      tool === "search_youtube" ||
      tool === "search_research_sources"
    ) {
      cards.push(<SearchCard key={tool} title={TITLES[tool]} data={data} />);
    } else if (tool === "get_project_recommendations") {
      cards.push(<RecommendationsCard key={tool} data={data} />);
    } else if (tool === "get_production_errors") {
      cards.push(<DiagnosisCard key={tool} data={data} />);
    } else if (tool === "get_project_details") {
      const project = asDict(data.project) || data;
      cards.push(
        <ProjectRows key={tool} projects={[project]} />,
      );
    }
  }
  if (cards.length === 0) return null;
  return <div className="assistant-cards">{cards}</div>;
}
