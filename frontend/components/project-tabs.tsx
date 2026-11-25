"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { getCurrentUser } from "@/lib/api";

/**
 * The tabbed project workspace (IA-11, extended by redesign F1/F2,
 * plan §2.6): one tab bar over the project's real work surfaces —
 * Overview, Visual Plan, Assets, Graphics, Editor, Delivery.
 * Every tab is a plain route link so deep links keep resolving
 * exactly as before; a tab whose backend artefact does not exist
 * yet for that project renders its honest empty/not-recorded
 * state — tabs are never hidden on a guess, and no tab exists
 * for a feature with no backend at all. The active tab is
 * derived from the pathname.
 *
 * FS-B (FS-7/FS-9/FS-10, FS-14): the diagnostic tabs — Assets
 * (Asset Decisions), Delivery (Delivery & Provenance) and
 * QA Report — are ADMIN-ONLY. They render only when /auth/me
 * reports an admin or super_admin account role (the operator
 * principal included, per backend/services/auth.py); for every
 * other principal — and until the role is known — they are
 * absent from the bar. The pages themselves are gated by
 * components/admin-only-gate.tsx and their backend endpoints
 * enforce the same rule server-side; hiding the tab is
 * presentation, not the barrier. The per-project collaboration
 * role (owner/editor/viewer) is NOT an admin signal and never
 * reveals these tabs.
 */
const TABS = [
  { suffix: "", label: "Overview" },
  { suffix: "/plan", label: "Visual Plan" },
  { suffix: "/configure", label: "Configuration" },
  { suffix: "/research", label: "Research" },
  { suffix: "/assets", label: "Assets" },
  { suffix: "/graphics", label: "Graphics" },
  { suffix: "/edit", label: "Editor" },
  { suffix: "/studio", label: "Editor Studio" },
  { suffix: "/certificate", label: "Delivery" },
  { suffix: "/qa", label: "QA Report" },
  { suffix: "/thumbnails", label: "Thumbnails" },
  { suffix: "/localization", label: "Localization" },
  { suffix: "/versions", label: "Versions" },
  { suffix: "/intelligence", label: "Intelligence" },
  { suffix: "/team", label: "Team" },
] as const;

const ADMIN_ONLY_SUFFIXES: ReadonlySet<string> = new Set([
  "/assets",
  "/certificate",
  "/qa",
]);

export function ProjectTabs({ projectId }: { projectId: string }) {
  const pathname = usePathname() || "";
  const base = `/projects/${projectId}`;
  const [isAdmin, setIsAdmin] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    getCurrentUser(controller.signal)
      .then((user) =>
        setIsAdmin(
          user !== null &&
            (user.role === "admin" || user.role === "super_admin"),
        ),
      )
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  const visibleTabs = TABS.filter(
    ({ suffix }) => isAdmin || !ADMIN_ONLY_SUFFIXES.has(suffix),
  );
  return (
    <nav className="project-tabs" aria-label="Project workspace">
      {visibleTabs.map(({ suffix, label }) => {
        const href = `${base}${suffix}`;
        return (
          <Link key={label} href={href} aria-current={pathname === href ? "page" : undefined}>
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
