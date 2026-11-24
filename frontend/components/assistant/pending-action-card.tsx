"use client";

import { useState } from "react";
import { AlertTriangle, CheckCircle2, ShieldAlert, XCircle } from "lucide-react";
import {
  confirmAssistantAction,
  rejectAssistantAction,
  type AssistantAction,
} from "@/lib/api";

// Phase 6 (design assistant/07 §2/§4): the confirmation card is an
// inline thread element, never a modal. WRITE actions confirm in
// one click; CONSEQUENTIAL actions arm first, then confirm — a
// real two-step state machine mirroring the backend's pending
// state. After a decision the card updates in place with the
// backend's actual outcome (executed / failed / rejected /
// expired); it never claims success the backend did not report.

function argumentSummary(action: AssistantAction): string {
  const entries = Object.entries(action.arguments || {}).filter(
    ([, value]) => value !== null && value !== undefined && value !== "",
  );
  if (entries.length === 0) return "";
  return entries
    .map(([key, value]) =>
      typeof value === "object"
        ? `${key}: ${JSON.stringify(value)}`
        : `${key}: ${String(value)}`,
    )
    .join(" · ");
}

function resultSummary(action: AssistantAction): string | null {
  const result = action.result;
  if (!result) return null;
  if (typeof result.error === "string") return result.error;
  if (action.tool_name === "cancel_run" && result.cancel_requested) {
    return "Cancellation requested — the run will stop at its next checkpoint.";
  }
  if (action.tool_name === "create_project" && typeof result.id === "string") {
    return `Project created: ${String(result.title || result.id)}`;
  }
  if (action.tool_name === "retry_eligible_stage") {
    const run = result.run as { run_id?: string } | undefined;
    return run?.run_id
      ? `Retry submitted as a new run (${run.run_id}).`
      : "Retry submitted.";
  }
  if (action.tool_name === "submit_production_run") {
    const run = result.run as { run_id?: string } | undefined;
    return run?.run_id ? `Run submitted (${run.run_id}).` : "Run submitted.";
  }
  return null;
}

export function PendingActionCard({
  action: initial,
  onDecided,
}: {
  action: AssistantAction;
  onDecided?: (action: AssistantAction) => void;
}) {
  const [action, setAction] = useState(initial);
  const [busy, setBusy] = useState<"confirm" | "reject" | null>(null);
  const [armed, setArmed] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const consequential = action.action_class === "consequential";
  const pending = action.status === "pending";
  const summary = resultSummary(action);

  async function decide(kind: "confirm" | "reject") {
    if (kind === "confirm" && consequential && !armed) {
      setArmed(true);
      return;
    }
    setBusy(kind);
    setError(null);
    try {
      const response =
        kind === "confirm"
          ? await confirmAssistantAction(action.id)
          : await rejectAssistantAction(action.id);
      setAction(response.action);
      onDecided?.(response.action);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "The decision could not be recorded.",
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <section
      className={`assistant-action-card${consequential ? " is-consequential" : ""}`}
      aria-label={`Proposed action: ${action.label}`}
    >
      <header className="assistant-action-head">
        {consequential ? (
          <ShieldAlert size={16} strokeWidth={2} aria-hidden />
        ) : (
          <AlertTriangle size={16} strokeWidth={2} aria-hidden />
        )}
        <strong>{action.label}</strong>
        <span className="assistant-action-class">
          {consequential ? "Consequential — needs explicit confirmation" : "Needs confirmation"}
        </span>
      </header>
      {argumentSummary(action) && (
        <p className="assistant-action-args">{argumentSummary(action)}</p>
      )}
      {action.tool_name === "retry_eligible_stage" &&
        action.result &&
        typeof action.result === "object" && (
          <p className="assistant-action-args">
            {(action.result as { eligibility?: { eligible?: boolean; reason?: string } }).eligibility
              ?.eligible
              ? "Eligibility checked: this run can be retried with stage adoption."
              : `Eligibility checked: ${(action.result as { eligibility?: { reason?: string } }).eligibility?.reason || "not eligible."}`}
          </p>
        )}
      {pending ? (
        <>
          <p className="assistant-action-note">
            Nothing has happened yet. This takes effect only if you confirm
            {action.expires_at
              ? ` — the proposal expires at ${new Date(action.expires_at).toLocaleTimeString()}.`
              : "."}
          </p>
          {armed && consequential && (
            <p className="assistant-action-note is-danger" role="alert">
              This cannot be undone from here — in-progress work on the run
              will be discarded. Click confirm again to proceed.
            </p>
          )}
          {error && (
            <p className="assistant-action-note is-danger" role="alert">
              {error}
            </p>
          )}
          <div className="assistant-action-buttons">
            <button
              type="button"
              className={consequential ? "button button-danger" : "button button-primary"}
              disabled={busy !== null}
              onClick={() => void decide("confirm")}
            >
              {busy === "confirm"
                ? "Working…"
                : armed && consequential
                  ? "Confirm — do it"
                  : "Confirm"}
            </button>
            <button
              type="button"
              className="button"
              disabled={busy !== null}
              onClick={() => void decide("reject")}
            >
              {busy === "reject" ? "Working…" : "Dismiss"}
            </button>
          </div>
        </>
      ) : (
        <p className="assistant-action-outcome">
          {action.status === "executed" ? (
            <CheckCircle2 size={14} strokeWidth={2} aria-hidden />
          ) : (
            <XCircle size={14} strokeWidth={2} aria-hidden />
          )}
          <span>
            {action.status === "executed"
              ? "Done."
              : action.status === "rejected"
                ? "Dismissed — nothing was changed."
                : action.status === "expired"
                  ? "Expired before confirmation — nothing was changed."
                  : "Failed."}
            {summary ? ` ${summary}` : ""}
          </span>
        </p>
      )}
    </section>
  );
}
