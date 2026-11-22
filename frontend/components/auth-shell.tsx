import Link from "next/link";
import type { ReactNode } from "react";
import { BrandLockup } from "@/components/brand-lockup";

/**
 * The centred card shell shared by the account pages (sign in,
 * register, password reset) — Phase 8.5. Pure white, the canonical
 * brand lockup, one card; the forms themselves are the pages.
 */
export function AuthShell({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <main className="auth-page">
      <div className="auth-card">
        <Link className="auth-brand" href="/" aria-label="Cinemora home">
          <BrandLockup size="app" priority />
        </Link>
        <p className="eyebrow">{eyebrow}</p>
        <h1 className="auth-title">{title}</h1>
        {children}
      </div>
    </main>
  );
}
