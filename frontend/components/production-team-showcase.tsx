"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { apiUrl, listProjects, type ProjectSummary } from "@/lib/api";

type Project = ProjectSummary;
const team = ["Voice actor", "Script analysis", "Asset manager", "Motion designer", "Sound designer", "Video editor"];

export function ProductionTeamShowcase() {
  const [project, setProject] = useState<Project | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      listProjects(controller.signal)
        .then((data) => {
          const completed = data?.projects?.find((item) => item.status === "completed" && item.video_url);
          if (completed) setProject(completed);
        })
        .catch(() => undefined);
    }, 0);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, []);

  return <section className="team-showcase" aria-labelledby="team-showcase-title">
    <div className="team-showcase-heading"><p className="eyebrow">ONE CONNECTED PRODUCTION</p><h2 id="team-showcase-title">A complete production team<br /><span>behind every video.</span></h2><p>Each part of the workflow supports the same story, from narration and research to the final edit.</p></div>
    <div className="team-stage">
      {team.map((role, index) => <div className={`team-role team-role-${index + 1}`} key={role}><span className="team-role-index">0{index + 1}</span><span>{role}</span></div>)}
      <div className="team-preview">
        {project?.video_url ? <><video src={apiUrl(project.video_url)} controls playsInline preload="metadata" aria-label={`Completed film preview: ${project.title || project.script_preview || project.category}`} /><div className="team-preview-caption"><span>COMPLETED PROJECT</span><strong>{project.script_preview || project.category}</strong></div></> : <div className="team-preview-empty"><span className="preview-symbol" aria-hidden="true"><i /></span><strong>Your film preview appears here</strong><p>Once a project is completed, you can play it from your library.</p><Link href="/projects">View projects <span aria-hidden="true">↗</span></Link></div>}
      </div>
      <div className="team-stage-note"><span className="team-stage-line" /><span>STORY → RESEARCH → PRODUCTION → REVIEW</span></div>
    </div>
  </section>;
}
