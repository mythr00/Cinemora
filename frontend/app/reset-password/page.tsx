"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";
import { ArrowRight } from "lucide-react";
import { AuthShell } from "@/components/auth-shell";
import {
  ApiClientError,
  confirmPasswordReset,
  requestPasswordReset,
} from "@/lib/api";

/**
 * Password reset (Phase 8.5), two honest halves:
 * - Without a token: the request form. The API answers identically
 *   for known and unknown emails; the confirmation text follows
 *   the deployment's real delivery mode — "smtp" means an email
 *   was sent, "manual" means this deployment sends no email and
 *   the operator provides the link (it is written to the server
 *   log). Nothing here pretends an email exists when it doesn't.
 * - With ?token=: the set-new-password form the emailed/logged
 *   link points at. A successful reset revokes every existing
 *   session for the account.
 */
function RequestForm() {
  const [email, setEmail] = useState("");
  const [delivery, setDelivery] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await requestPasswordReset(email.trim());
      setDelivery(result.delivery);
    } catch (caught) {
      setError(
        caught instanceof ApiClientError
          ? caught.message
          : "The request failed. Check your connection and try again.",
      );
    } finally {
      setBusy(false);
    }
  };

  if (delivery) {
    return (
      <>
        <p className="auth-note">
          {delivery === "smtp"
            ? "If that email matches an account, a reset link is on its way. It expires in 1 hour."
            : "If that email matches an account, a reset link has been created. This deployment doesn't send email — ask the operator for your link (it is recorded in the server log). It expires in 1 hour."}
        </p>
        <p className="auth-alt">
          <Link href="/login">Back to sign in</Link>
        </p>
      </>
    );
  }

  return (
    <>
      <form className="auth-form" onSubmit={submit}>
        <label className="field-label" htmlFor="reset-email">
          EMAIL
          <input
            id="reset-email"
            className="thumbnail-input"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            placeholder="you@example.com"
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
          disabled={busy || !email.trim()}
        >
          {busy ? "Requesting…" : "Request reset link"}
          {!busy && <ArrowRight size={15} strokeWidth={2} aria-hidden />}
        </button>
      </form>
      <p className="auth-alt">
        <Link href="/login">Back to sign in</Link>
      </p>
    </>
  );
}

function ConfirmForm({ token }: { token: string }) {
  const [password, setPassword] = useState("");
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await confirmPasswordReset(token, password);
      setDone(true);
    } catch (caught) {
      setError(
        caught instanceof ApiClientError
          ? caught.message
          : "The reset failed. Check your connection and try again.",
      );
      setBusy(false);
    }
  };

  if (done) {
    return (
      <>
        <p className="auth-note">
          Your password has been reset, and every existing session
          for the account has been signed out.
        </p>
        <p className="auth-alt">
          <Link href="/login">Sign in with your new password</Link>
        </p>
      </>
    );
  }

  return (
    <>
      <form className="auth-form" onSubmit={submit}>
        <label className="field-label" htmlFor="reset-new-password">
          NEW PASSWORD
          <input
            id="reset-new-password"
            className="thumbnail-input"
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        <p className="auth-note">At least 12 characters.</p>
        {error && (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}
        <button
          className="button button-primary auth-submit"
          type="submit"
          disabled={busy || !password}
        >
          {busy ? "Resetting…" : "Set new password"}
          {!busy && <ArrowRight size={15} strokeWidth={2} aria-hidden />}
        </button>
      </form>
      <p className="auth-alt">
        <Link href="/login">Back to sign in</Link>
      </p>
    </>
  );
}

function ResetPasswordInner() {
  const searchParams = useSearchParams();
  const token = searchParams.get("token");
  return (
    <AuthShell
      eyebrow="ACCOUNT RECOVERY"
      title={token ? "Set a new password" : "Reset your password"}
    >
      {token ? <ConfirmForm token={token} /> : <RequestForm />}
    </AuthShell>
  );
}

export default function ResetPasswordPage() {
  return (
    <Suspense>
      <ResetPasswordInner />
    </Suspense>
  );
}
