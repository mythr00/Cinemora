"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import { BrandLockup } from "@/components/brand-lockup";
import { ArrowUpRight } from "lucide-react";

// Brief Stage 3 (C-02): /channel-lab, /thumbnail and /viral-clips
// were removed — their backends do not exist in this build and the
// routes now serve honest unavailable states (see register C-02).
// They must not reappear here.
//
// Brief Stage 4 (C-11): this is the ONE studio navigation. Every
// studio page renders it; the active item is derived from the
// pathname (project detail, plan and certificate routes belong to
// Projects) instead of being passed in by each caller, and the
// Create page's private sidebar — different labels for the same
// destinations, a brand link to the landing page — is retired.
const links = [
  ["/studio", "Dashboard"],
  ["/projects", "Projects"],
  ["/create", "New production"],
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
  const [open, setOpen] = useState(false);
  const active = activeFor(usePathname());

  return (
    <header className="app-nav">
      <Link className="app-brand" href="/studio" aria-label="Cinemora studio home" onClick={() => setOpen(false)}>
        <BrandLockup priority />
      </Link>
      <nav className={`app-nav-links ${open ? "is-open" : ""}`} id="studio-menu" aria-label="Studio navigation">
        {links.map(([href, label]) => (
          <Link key={href} href={href} aria-current={active === href ? "page" : undefined} onClick={() => setOpen(false)}>
            {label}
          </Link>
        ))}
      </nav>
      <Link className="app-nav-cta" href="/create">Create a film <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>
      <button className="app-menu-toggle" type="button" aria-expanded={open} aria-controls="studio-menu" aria-label={open ? "Close studio navigation" : "Open studio navigation"} onClick={() => setOpen((current) => !current)}>
        <span /><span />
      </button>
    </header>
  );
}
