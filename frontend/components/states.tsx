import type { ReactNode } from "react";
import { CircleExclamationPoint } from "lucide-react";
import { ApiClientError } from "@/lib/api";

/**
 * Shared loading / empty / error patterns (Brief Stage 4, C-16).
 *
 * The v1 pages already handled these states honestly but each in
 * its own markup; these components normalise the presentation
 * while keeping each page's existing CSS classes (and therefore
 * its exact styling). The rule they exist to enforce: a failed
 * request is presented as an error — with its classified cause
 * and a retry — never as an empty state. (The certificate page's
 * "every error is 'No certificate yet'" presentation was the last
 * instance; Brief Stage 5 (C-06) corrected it on top of
 * `classifyApiError` below, reserving the empty state for the
 * API's genuine no_runs case.)
 */

export type ApiErrorKind =
  | "auth"
  | "forbidden"
  | "not-found"
  | "unavailable"
  | "network"
  | "error";

/**
 * Map a failed request to its honest presentation class, from the
 * API client's error shape: ApiClientError carries the HTTP
 * status (401 auth, 403 forbidden, 404 not found, 503 the API's
 * fail-closed "not configured" state); a fetch that never reached
 * the server surfaces as a TypeError and is a network failure.
 * AbortError is deliberately not classified here — callers guard
 * aborted requests themselves and never render them.
 */
export function classifyApiError(error: unknown): { kind: ApiErrorKind; message: string } {
  if (error instanceof ApiClientError) {
    switch (error.status) {
      case 401:
        return { kind: "auth", message: error.message };
      case 403:
        return { kind: "forbidden", message: error.message };
      case 404:
        return { kind: "not-found", message: error.message };
      case 503:
        return { kind: "unavailable", message: error.message };
      default:
        return { kind: "error", message: error.message };
    }
  }
  if (error instanceof TypeError) {
    return {
      kind: "network",
      message: "The studio API could not be reached. Check that it is running, then try again.",
    };
  }
  return {
    kind: "error",
    message: error instanceof Error ? error.message : "The request could not be completed.",
  };
}

const ERROR_HEADINGS: Record<ApiErrorKind, string> = {
  auth: "Authentication required",
  forbidden: "Access denied",
  "not-found": "Not found",
  unavailable: "Studio API unavailable",
  network: "Connection problem",
  error: "Something went wrong",
};

export function LoadingState({ label, className }: { label: string; className?: string }) {
  return (
    <div className={className} role="status">
      {label}
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
  className,
}: {
  error: unknown;
  onRetry?: () => void;
  className?: string;
}) {
  const { kind, message } = classifyApiError(error);
  return (
    <div className={`state-error${className ? ` ${className}` : ""}`} role="alert">
      <span className="state-error-copy">
        <CircleExclamationPoint size={15} strokeWidth={2} aria-hidden />
        <span>
          <strong>{ERROR_HEADINGS[kind]}.</strong> {message}
        </span>
      </span>
      {onRetry && (
        <button type="button" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

export function EmptyState({
  eyebrow,
  title,
  titleLevel = "h3",
  description,
  action,
  className,
}: {
  eyebrow: string;
  title: string;
  titleLevel?: "h2" | "h3";
  description: string;
  action?: ReactNode;
  className?: string;
}) {
  const Title = titleLevel;
  return (
    <div className={className}>
      <div className="empty-reel" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <p className="eyebrow">{eyebrow}</p>
      <Title>{title}</Title>
      <p>{description}</p>
      {action}
    </div>
  );
}
