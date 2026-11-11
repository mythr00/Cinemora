"use client";

import Link from "next/link";
import { useState } from "react";
import { BrandLockup } from "@/components/brand-lockup";

const links = [
  ["/studio", "Dashboard"],
  ["/projects", "Projects"],
  ["/create", "New production"],
  ["/channel-lab", "Script"],
  ["/thumbnail", "Thumbnail"],
  ["/viral-clips", "Short clips"],
] as const;

export function AppNavigation({ active }: { active: string }) {
  const [open, setOpen] = useState(false);

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
      <Link className="app-nav-cta" href="/create">Create a film <span aria-hidden="true">↗</span></Link>
      <button className="app-menu-toggle" type="button" aria-expanded={open} aria-controls="studio-menu" aria-label={open ? "Close studio navigation" : "Open studio navigation"} onClick={() => setOpen((current) => !current)}>
        <span /><span />
      </button>
    </header>
  );
}
