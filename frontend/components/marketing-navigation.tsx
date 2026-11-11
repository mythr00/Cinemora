"use client";

import Link from "next/link";
import { useState } from "react";
import { BrandLockup } from "@/components/brand-lockup";

const links = [
  ["#workflow", "How it works"],
  ["#stories", "Story types"],
  ["#capabilities", "Tools"],
  ["/projects", "Projects"],
] as const;

export function MarketingNavigation() {
  const [open, setOpen] = useState(false);

  return (
    <header className="marketing-nav-wrap">
      <nav className="marketing-nav" aria-label="Main navigation">
        <Link className="marketing-brand" href="/" aria-label="Cinemora home" onClick={() => setOpen(false)}>
          <BrandLockup priority />
        </Link>
        <div className={`marketing-links ${open ? "is-open" : ""}`} id="marketing-menu">
          {links.map(([href, label]) => href.startsWith("#") ? <a key={href} href={href} onClick={() => setOpen(false)}>{label}</a> : <Link key={href} href={href} onClick={() => setOpen(false)}>{label}</Link>)}
        </div>
        <Link className="marketing-nav-cta" href="/create">Open the studio <span aria-hidden="true">↗</span></Link>
        <button className="marketing-menu-toggle" type="button" aria-expanded={open} aria-controls="marketing-menu" aria-label={open ? "Close navigation menu" : "Open navigation menu"} onClick={() => setOpen((current) => !current)}>
          <span /><span />
        </button>
      </nav>
    </header>
  );
}
