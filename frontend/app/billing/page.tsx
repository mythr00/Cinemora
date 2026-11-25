"use client";

/**
 * Billing & credits (Phase 8 MON, directive §14).
 *
 * Everything on this page is the server's own record: plan,
 * subscription state, period allowances, the three credit
 * buckets, the immutable ledger and verified payments. Checkout
 * runs through the configured payment provider (Paddle); when
 * the provider is not configured the page says so plainly and
 * offers no purchase buttons — benefits are only ever granted
 * by the provider's verified webhook, never by this page.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { ErrorState, LoadingState } from "@/components/states";
import {
  cancelSubscription,
  changeSubscription,
  getBillingPayments,
  getBillingSummary,
  getBillingTransactions,
  getNotifications,
  markNotificationRead,
  startSubscriptionCheckout,
  startTopupCheckout,
  type BillingPayment,
  type BillingSummary,
  type LedgerEntry,
  type UserNotification,
} from "@/lib/api";
import { Bell, Check, CreditCard, Wallet } from "lucide-react";

type PaddleWindow = Window & {
  Paddle?: {
    Environment: { set: (env: string) => void };
    Initialize: (options: { token: string }) => void;
    Checkout: { open: (options: { transactionId: string }) => void };
  };
};

function money(cents: number | null, currency = "USD"): string {
  if (cents === null) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
  }).format(cents / 100);
}

function dateTime(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString();
}

export default function BillingPage() {
  const [summary, setSummary] = useState<BillingSummary | null>(null);
  const [entries, setEntries] = useState<LedgerEntry[]>([]);
  const [payments, setPayments] = useState<BillingPayment[]>([]);
  const [notifications, setNotifications] = useState<UserNotification[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [interval, setInterval] = useState<"monthly" | "annual">("monthly");
  const paddleReady = useRef(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const [sum, tx, pay, notes] = await Promise.all([
        getBillingSummary(signal),
        getBillingTransactions(signal).catch(() => ({ entries: [] })),
        getBillingPayments(signal).catch(() => ({ payments: [] })),
        getNotifications(signal).catch(() => ({
          notifications: [],
          unread_count: 0,
        })),
      ]);
      setSummary(sum);
      setEntries(tx.entries);
      setPayments(pay.payments);
      setNotifications(notes.notifications);
    } catch (cause) {
      if (!signal?.aborted) setError(cause);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  // Load Paddle.js only when the provider is configured.
  useEffect(() => {
    const provider = summary?.payment_provider;
    if (!provider?.configured || !provider.public_config.client_token) return;
    if (paddleReady.current) return;
    const script = document.createElement("script");
    script.src = "https://cdn.paddle.com/paddle/paddle.js";
    script.async = true;
    script.onload = () => {
      const paddle = (window as PaddleWindow).Paddle;
      if (!paddle) return;
      if (provider.public_config.environment === "sandbox") {
        paddle.Environment.set("sandbox");
      }
      paddle.Initialize({ token: provider.public_config.client_token! });
      paddleReady.current = true;
    };
    document.body.appendChild(script);
  }, [summary]);

  const openCheckout = (transactionId: string) => {
    const paddle = (window as PaddleWindow).Paddle;
    if (paddle && paddleReady.current) {
      paddle.Checkout.open({ transactionId });
      setNotice(
        "Checkout opened. Your plan and credits update automatically " +
          "once the payment provider confirms the payment.",
      );
    } else {
      setNotice(
        "Checkout was created but the payment window could not be " +
          "opened. Reload the page and try again.",
      );
    }
  };

  const subscribe = async (planName: string) => {
    setBusy(planName);
    setNotice(null);
    try {
      if (summary?.subscription) {
        const result = await changeSubscription(planName, interval);
        setNotice(
          result.effective === "immediate"
            ? "Plan upgraded. The credit difference for this period has been added."
            : "Plan change scheduled — it takes effect at the end of the current period. Nothing is deleted.",
        );
        await load();
      } else {
        const checkout = await startSubscriptionCheckout(planName, interval);
        openCheckout(checkout.transaction_id);
      }
    } catch (cause) {
      setNotice(
        cause instanceof Error ? cause.message : "The change failed.",
      );
    } finally {
      setBusy(null);
    }
  };

  const buyTopup = async (packageId: string) => {
    setBusy(packageId);
    setNotice(null);
    try {
      const checkout = await startTopupCheckout(packageId);
      openCheckout(checkout.transaction_id);
    } catch (cause) {
      setNotice(
        cause instanceof Error ? cause.message : "Checkout failed.",
      );
    } finally {
      setBusy(null);
    }
  };

  const cancel = async () => {
    setBusy("cancel");
    try {
      const result = await cancelSubscription();
      setNotice(
        `Cancellation scheduled. Access continues until ${dateTime(result.current_period_end)}. Your projects, scripts and history are kept.`,
      );
      await load();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "Cancel failed.");
    } finally {
      setBusy(null);
    }
  };

  const readNotification = async (id: string) => {
    await markNotificationRead(id).catch(() => undefined);
    setNotifications((current) =>
      current.map((n) => (n.id === id ? { ...n, read_at: new Date().toISOString() } : n)),
    );
  };

  if (error) {
    return (
      <main className="app-page">
        <AppNavigation />
        <ErrorState error={error} onRetry={() => void load()} />
      </main>
    );
  }
  if (!summary) {
    return (
      <main className="app-page">
        <AppNavigation />
        <LoadingState label="Loading billing…" />
      </main>
    );
  }

  const provider = summary.payment_provider;
  const plan = summary.plan;
  const sub = summary.subscription;
  const period = summary.period;
  const wallet = summary.wallet;

  return (
    <main className="app-page billing-page">
      <AppNavigation />
      <section className="projects-content">
        <div className="projects-heading">
          <div>
            <p className="eyebrow">BILLING</p>
            <h1>Billing &amp; credits</h1>
          </div>
        </div>

        {!provider.configured && (
          <div className="panel billing-note">
            <CreditCard size={18} strokeWidth={1.8} aria-hidden />
            <p>
              Payments are not configured yet, so checkout is
              unavailable. Your current plan and credits are shown
              below exactly as recorded.
            </p>
          </div>
        )}
        {notice && <p className="billing-notice">{notice}</p>}

        <div className="panel">
          <h2 className="admin-section-title">
            <Wallet size={16} strokeWidth={2} aria-hidden /> Current plan
          </h2>
          <p className="billing-plan-name">
            {plan ? plan.display_name || plan.name : "No plan"}
            {sub ? ` · ${sub.status}` : ""}
            {sub?.billing_interval ? ` · billed ${sub.billing_interval}` : ""}
          </p>
          {sub?.cancel_at_period_end && (
            <p className="billing-warn">
              Cancellation scheduled — access continues until{" "}
              {dateTime(sub.current_period_end)}. Your content is kept.
            </p>
          )}
          {sub?.pending_plan_name && (
            <p>
              Scheduled plan change: {sub.pending_plan_name} at the
              end of the current period.
            </p>
          )}
          {period && (
            <div className="billing-allowances">
              <div>
                <span>Documentary generations</span>
                <strong>
                  {period.generations_used} of {period.generations_included} used
                  {" · "}{period.generations_remaining} remaining
                </strong>
              </div>
              <div>
                <span>Script operations</span>
                <strong>
                  {period.script_ops_used} of {period.script_allowance} used
                  {" · "}{period.script_remaining} remaining
                </strong>
              </div>
              <div>
                <span>Thumbnail operations</span>
                <strong>
                  {period.thumbnail_ops_used} of {period.thumbnail_allowance} used
                  {" · "}{period.thumbnail_remaining} remaining
                </strong>
              </div>
              <div>
                <span>Period ends</span>
                <strong>{dateTime(period.period_end)}</strong>
              </div>
            </div>
          )}
          <div className="billing-allowances">
            <div>
              <span>Subscription credits</span>
              <strong>{wallet.subscription.available} available</strong>
            </div>
            <div>
              <span>Purchased credits</span>
              <strong>
                {wallet.purchased.available} available
                {wallet.purchased.next_expiry_at
                  ? ` · next expiry ${dateTime(wallet.purchased.next_expiry_at)}`
                  : ""}
              </strong>
            </div>
            <div>
              <span>Promotional credits</span>
              <strong>{wallet.promo.available} available</strong>
            </div>
            <div>
              <span>Reserved for running generations</span>
              <strong>
                {wallet.subscription.reserved + wallet.purchased.reserved + wallet.promo.reserved}
              </strong>
            </div>
          </div>
          {sub && !sub.cancel_at_period_end && (
            <button
              type="button"
              className="button button-quiet"
              onClick={() => void cancel()}
              disabled={busy !== null}
            >
              {busy === "cancel" ? "Scheduling…" : "Cancel subscription"}
            </button>
          )}
        </div>

        <div className="panel">
          <h2 className="admin-section-title">Plans</h2>
          <div className="billing-interval">
            <button
              type="button"
              className={interval === "monthly" ? "button button-primary" : "button button-quiet"}
              onClick={() => setInterval("monthly")}
            >
              Monthly
            </button>
            <button
              type="button"
              className={interval === "annual" ? "button button-primary" : "button button-quiet"}
              onClick={() => setInterval("annual")}
            >
              Annual — 20% off
            </button>
          </div>
          <div className="billing-plans">
            {summary.available_plans.map((candidate) => {
              const cents =
                interval === "annual"
                  ? candidate.price_annual_cents
                  : candidate.price_monthly_cents;
              const isCurrent = plan?.id === candidate.id;
              return (
                <article key={candidate.id} className="billing-plan-card">
                  <h3>{candidate.display_name || candidate.name}</h3>
                  <p className="billing-price">
                    {money(cents, candidate.currency)}
                    <span>{interval === "annual" ? " / year" : " / month"}</span>
                  </p>
                  <ul>
                    <li>
                      <Check size={13} aria-hidden /> {candidate.monthly_credits} credits monthly
                    </li>
                    <li>
                      <Check size={13} aria-hidden /> {candidate.included_generations} included
                      15-minute documentaries
                    </li>
                    <li>
                      <Check size={13} aria-hidden /> {candidate.script_allowance} script
                      operations · {candidate.thumbnail_allowance} thumbnail operations
                    </li>
                    <li>
                      <Check size={13} aria-hidden /> Same production engine and quality on every plan
                    </li>
                  </ul>
                  <button
                    type="button"
                    className="button button-primary"
                    disabled={isCurrent || busy !== null || !provider.configured}
                    onClick={() => void subscribe(candidate.name)}
                  >
                    {isCurrent
                      ? "Current plan"
                      : busy === candidate.name
                        ? "Working…"
                        : sub
                          ? "Switch to this plan"
                          : "Subscribe"}
                  </button>
                </article>
              );
            })}
          </div>
          <p className="billing-footnote">
            A standard generation is up to 15 minutes and uses one
            included generation. Longer generations use an included
            generation plus credits for the extra minutes at{" "}
            {summary.credit_pricing.standard_generation_credits} credits
            per 15 minutes. Failed generations are always fully
            restored — credits and included generations.
          </p>
        </div>

        <div className="panel">
          <h2 className="admin-section-title">Credit top-ups</h2>
          <div className="billing-plans">
            {summary.topup_packages.map((pack) => (
              <article key={pack.id} className="billing-plan-card">
                <h3>{pack.name}</h3>
                <p className="billing-price">
                  {money(pack.price_cents, pack.currency)}
                </p>
                <p>{pack.credits} credits · valid 12 months</p>
                <button
                  type="button"
                  className="button button-secondary"
                  disabled={busy !== null || !provider.configured}
                  onClick={() => void buyTopup(pack.id)}
                >
                  {busy === pack.id ? "Working…" : "Buy credits"}
                </button>
              </article>
            ))}
          </div>
        </div>

        {notifications.length > 0 && (
          <div className="panel">
            <h2 className="admin-section-title">
              <Bell size={16} strokeWidth={2} aria-hidden /> Notifications
            </h2>
            <ul className="billing-notifications">
              {notifications.map((note) => (
                <li key={note.id} className={note.read_at ? "" : "unread"}>
                  <div>
                    <strong>{note.title}</strong>
                    {note.body && <p>{note.body}</p>}
                    <span>{dateTime(note.created_at)}</span>
                  </div>
                  {!note.read_at && (
                    <button
                      type="button"
                      className="button button-quiet"
                      onClick={() => void readNotification(note.id)}
                    >
                      Mark read
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="panel">
          <h2 className="admin-section-title">Credit history</h2>
          {entries.length === 0 ? (
            <p>No credit activity recorded yet.</p>
          ) : (
            <table className="admin-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Activity</th>
                  <th>Bucket</th>
                  <th>Credits</th>
                  <th>Balance after</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => (
                  <tr key={entry.id}>
                    <td>{dateTime(entry.created_at)}</td>
                    <td>{entry.description || entry.entry_type}</td>
                    <td>{entry.bucket}</td>
                    <td>{entry.credits_delta > 0 ? `+${entry.credits_delta}` : entry.credits_delta}</td>
                    <td>{entry.balance_after ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <div className="panel">
          <h2 className="admin-section-title">Payments</h2>
          {payments.length === 0 ? (
            <p>No payments recorded.</p>
          ) : (
            <table className="admin-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Description</th>
                  <th>Amount</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {payments.map((payment) => (
                  <tr key={payment.id}>
                    <td>{dateTime(payment.created_at)}</td>
                    <td>{payment.description || payment.kind}</td>
                    <td>{money(payment.amount_cents, payment.currency)}</td>
                    <td>{payment.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>
    </main>
  );
}
