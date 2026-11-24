import type { StudioItem } from "@/lib/api";

/** Studio selection state — pure UI state; the document itself
 * always comes from the server (the Studio view of the current
 * revision), exactly like the classic editor. */
export type StudioSelection =
  | { kind: "item"; itemId: string }
  | { kind: "caption"; segmentIndex: number }
  | { kind: "sentence"; sentenceNumber: number }
  | { kind: "music" }
  | null;

/** HH:MM:SS:FF at the renderer's 30 fps (services/transitions FPS). */
export function formatTimecode(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) seconds = 0;
  const frames = Math.floor((seconds % 1) * 30);
  const totalSeconds = Math.floor(seconds);
  const s = totalSeconds % 60;
  const m = Math.floor(totalSeconds / 60) % 60;
  const h = Math.floor(totalSeconds / 3600);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${pad(h)}:${pad(m)}:${pad(s)}:${pad(frames)}`;
}

export function studioItemLabel(item: StudioItem): string {
  if (item.kind === "gap") return "Gap (held over at render)";
  if (item.kind === "graphic") {
    return item.graphic?.template
      ? `Graphic · ${item.graphic.template.replaceAll("_", " ")}`
      : "Graphic";
  }
  return item.asset.title || item.asset.file_name || "Clip";
}

/** The narration sentence whose window contains t, if any. */
export function sentenceAt<T extends { start: number; end: number }>(
  windows: T[],
  t: number,
): T | null {
  for (const window of windows) {
    if (t >= window.start && t < window.end) return window;
  }
  return null;
}
