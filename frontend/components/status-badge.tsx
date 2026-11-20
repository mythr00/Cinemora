import type { LucideIcon } from "lucide-react";
import {
  Ban,
  BellRing,
  CircleCheck,
  CircleDashed,
  CircleDot,
  CircleExclamationPoint,
  Clapperboard,
  Clock3,
  Download,
  FilePen,
  LoaderCircle,
} from "lucide-react";

/**
 * The shared status system (Brief Stage 4, C-12).
 *
 * One component renders every project / run / revision status in
 * the studio: a text label plus a Lucide icon plus a colour, never
 * colour alone. Before this component each page improvised — the
 * dashboard and library used a `.project-state` pill whose
 * completed/failed colours measured 1.62:1 and 1.92:1 on white,
 * and the overview, plan and certificate pages printed raw status
 * strings as plain text.
 *
 * The vocabulary below is the backend's, read from source — not
 * invented here:
 * - Project / run statuses: `created` (api_v1 project creation),
 *   `queued`, `running`, `awaiting_approval`, `completed`,
 *   `failed`, `cancelled` (jobs.py, services/run_ledger.py), and
 *   `pipeline_queued` (the legacy status response in main.py,
 *   which the project list can still surface).
 * - Revision statuses (services/editor.py): `draft`, `rendering`,
 *   `exported`, `failed`. (The editor UI itself is C-03; the badge
 *   already speaks its vocabulary so that stage reuses this
 *   component instead of growing a second system.)
 *
 * An unlisted status is rendered as a neutral badge carrying the
 * humanised raw value — never hidden, never guessed at.
 */

type StatusTone = "neutral" | "progress" | "attention" | "success" | "danger";

type StatusPresentation = {
  label: string;
  tone: StatusTone;
  Icon: LucideIcon;
  spin?: boolean;
};

const STATUS_PRESENTATION: Record<string, StatusPresentation> = {
  created: { label: "Created", tone: "neutral", Icon: CircleDashed },
  queued: { label: "Queued", tone: "neutral", Icon: Clock3 },
  pipeline_queued: { label: "Pipeline queued", tone: "neutral", Icon: Clock3 },
  running: { label: "Running", tone: "progress", Icon: LoaderCircle, spin: true },
  awaiting_approval: { label: "Awaiting approval", tone: "attention", Icon: BellRing },
  rendering: { label: "Rendering", tone: "progress", Icon: Clapperboard },
  completed: { label: "Completed", tone: "success", Icon: CircleCheck },
  failed: { label: "Failed", tone: "danger", Icon: CircleExclamationPoint },
  cancelled: { label: "Cancelled", tone: "neutral", Icon: Ban },
  draft: { label: "Draft", tone: "neutral", Icon: FilePen },
  exported: { label: "Exported", tone: "success", Icon: Download },
};

/**
 * The project/run status vocabulary, exported for filters and
 * other shared UI (IA-12): exactly the project/run subset of the
 * presentation map above — revision-only statuses (draft,
 * exported, rendering) are not project statuses.
 */
export const PROJECT_STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  "created",
  "queued",
  "pipeline_queued",
  "running",
  "awaiting_approval",
  "completed",
  "failed",
  "cancelled",
].map((value) => ({ value, label: STATUS_PRESENTATION[value].label }));

export function StatusBadge({
  status,
  overlay = false,
}: {
  status: string;
  /** Card-art placement (absolute, over the preview) instead of inline flow. */
  overlay?: boolean;
}) {
  const presentation = STATUS_PRESENTATION[status];
  const Icon = presentation?.Icon ?? CircleDot;
  const label = presentation?.label ?? status.replaceAll("_", " ");
  return (
    <span
      className={`status-badge status-badge--${presentation?.tone ?? "neutral"}${overlay ? " status-badge--overlay" : ""}`}
    >
      <Icon
        size={11}
        strokeWidth={2.2}
        aria-hidden
        className={presentation?.spin ? "status-badge-spin" : undefined}
      />
      {label}
    </span>
  );
}
