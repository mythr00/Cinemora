"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { ChevronDown, LogOut, UserRound } from "lucide-react";
import {
  getAccountUsage,
  getNotifications,
  logout,
  type AccountUsage,
  type AccountUser,
} from "@/lib/api";

/**
 * The account menu (Phase 8.5): the signed-in user's identity in
 * the studio top bar — email and plan — with sign-out. All data
 * is real: the user comes from /auth/me (resolved by
 * AppNavigation's session gate) and the plan line from
 * /account/usage (derived from the run ledger server-side). When
 * usage cannot be loaded the plan line is simply omitted — never
 * estimated.
 *
 * Item 7 (Product Improvements, 2026-11-26): account roles are
 * an internal authorisation detail. They are never rendered as
 * labels anywhere in the customer-facing UI (no "Role: user",
 * no administrator marker); backend enforcement is unchanged
 * and the admin console keeps its own role displays.
 */
export function AccountMenu({ user }: { user: AccountUser }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [usage, setUsage] = useState<AccountUsage | null>(null);
  const [unread, setUnread] = useState(0);
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
    const controller = new AbortController();
    getNotifications(controller.signal)
      .then((result) => setUnread(result.unread_count))
      .catch(() => undefined);
    return () => controller.abort();
  }, [open ]);

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

  // Item 7 (Product Improvements): role values drive backend
  // authorisation and route gating only — they are never
  // rendered as labels in the customer-facing UI.

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
        <ChevronDown size={14} strokeWidth={2} aria-hidden />
      </button>
      {open && (
        <div className="account-menu-panel" role="menu">
          <div className="account-menu-identity">
            <p className="account-menu-name">
              {user.display_name || user.email}
            </p>
            <p className="account-menu-sub">{user.email}</p>
            {usage?.plan && (
              <p className="account-menu-sub">
                Plan: {usage.plan.name}
              </p>
            )}
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
            href="/billing"
            role="menuitem"
            onClick={() => setOpen(false)}
          >
            Billing &amp; credits
            {unread > 0 && (
              <span className="account-menu-badge">{unread}</span>
            )}
          </Link>
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
