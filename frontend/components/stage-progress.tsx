import { AlertCircle, Check } from "lucide-react";
import type { ProjectStatus } from "@/lib/api";

/**
 * StageProgress (IA-12, audit §H.3): renders the REAL production
 * stages from a project's /status record — the six stages of
 * backend services/production_state.PRODUCTION_STAGES, in run
 * order, with each stage's recorded state (completed / running /
 * failed / skipped / pending). No percentages and no invented
 * progress: a stage the record does not mention reads as pending
 * unless it is the record's current stage.
 */
export const PRODUCTION_STAGE_ORDER = [
  ["voice_actor", "Narration"],
  ["script_analysis", "Story analysis"],
  ["asset_manager", "Visual research"],
  ["motion_designer", "Motion design"],
  ["sound_designer", "Sound mix"],
  ["video_editor", "Final edit"],
] as const;

export function StageProgress({
  stages,
  currentStage,
}: {
  stages: ProjectStatus["stages"];
  currentStage?: string | null;
}) {
  return (
    <div className="stage-progress">
      <p className="stage-progress-heading">PRODUCTION STAGES</p>
      <div className="stage-list">
        {PRODUCTION_STAGE_ORDER.map(([key, label]) => {
          const stageStatus =
            stages?.[key]?.status || (currentStage === key ? "running" : "pending");
          return (
            <div className={`stage-row ${stageStatus}`} key={key}>
              <span className="stage-check">
                {stageStatus === "completed" ? (
                  <Check size={9} strokeWidth={3} aria-hidden />
                ) : stageStatus === "running" ? (
                  <span />
                ) : stageStatus === "failed" ? (
                  <AlertCircle size={9} strokeWidth={2.5} aria-hidden />
                ) : (
                  ""
                )}
              </span>
              <span>{label}</span>
              {stageStatus === "running" && <small>NOW</small>}
              {stageStatus === "skipped" && <small>SKIPPED</small>}
              {stageStatus === "failed" && <small>FAILED</small>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
