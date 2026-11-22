"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";
import { ArrowRight } from "lucide-react";
import { AuthShell } from "@/components/auth-shell";
import { ApiClientError, login } from "@/lib/api";

/**
 * Sign in (Phase 8.5). The backend issues an httpOnly session
 * cookie; nothing credential-shaped is stored in the browser.
 * `?next=` returns the user to the page the session gate sent
 * them from (internal paths only — an external `next` is never
 * honoured).
 */
function safeNext(raw: string | null): string {
  if (raw && raw.startsWith("/") && !raw.startsWith("//")) return raw;
  return "/studio";
}

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email.trim(), password);
      router.replace(safeNext(searchParams.get("next")));
      router.refresh();
    } catch (caught) {
      setError(
        caught instanceof ApiClientError
          ? caught.message
          : "Sign-in failed. Check your connection and try again.",
      );
      setBusy(false);
    }
  };

  return (
    <AuthShell eyebrow="WELCOME BACK" title="Sign in to the studio">
      <form className="auth-form" onSubmit={submit}>
        <label className="field-label" htmlFor="login-email">
          EMAIL
          <input
            id="login-email"
            className="thumbnail-input"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="you@example.com"
          />
        </label>
        <label className="field-label" htmlFor="login-password">
          PASSWORD
          <input
            id="login-password"
            className="thumbnail-input"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        {error && (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}
        <button
          className="button button-primary auth-submit"
          type="submit"
          disabled={busy || !email.trim() || !password}
        >
          {busy ? "Signing in…" : "Sign in"}
          {!busy && <ArrowRight size={15} strokeWidth={2} aria-hidden />}
        </button>
      </form>
      <p className="auth-alt">
        New to Cinemora? <Link href="/register">Create an account</Link>
      </p>
      <p className="auth-alt">
        <Link href="/reset-password">Forgot your password?</Link>
      </p>
    </AuthShell>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}
