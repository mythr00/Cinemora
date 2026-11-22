"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { ChevronDown, LogOut, ShieldCheck, UserRound } from "lucide-react";
import {
  getAccountUsage,
  logout,
  type AccountUsage,
  type AccountUser,
} from "@/lib/api";

/**
 * The account menu (Phase 8.5): the signed-in user's identity in
 * the studio top bar — email, plan, role — with sign-out. All data
 * is real: the user comes from /auth/me (resolved by
 * AppNavigation's session gate) and the plan line from
 * /account/usage (derived from the run ledger server-side). When
 * usage cannot be loaded the plan line is simply omitted — never
 * estimated.
 *
 * The role is shown because Phase 7's admin surface gates on it:
 * admins carry the shield marker here; the admin UI itself is
 * Phase 7 scope, so this menu intentionally has no admin links.
 */
export function AccountMenu({ user }: { user: AccountUser }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [usage, setUsage] = useState<AccountUsage | null>(null);
  const [signingOut, setSigningOut] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open || usage) return;
    const controller = new AbortController();
    getAccountUsage(controller.signal)
      .then(setUsage)
      .catch(() => undefined);
    return () => controller.abort();
  }, [open, usage]);

  useEffect(() => {
    if (!open) return;
    const onClick = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open ]);

  const isAdmin = user.role === "admin" || user.role === "super_admin";

  const signOut = async () => {
    setSigningOut(true);
    try {
      await logout();
    } catch {
      // The cookie is cleared server-side on success; on failure
      // the session may still exist — the redirect to /login and
      // its gate re-check will surface the true state.
    }
    router.replace("/login");
    router.refresh();
  };

  return (
    <div className="account-menu" ref={rootRef}>
      <button
        type="button"
        className="account-menu-button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <UserRound size={15} strokeWidth={2} aria-hidden />
        <span className="account-menu-email">{user.email}</span>
        {isAdmin && (
          <ShieldCheck
            size={14}
            strokeWidth={2}
            aria-label="Administrator"
            className="account-menu-admin-icon"
          />
        )}
        <ChevronDown size={14} strokeWidth={2} aria-hidden />
      </button>
      {open && (
        <div className="account-menu-panel" role="menu">
          <div className="account-menu-identity">
            <p className="account-menu-name">
              {user.display_name || user.email}
            </p>
            <p className="account-menu-sub">{user.email}</p>
            <p className="account-menu-sub">
              Role: {user.role}
              {usage?.plan ? ` · Plan: ${usage.plan.name}` : ""}
            </p>
            {usage?.plan && (
              <p className="account-menu-sub">
                {usage.runs_in_period} production
                {usage.runs_in_period === 1 ? "" : "s"} this period
                {usage.plan.max_runs_per_period
                  ? ` of ${usage.plan.max_runs_per_period}`
                  : ""}
                {" · "}
                {usage.concurrent_runs} running now
              </p>
            )}
          </div>
          <Link
            className="account-menu-item"
            href="/reset-password"
            role="menuitem"
            onClick={() => setOpen(false)}
          >
            Reset password
          </Link>
          <button
            type="button"
            className="account-menu-item"
            role="menuitem"
            onClick={signOut}
            disabled={signingOut}
          >
            <LogOut size={14} strokeWidth={2} aria-hidden />
            {signingOut ? "Signing out…" : "Sign out"}
          </button>
        </div>
      )}
    </div>
  );
}
