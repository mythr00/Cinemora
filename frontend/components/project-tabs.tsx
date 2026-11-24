"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

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
 */
const TABS = [
  { suffix: "", label: "Overview" },
  { suffix: "/plan", label: "Visual Plan" },
  { suffix: "/assets", label: "Assets" },
  { suffix: "/graphics", label: "Graphics" },
  { suffix: "/edit", label: "Editor" },
  { suffix: "/studio", label: "Editor Studio" },
  { suffix: "/certificate", label: "Delivery" },
] as const;

export function ProjectTabs({ projectId }: { projectId: string }) {
  const pathname = usePathname() || "";
  const base = `/projects/${projectId}`;
  return (
    <nav className="project-tabs" aria-label="Project workspace">
      {TABS.map(({ suffix, label }) => {
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
