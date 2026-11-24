"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { AccountMenu } from "@/components/account-menu";
import { BrandLockup } from "@/components/brand-lockup";
import { ApiConnectionBadge } from "@/components/connection-status";
import { getCurrentUser, listProjects, type AccountUser } from "@/lib/api";
import { BellRing, CirclePlus, Ellipsis, FolderOpen, KeyRound, LayoutDashboard, LayoutTemplate, Library, Network, ScrollText, Sparkles } from "lucide-react";

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
//
// GoMotion redesign (2026-11-24): the phone bottom bar carries
// five primary destinations (Create first, then Projects,
// Scripts, Templates, Library — the `phone` flag); the remaining
// sections live in the bar's More panel, rendered below. Desktop
// and tablet rails show every link exactly as before.
const links = [
  { href: "/studio", label: "Dashboard", Icon: LayoutDashboard, phone: false },
  { href: "/projects", label: "Projects", Icon: FolderOpen, phone: true },
  { href: "/scripts", label: "Scripts", Icon: ScrollText, phone: true },
  { href: "/templates", label: "Templates", Icon: LayoutTemplate, phone: true },
  { href: "/library", label: "Library", Icon: Library, phone: true },
  { href: "/intelligence", label: "Intelligence", Icon: Network, phone: false },
  { href: "/create", label: "Create", Icon: CirclePlus, phone: true },
  { href: "/assistant", label: "Assistant", Icon: Sparkles, phone: false },
  { href: "/developers", label: "Developers", Icon: KeyRound, phone: false },
] as const;

function activeFor(pathname: string | null): string {
  if (!pathname) return "";
  if (pathname === "/projects" || pathname.startsWith("/projects/")) return "/projects";
  if (pathname === "/scripts" || pathname.startsWith("/scripts/")) return "/scripts";
  if (pathname === "/templates" || pathname.startsWith("/templates/")) return "/templates";
  if (pathname === "/library" || pathname.startsWith("/library/")) return "/library";
  if (pathname === "/create") return "/create";
  if (pathname === "/assistant" || pathname.startsWith("/assistant")) return "/assistant";
  if (pathname === "/developers" || pathname.startsWith("/developers/")) return "/developers";
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
  const pathname = usePathname();
  const active = activeFor(pathname);
  const router = useRouter();

  // Phone bottom bar: the More panel (Dashboard, Intelligence,
  // Assistant, Developers) opens from the bar and closes on any
  // navigation. It is markup-identical links — the same routes,
  // never a second navigation system.
  const [moreOpen, setMoreOpen] = useState(false);
  useEffect(() => { setMoreOpen(false); }, [pathname]);
  const moreActive = links.some((link) => !link.phone && active === link.href);

  // Session gate (Phase 8.5): every studio surface is per-user,
  // so an unresolved session means sign-in comes first. Only a
  // definitive 401 from /auth/me redirects — a network failure
  // leaves the page in place, where the connection badge reports
  // the outage honestly instead of bouncing the user to a login
  // form that cannot work either.
  const [user, setUser] = useState<AccountUser | null>(null);
  const redirectedRef = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    getCurrentUser(controller.signal)
      .then((resolved) => {
        if (resolved) {
          setUser(resolved);
        } else if (!redirectedRef.current) {
          redirectedRef.current = true;
          const next = pathname ? `?next=${encodeURIComponent(pathname)}` : "";
          router.replace(`/login${next}`);
        }
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [pathname, router]);

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
          {user && <AccountMenu user={user} />}
        </div>
      </header>
      <nav className="app-rail" aria-label="Studio navigation">
        {links.map(({ href, label, Icon, phone }) => (
          <Link key={href} href={href} className={phone ? "rail-primary" : "rail-secondary"} aria-current={active === href ? "page" : undefined}>
            <Icon size={18} strokeWidth={1.8} aria-hidden />
            <span className="app-rail-label">{label}</span>
          </Link>
        ))}
        <div className="app-more">
          <button
            type="button"
            className={`app-more-button${moreActive ? " has-active" : ""}`}
            aria-expanded={moreOpen}
            aria-haspopup="true"
            onClick={() => setMoreOpen((open) => !open)}
          >
            <Ellipsis size={18} strokeWidth={1.8} aria-hidden />
            <span className="app-rail-label">More</span>
          </button>
          {moreOpen && (
            <div className="app-more-panel">
              {links.filter((link) => !link.phone).map(({ href, label, Icon }) => (
                <Link key={href} href={href} aria-current={active === href ? "page" : undefined} onClick={() => setMoreOpen(false)}>
                  <Icon size={18} strokeWidth={1.8} aria-hidden />
                  <span>{label}</span>
                </Link>
              ))}
            </div>
          )}
        </div>
      </nav>
    </>
  );
}
