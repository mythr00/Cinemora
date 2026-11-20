/**
 * VerdictChip (redesign §2.4 / §3.10): one production-quality
 * verdict — Integrity, Quality or Reliability — rendered as a
 * labelled chip. The three verdicts are separate facts and are
 * never merged into one badge: a render can complete while
 * quality fails, and neither state is a delivery approval.
 *
 * The backend's values are PASS / QUALITY_WARNING / FAIL /
 * UNKNOWN (services/quality_verdict.py). A null verdict —
 * nothing evaluable was recorded — renders as "Not recorded",
 * never as a pass.
 */
const VERDICT_PRESENTATION: Record<
  string,
  { label: string; tone: "success" | "attention" | "danger" | "neutral" }
> = {
  pass: { label: "Pass", tone: "success" },
  quality_warning: { label: "Quality warning", tone: "attention" },
  fail: { label: "Fail", tone: "danger" },
  unknown: { label: "Unknown", tone: "neutral" },
};

export function VerdictChip({
  label,
  verdict,
}: {
  label: string;
  verdict?: string | null;
}) {
  const presentation = verdict
    ? (VERDICT_PRESENTATION[verdict.toLowerCase()] ?? {
        label: verdict.replaceAll("_", " "),
        tone: "neutral" as const,
      })
    : { label: "Not recorded", tone: "neutral" as const };
  return (
    <span className={`verdict-chip verdict-chip--${presentation.tone}`}>
      <span>{label}</span>
      {presentation.label}
    </span>
  );
}
