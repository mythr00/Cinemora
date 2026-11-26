"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { ArrowRight } from "lucide-react";
import { AuthShell } from "@/components/auth-shell";
import { ApiClientError, recordAffiliateClick, register } from "@/lib/api";

/**
 * Create an account (Phase 8.5). Registration follows the
 * deployment's gate on the server (open / invite / closed — the
 * API's own error is shown verbatim when it refuses). The account
 * starts on the default plan; the session cookie is issued by the
 * register call itself, so a successful registration lands
 * straight in the studio. An affiliate referral code in the
 * URL (?ref=) is recorded and attributed at registration.
 */
export default function RegisterPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Item 1: an affiliate link (?ref=CODE) is recorded as a link
  // visit on arrival and carried into the registration itself,
  // where the server creates the lifetime attribution.
  const [referralCode, setReferralCode] = useState<string | null>(null);

  useEffect(() => {
    const ref = new URLSearchParams(window.location.search).get("ref");
    if (ref) {
      setReferralCode(ref);
      void recordAffiliateClick(ref).catch(() => undefined);
    }
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await register(
        email.trim(),
        password,
        displayName.trim() || undefined,
        referralCode || undefined,
      );
      router.replace("/studio");
      router.refresh();
    } catch (caught) {
      setError(
        caught instanceof ApiClientError
          ? caught.message
          : "Registration failed. Check your connection and try again.",
      );
      setBusy(false);
    }
  };

  return (
    <AuthShell eyebrow="GET STARTED" title="Create your account">
      <form className="auth-form" onSubmit={submit}>
        <label className="field-label" htmlFor="register-email">
          EMAIL
          <input
            id="register-email"
            className="thumbnail-input"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="you@example.com"
          />
        </label>
        <label className="field-label" htmlFor="register-name">
          DISPLAY NAME (OPTIONAL)
          <input
            id="register-name"
            className="thumbnail-input"
            type="text"
            autoComplete="name"
            maxLength={200}
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
          />
        </label>
        <label className="field-label" htmlFor="register-password">
          PASSWORD
          <input
            id="register-password"
            className="thumbnail-input"
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        <p className="auth-note">
          At least 12 characters. Your projects, media and
          productions are private to your account.
        </p>
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
          {busy ? "Creating account…" : "Create account"}
          {!busy && <ArrowRight size={15} strokeWidth={2} aria-hidden />}
        </button>
      </form>
      <p className="auth-alt">
        Already have an account? <Link href="/login">Sign in</Link>
      </p>
    </AuthShell>
  );
}
