import { CLASSIFICATION_LABELS, classificationTone } from "./shared";

/**
 * The backend's per-asset classification (Part 4 vocabulary:
 * verified footage / positional segment / generated graphic /
 * still / user-supplied / unverified / rejected), rendered as a
 * labelled chip — text first, colour second, so the meaning never
 * rides on colour alone.
 */
export function ClassificationChip({ classification }: { classification: string }) {
  const label = CLASSIFICATION_LABELS[classification] ?? classification.replaceAll("_", " ");
  return (
    <span className={`editor-chip editor-chip--${classificationTone(classification)}`}>
      {label}
    </span>
  );
}
