"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { BrandLockup } from "@/components/brand-lockup";
import { ApiConnectionBadge } from "@/components/connection-status";
import { listProjects } from "@/lib/api";
import { BellRing, CirclePlus, FolderOpen, LayoutDashboard } from "lucide-react";

// Brief Stage 3 (C-02): /channel-lab, /thumbnail and /viral-clips
// were removed from navigation — their backends do not exist in
// this build and the routes now serve honest unavailable states.
// They must not reappear here.
//
// Redesign F0 (plan §2.6): the ONE studio shell. A slim top bar
// (brand → /studio, the authenticated connection badge, the
// awaiting-approval indicator — the IA-11 system strip's real
// data, folded in) plus a left rail for the application
// sections. The rail is icon+label, collapses to icons on
// tablet, and becomes a bottom bar on phone (CSS, globals.css).
// The active item is derived from the pathname (project detail,
// plan, assets, graphics, editor and certificate routes belong
// to Projects) instead of being passed in by each caller.
const links = [
  { href: "/studio", label: "Dashboard", Icon: LayoutDashboard },
  { href: "/projects", label: "Projects", Icon: FolderOpen },
  { href: "/create", label: "New production", Icon: CirclePlus },
] as const;

function activeFor(pathname: string | null): string {
  if (!pathname) return "";
  if (pathname === "/projects" || pathname.startsWith("/projects/")) return "/projects";
  if (pathname === "/create") return "/create";
  if (pathname === "/studio") return "/studio";
  return "";
}

export function AppNavigation({
  // Accepted and ignored, solely so the verbatim Stage 3
  // quarantine copies under frontend/preserved/ — which still
  // pass active="…" and must not be edited — keep typechecking.
  // No live caller passes it; the active item always comes from
  // the pathname.
  active: _legacyActive,
}: { active?: string } = {}) {
  void _legacyActive;
  const active = activeFor(usePathname());

  // The awaiting-approval count comes from the real project list
  // (the same data the dashboard queue uses). When the list
  // cannot be loaded the count is simply not shown — never
  // estimated, never faked.
  const [awaitingCount, setAwaitingCount] = useState<number | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      listProjects(controller.signal)
        .then((data) =>
          setAwaitingCount(
            (data.projects || []).filter(
              (project) =>
                project.status === "awaiting_approval" ||
                project.latest_run?.status === "awaiting_approval",
            ).length,
          ),
        )
        .catch(() => undefined);
    }, 0);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, []);

  return (
    <>
      <header className="app-topbar">
        <Link className="app-brand" href="/studio" aria-label="Cinemora studio home">
          <BrandLockup size="app" priority />
        </Link>
        <div className="app-topbar-right">
          {awaitingCount !== null && awaitingCount > 0 && (
            <Link className="app-approvals" href="/studio">
              <BellRing size={14} strokeWidth={2} aria-hidden />
              {awaitingCount} awaiting approval
            </Link>
          )}
          <ApiConnectionBadge />
        </div>
      </header>
      <nav className="app-rail" aria-label="Studio navigation">
        {links.map(({ href, label, Icon }) => (
          <Link key={href} href={href} aria-current={active === href ? "page" : undefined}>
            <Icon size={18} strokeWidth={1.8} aria-hidden />
            <span className="app-rail-label">{label}</span>
          </Link>
        ))}
      </nav>
    </>
  );
}
