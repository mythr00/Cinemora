"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

/**
 * The tabbed project workspace (IA-11, audit §H.1/H.2): one tab
 * bar over the four EXISTING project routes — no route is added,
 * removed or renamed, and every tab is a plain link so deep
 * links keep resolving exactly as before. The active tab is
 * derived from the pathname.
 */
const TABS = [
  { suffix: "", label: "Overview" },
  { suffix: "/plan", label: "Plan" },
  { suffix: "/edit", label: "Editor" },
  { suffix: "/certificate", label: "Provenance" },
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
