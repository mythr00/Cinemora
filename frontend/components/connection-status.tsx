"use client";

import { useCallback, useEffect, useState } from "react";
import {
  CircleCheck,
  CircleExclamationPoint,
  CloudOff,
  KeyRound,
  LoaderCircle,
  Settings2,
} from "lucide-react";
import {
  checkApiConnection,
  type ApiConnectionState,
  type ApiConnectionStatus,
} from "@/lib/api";

/**
 * The authenticated connection indicator (Brief Stage 5, C-06).
 *
 * Replaces the unauthenticated health probe that used to drive a
 * "Studio API reachable" badge on the Create page (audit D-05):
 * that probe could glow while every authenticated call failed.
 * This component performs one authenticated v1 call on mount (and
 * again only when the operator presses retry — no polling) and
 * renders the classified state as icon + text, never a bare dot:
 * connected / session rejected / frontend not configured / server
 * not configured / API unavailable / network unreachable. The
 * detailed message is exposed as the element's title and as
 * screen-reader text; the visible label stays short.
 */

const PRESENTATION: Record<
  ApiConnectionState,
  { label: string; Icon: typeof CircleCheck; tone: "ok" | "bad" | "warn" }
> = {
  connected: { label: "API connected", Icon: CircleCheck, tone: "ok" },
  auth: { label: "Session rejected", Icon: KeyRound, tone: "bad" },
  "frontend-not-configured": { label: "API not configured in this build", Icon: Settings2, tone: "warn" },
  "backend-not-configured": { label: "API not configured on server", Icon: Settings2, tone: "warn" },
  unavailable: { label: "Studio API unavailable", Icon: CircleExclamationPoint, tone: "bad" },
  network: { label: "Studio API unreachable", Icon: CloudOff, tone: "bad" },
};

export function ApiConnectionBadge({ onStatus }: { onStatus?: (status: ApiConnectionStatus) => void }) {
  const [status, setStatus] = useState<ApiConnectionStatus | null>(null);
  const [checking, setChecking] = useState(true);

  const check = useCallback(async () => {
    setChecking(true);
    try {
      const result = await checkApiConnection();
      setStatus(result);
      onStatus?.(result);
    } catch {
      // Aborted (unmount): leave the previous state untouched.
    } finally {
      setChecking(false);
    }
  }, [onStatus]);

  useEffect(() => {
    const timer = setTimeout(() => { void check(); }, 0);
    return () => clearTimeout(timer);
  }, [check]);

  if (!status) {
    return (
      <span className="connection connection-checking" role="status">
        <LoaderCircle size={13} strokeWidth={2} aria-hidden className="status-badge-spin" />
        <span>Checking API connection…</span>
      </span>
    );
  }

  const { label, Icon, tone } = PRESENTATION[status.state];
  return (
    <span className={`connection connection-${tone}`} role="status" title={status.message}>
      <Icon size={13} strokeWidth={2} aria-hidden />
      <span>{checking ? "Checking…" : label}</span>
      <span className="sr-only">{status.message}</span>
      <button type="button" className="connection-retry" onClick={() => void check()} disabled={checking}>
        Retry
      </button>
    </span>
  );
}
