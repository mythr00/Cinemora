"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { getCurrentUser } from "@/lib/api";
import { checkEditorGate } from "@/lib/editor-gate";

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
 *
 * FS-C (FS-1/FS-5/FS-6/FS-11/FS-13): the presentational internal
 * tabs — Visual Plan, Research, Graphics, Versions and
 * Intelligence — join the same ADMIN-ONLY set, so the ordinary
 * user's tab bar is: Overview, Configuration, Editor, Editor
 * Studio, Thumbnails, Localization, Team. Nothing is deleted;
 * admins keep every tab, and the pages/endpoints enforce the
 * same rule server-side.
 *
 * FS-F (FS-8): the Editor and Editor Studio tabs are DISABLED
 * (rendered as inert, aria-disabled spans, never links) for
 * ordinary users until the project has a completed run and its
 * finished video exists — the same predicate the editor pages
 * and the backend gate apply (lib/editor-gate.ts). Admins are
 * exempt. While the gate state is still loading the tabs render
 * as links, matching this bar's existing role-loading behaviour
 * for the admin-only set; the pages and backend refuse a
 * not-ready project regardless.
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

const EDITOR_SUFFIXES: ReadonlySet<string> = new Set(["/edit", "/studio"]);

const ADMIN_ONLY_SUFFIXES: ReadonlySet<string> = new Set([
  // FS-B diagnostic surfaces (FS-7/FS-9/FS-10).
  "/assets",
  "/certificate",
  "/qa",
  // FS-C presentational surfaces (FS-5/FS-6/FS-11/FS-13).
  "/plan",
  "/research",
  "/graphics",
  "/versions",
  "/intelligence",
]);

export function ProjectTabs({ projectId }: { projectId: string }) {
  const pathname = usePathname() || "";
  const base = `/projects/${projectId}`;
  const [isAdmin, setIsAdmin] = useState(false);
  const [editorReady, setEditorReady] = useState<boolean | null>(null);

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

  useEffect(() => {
    const controller = new AbortController();
    checkEditorGate(projectId, controller.signal)
      .then((gate) => setEditorReady(gate.kind !== "not-ready"))
      .catch(() => undefined);
    return () => controller.abort();
  }, [projectId]);

  const visibleTabs = TABS.filter(
    ({ suffix }) => isAdmin || !ADMIN_ONLY_SUFFIXES.has(suffix),
  );
  return (
    <nav className="project-tabs" aria-label="Project workspace">
      {visibleTabs.map(({ suffix, label }) => {
        const href = `${base}${suffix}`;
        if (EDITOR_SUFFIXES.has(suffix) && editorReady === false) {
          return (
            <span
              key={label}
              className="project-tab-disabled"
              aria-disabled="true"
              title="Editing opens when generation completes successfully and the finished video exists"
            >
              {label}
            </span>
          );
        }
        return (
          <Link key={label} href={href} aria-current={pathname === href ? "page" : undefined}>
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
