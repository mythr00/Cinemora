import type { EditorAsset, EditorVerification, VisualItem } from "@/lib/api";

/** What the properties panel is showing. Selections are pure UI
 * state — the document itself always comes from the server. */
export type EditorSelection =
  | { kind: "item"; itemId: string }
  | { kind: "caption"; segmentIndex: number }
  | { kind: "sentence"; sentenceNumber: number }
  | { kind: "music" }
  | null;

/**
 * Shared presentation helpers for the Stage 7 editor (C-03).
 * Every label here is derived from the revision document's own
 * recorded fields — classification, evidence class, verification
 * and rights are the backend's vocabulary (services/editor.py
 * `classify_asset`), never invented by the UI. A licence recorded
 * as "unknown" is presented as unknown; unknown is not cleared.
 */

export function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00.0";
  const minutes = Math.floor(seconds / 60);
  const rest = seconds - minutes * 60;
  return `${minutes}:${rest.toFixed(1).padStart(4, "0")}`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  return `${seconds.toFixed(2)}s`;
}

export function fileNameOf(filePath: string | null | undefined): string {
  if (!filePath) return "—";
  const parts = filePath.split("/");
  return parts[parts.length - 1] || filePath;
}

/** The founder's Part 4 classification vocabulary, as recorded on
 * each timeline item by the backend. */
export const CLASSIFICATION_LABELS: Record<string, string> = {
  verified_footage: "Verified footage",
  positional_segment: "Positional segment",
  generated_graphic: "Generated graphic",
  still_image: "Still image",
  user_supplied: "User supplied",
  unverified: "Unverified",
  rejected: "Rejected",
  gap: "Gap",
};

export type ChipTone = "success" | "attention" | "accent" | "neutral" | "danger";

export function classificationTone(classification: string): ChipTone {
  switch (classification) {
    case "verified_footage":
    case "still_image":
      return "success";
    case "positional_segment":
    case "unverified":
      return "attention";
    case "generated_graphic":
      return "accent";
    case "rejected":
    case "gap":
      return "danger";
    default:
      return "neutral";
  }
}

export function itemTitle(item: VisualItem): string {
  if (item.kind === "gap") return "Gap";
  if (item.kind === "graphic") {
    return item.graphic?.template
      ? `Graphic · ${item.graphic.template.replaceAll("_", " ")}`
      : "Graphic";
  }
  return item.asset?.title || fileNameOf(item.asset?.file_path);
}

export function verificationLabel(verification: EditorVerification): string {
  if (!verification) return "No verdict recorded";
  if (verification.accepted === true) return "Accepted by the verifier";
  if (verification.accepted === false) {
    const reason = verification.rejection_reason || verification.reason;
    return reason ? `Not accepted (${String(reason).replaceAll("_", " ")})` : "Not accepted";
  }
  if (typeof verification.reason === "string" && verification.reason) {
    return String(verification.reason).replaceAll("_", " ");
  }
  return "Verdict recorded";
}

export function rightsLabel(asset: EditorAsset): string {
  const rights = asset?.rights;
  if (!rights) return "No rights record";
  const license =
    typeof rights.license === "string" && rights.license ? rights.license : "unknown";
  const status =
    typeof rights.rights_status === "string" && rights.rights_status
      ? rights.rights_status.replaceAll("_", " ")
      : null;
  return status ? `${license} · ${status}` : license;
}

/** Explicit gap time available inside one narration sentence —
 * the only time add_clip / move_clip may consume. Computed from
 * the document's gap items, exactly as the backend accounts it. */
export function gapTimeForSentence(items: VisualItem[], sentenceNumber: number): number {
  return items
    .filter((item) => item.kind === "gap" && item.sentence_number === sentenceNumber)
    .reduce((total, item) => total + item.duration, 0);
}
