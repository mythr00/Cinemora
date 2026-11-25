"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { BrandLockup } from "@/components/brand-lockup";
import { getCurrentUser, type AccountUser } from "@/lib/api";
import { ArrowUpRight } from "lucide-react";

const links = [
  ["#workflow", "How it works"],
  ["#capabilities", "Tools"],
  ["/projects", "Projects"],
  ["/studio", "Studio"],
] as const;

/**
 * Marketing navigation (Phase 6, A-01).
 *
 * The landing page previously offered NO sign-in / sign-up entry:
 * the only path in was an indirect session-gate bounce from an
 * app surface. This nav now carries explicit account entries —
 * "Sign in" (/login) and "Create account" (/register) for signed-
 * out visitors, in the desktop bar AND the mobile menu. Session
 * state comes from the same /auth/me source the app shell uses
 * (getCurrentUser): a signed-in visitor sees an "Open app" entry
 * instead, never login links. While the session is unresolved
 * nothing account-related renders (no wrong-state flash); a
 * network failure resolves to signed-out, because this is a
 * public page and the login page itself reports outages.
 */
export function MarketingNavigation() {
  const [open, setOpen] = useState(false);
  const [user, setUser] = useState<AccountUser | null | undefined>(undefined);

  useEffect(() => {
    const controller = new AbortController();
    getCurrentUser(controller.signal)
      .then((resolved) => setUser(resolved))
      .catch(() => {
        if (!controller.signal.aborted) setUser(null);
      });
    return () => controller.abort();
  }, []);

  const signedIn = user !== null && user !== undefined;

  return (
    <header className="marketing-nav-wrap">
      <nav className="marketing-nav" aria-label="Main navigation">
        <Link className="marketing-brand" href="/" aria-label="Cinemora home" onClick={() => setOpen(false)}>
          <BrandLockup size="marketing" priority />
        </Link>
        <div className={`marketing-links ${open ? "is-open" : ""}`} id="marketing-menu">
          {links.map(([href, label]) => href.startsWith("#") ? <a key={href} href={href} onClick={() => setOpen(false)}>{label}</a> : <Link key={href} href={href} onClick={() => setOpen(false)}>{label}</Link>)}
          {user !== undefined && (signedIn ? (
            <Link href="/studio" onClick={() => setOpen(false)}>Open app</Link>
          ) : (
            <>
              <Link href="/login" onClick={() => setOpen(false)}>Sign in</Link>
              <Link href="/register" onClick={() => setOpen(false)}>Create account</Link>
            </>
          ))}
        </div>
        {user !== undefined && (signedIn ? (
          <Link className="marketing-nav-cta" href="/studio">Open app <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>
        ) : (
          <span className="marketing-auth">
            <Link className="marketing-signin" href="/login">Sign in</Link>
            <Link className="marketing-nav-cta" href="/register">Create account <ArrowUpRight size={14} strokeWidth={2} aria-hidden /></Link>
          </span>
        ))}
        <button className="marketing-menu-toggle" type="button" aria-expanded={open} aria-controls="marketing-menu" aria-label={open ? "Close navigation menu" : "Open navigation menu"} onClick={() => setOpen((current) => !current)}>
          <span /><span />
        </button>
      </nav>
    </header>
  );
}
