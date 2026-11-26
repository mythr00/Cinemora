"use client";

/**
 * Administration shell (Phase 7).
 *
 * Renders only for a signed-in administrator: the signed-in user
 * is loaded from /api/v1/auth/me and must carry the admin or
 * super_admin role. Anyone else — signed out, or an ordinary
 * account — gets a plain not-found view: the admin surface is
 * never linked from the studio navigation and does not advertise
 * itself (design admin/03 §2.3). This gate is presentation only;
 * every /api/v1/admin endpoint enforces the same role check on
 * the backend, which is where the authority lives.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { BrandLockup } from "@/components/brand-lockup";
import { ErrorState, LoadingState } from "@/components/states";
import { getCurrentUser, type AccountUser } from "@/lib/api";
import { ShieldCheck } from "lucide-react";

/**
 * Admin navigation, grouped (A-12 closure, 2026-11-26,
 * completeness S-4): the twelve destinations render under
 * three section labels — Operations (the production system),
 * Accounts & billing (the customer estate) and System
 * (platform plumbing and records). Grouping is presentation
 * only: the same hrefs, labels and active-state rule as the
 * previous flat list; no page or capability changed.
 */
const ADMIN_LINK_GROUPS: Array<{
  label: string;
  links: Array<{ href: string; label: string }>;
}> = [
  {
    label: "Operations",
    links: [
      { href: "/admin", label: "Overview" },
      { href: "/admin/runs", label: "Productions" },
      { href: "/admin/workers", label: "Workers & queues" },
      { href: "/admin/providers", label: "Provider usage" },
      { href: "/admin/health", label: "Health" },
      { href: "/admin/storage", label: "Storage" },
    ],
  },
  {
    label: "Accounts & billing",
    links: [
      { href: "/admin/users", label: "Users" },
      { href: "/admin/subscriptions", label: "Subscriptions overview" },
      { href: "/admin/projects", label: "Projects" },
    ],
  },
  {
    label: "System",
    links: [
      { href: "/admin/developer", label: "Developer API" },
      { href: "/admin/audit", label: "Audit log" },
      { href: "/admin/settings", label: "Settings overview" },
    ],
  },
];

function NotFoundView() {
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

export function AdminShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
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
    return <NotFoundView />;
  }

  return (
    <div className="admin-frame">
      <header className="admin-header">
        <div className="admin-header-brand">
          <BrandLockup />
          <span className="admin-header-title">
            <ShieldCheck size={15} strokeWidth={2} aria-hidden />
            Administration
          </span>
        </div>
        <p className="admin-header-identity">
          {user.email} · {user.role}
        </p>
      </header>
      <nav className="admin-nav" aria-label="Administration">
        {ADMIN_LINK_GROUPS.map((group) => (
          <span key={group.label} style={{ display: "contents" }}>
            <span
              className="admin-nav-group-label"
              style={{
                alignSelf: "center",
                padding: "6px 4px 6px 14px",
                color: "var(--ink-muted)",
                fontSize: 11,
                fontWeight: 650,
                letterSpacing: "0.07em",
                textTransform: "uppercase",
                whiteSpace: "nowrap",
              }}
            >
              {group.label}
            </span>
            {group.links.map((link) => (
              <Link
                key={link.href}
                href={link.href}
                className={
                  pathname === link.href ? "admin-nav-link is-active" : "admin-nav-link"
                }
              >
                {link.label}
              </Link>
            ))}
          </span>
        ))}
      </nav>
      <main className="admin-page">{children}</main>
    </div>
  );
}
