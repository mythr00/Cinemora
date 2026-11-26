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
 * Intelligence — join the same ADMIN-ONLY set. Nothing is
 * deleted; admins keep every admin tab, and the pages/endpoints
 * enforce the same rule server-side.
 *
 * FS-G (FS-12): Team is NOT a tab at all — for any role. It has
 * its own dedicated page (the unchanged per-project route
 * /projects/[id]/team) reached from its own navigation entry:
 * the "Team" button in the project Overview header. Removing it
 * from this bar also ends the old completion walk in which Team,
 * as the terminal tab, was the last screen the tab bar led to
 * after generation. The ordinary user's tab bar is now exactly:
 * Overview, Configuration, Editor, Editor Studio, Thumbnails,
 * Localization.
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
 *
 * A-12 closure (2026-11-26, completeness S-4): the bar is now
 * GROUPED. The ordinary user's single group (Workspace) renders
 * exactly as before — no label, same six tabs in the same
 * order. Admins, who see all fourteen destinations, get three
 * labelled groups in the bar: Workspace (the user surfaces),
 * Production internals (Visual Plan, Research, Assets,
 * Graphics, Intelligence) and Delivery & records (Delivery,
 * QA Report, Versions). Grouping is presentation only: the
 * role filter (ADMIN_ONLY_SUFFIXES) and the editor-gate
 * behaviour are unchanged, and every tab remains a plain route
 * link to the same href as before.
 */
const TAB_GROUPS = [
  {
    label: "Workspace",
    tabs: [
      { suffix: "", label: "Overview" },
      { suffix: "/configure", label: "Configuration" },
      { suffix: "/edit", label: "Editor" },
      { suffix: "/studio", label: "Editor Studio" },
      { suffix: "/thumbnails", label: "Thumbnails" },
      { suffix: "/localization", label: "Localization" },
    ],
  },
  {
    label: "Production internals",
    tabs: [
      { suffix: "/plan", label: "Visual Plan" },
      { suffix: "/research", label: "Research" },
      { suffix: "/assets", label: "Assets" },
      { suffix: "/graphics", label: "Graphics" },
      { suffix: "/intelligence", label: "Intelligence" },
    ],
  },
  {
    label: "Delivery & records",
    tabs: [
      { suffix: "/certificate", label: "Delivery" },
      { suffix: "/qa", label: "QA Report" },
      { suffix: "/versions", label: "Versions" },
    ],
  },
  // FS-G (FS-12): no Team tab in any group — see the header
  // comment. The team page is reached from the Overview
  // header's Team entry.
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

  const visibleGroups = TAB_GROUPS.map((group) => ({
    ...group,
    tabs: group.tabs.filter(
      ({ suffix }) => isAdmin || !ADMIN_ONLY_SUFFIXES.has(suffix),
    ),
  })).filter((group) => group.tabs.length > 0);
  // Group labels are shown only when more than one group is
  // visible (the admin view). The ordinary user's bar is the
  // single Workspace group and renders exactly as it did
  // before grouping.
  const showGroupLabels = visibleGroups.length > 1;
  return (
    <nav className="project-tabs" aria-label="Project workspace">
      {visibleGroups.map((group) => (
        <span key={group.label} style={{ display: "contents" }}>
          {showGroupLabels && (
            <span
              className="project-tab-group-label"
              style={{
                alignSelf: "center",
                padding: "0 4px 0 12px",
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
          )}
          {group.tabs.map(({ suffix, label }) => {
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
        </span>
      ))}
    </nav>
  );
}
