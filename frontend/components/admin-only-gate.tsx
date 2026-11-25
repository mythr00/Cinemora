"use client";

/**
 * Admin-only workspace gate (FS-B: FS-7/FS-9/FS-10, FS-14).
 *
 * Wraps the project-workspace diagnostic surfaces that are
 * admin-only — Asset Decisions (/assets), Delivery & Provenance
 * (/certificate) and the QA Report (/qa) — and, since FS-C
 * (FS-1/FS-5/FS-6/FS-11/FS-13), the presentational internal
 * surfaces: Visual Plan (/plan), Research (/research), Graphics
 * (/graphics), Versions (/versions), project and account
 * Intelligence (/intelligence), Library (/library) and Templates
 * (/templates). Follows the AdminShell
 * pattern (components/admin-shell.tsx): the signed-in user is
 * loaded from /api/v1/auth/me and the children render only when
 * the account role is admin or super_admin. Anyone else —
 * signed out, or an ordinary account, including the project
 * owner — gets the same plain not-found view the admin shell
 * uses, so a direct URL neither renders the internal UI nor
 * advertises the surface. This gate is presentation only; the
 * backend endpoints enforce the same rule, which is where the
 * authority lives.
 */

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";
import { ErrorState, LoadingState } from "@/components/states";
import { getCurrentUser, type AccountUser } from "@/lib/api";

export function AdminOnlyGate({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AccountUser | null | undefined>(
    undefined,
  );
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    const controller = new AbortController();
    getCurrentUser(controller.signal)
      .then((loaded) => setUser(loaded))
      .catch((cause) => {
        if (!controller.signal.aborted) setError(cause);
      });
    return () => controller.abort();
  }, []);

  if (error) {
    return (
      <main className="admin-page">
        <ErrorState
          error={error}
          onRetry={() => window.location.reload()}
        />
      </main>
    );
  }
  if (user === undefined) {
    return (
      <main className="admin-page">
        <LoadingState label="Loading…" />
      </main>
    );
  }
  const isAdmin =
    user !== null && (user.role === "admin" || user.role === "super_admin");
  if (!isAdmin) {
    return (
      <main className="admin-notfound">
        <p className="admin-notfound-code">404</p>
        <h1>This page could not be found.</h1>
        <p>
          The address may be wrong, or the page may have moved.{" "}
          <Link href="/projects">Return to the studio</Link>
        </p>
      </main>
    );
  }

  return <>{children}</>;
}
