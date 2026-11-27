"use client";

/**
 * Billing & credits (founder structure, Master Product
 * Redesign directive §3, 2026-11-26). This page shows the
 * account's own record only — current plan, the credit
 * buckets, plan allowances, the ledger and verified
 * payments — in the founder's dictated section order. It
 * displays NO prices: plan prices live on the pricing page,
 * and checkout runs through the configured payment provider
 * (Paddle); when the provider is not configured the page
 * says so plainly and offers no purchase actions. Benefits
 * are only ever granted by the provider's verified webhook,
 * never by this page.
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
  startSubscriptionCheckout,
  startTopupCheckout,
  type BillingPayment,
  type BillingSummary,
  type LedgerEntry,
} from "@/lib/api";
import { Check, CreditCard, Wallet } from "lucide-react";

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
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [interval, setInterval] = useState<"monthly" | "annual">("monthly");
  const paddleReady = useRef(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const [sum, tx, pay] = await Promise.all([
        getBillingSummary(signal),
        getBillingTransactions(signal).catch(() => ({ entries: [] })),
        getBillingPayments(signal).catch(() => ({ payments: [] })),
      ]);
      setSummary(sum);
      setEntries(tx.entries);
      setPayments(pay.payments);
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
  const reservedTotal =
    wallet.subscription.reserved +
    wallet.purchased.reserved +
    wallet.promo.reserved;

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
                <span>Documentaries delivered</span>
                <strong>
                  {period.generations.used} of {period.generations.granted} used
                  {" · "}{period.generations.remaining} remaining
                </strong>
              </div>
              <div>
                <span>AI script operations</span>
                <strong>
                  {period.scripts.used} of {period.scripts.granted} used
                  {" · "}{period.scripts.remaining} remaining
                </strong>
              </div>
              <div>
                <span>AI thumbnail operations</span>
                <strong>
                  {period.thumbnails.used} of {period.thumbnails.granted} used
                  {" · "}{period.thumbnails.remaining} remaining
                </strong>
              </div>
              <div>
                <span>Period ends</span>
                <strong>{dateTime(period.period_end)}</strong>
              </div>
            </div>
          )}
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
          <h2 className="admin-section-title">Credits</h2>
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
              <strong>{reservedTotal}</strong>
            </div>
          </div>
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
              const isCurrent = plan?.id === candidate.id;
              return (
                <article key={candidate.id} className="billing-plan-card">
                  <h3>{candidate.display_name || candidate.name}</h3>
                  <ul>
                    <li>
                      <Check size={13} aria-hidden /> {candidate.monthly_credits} video credits monthly
                    </li>
                    <li>
                      <Check size={13} aria-hidden /> {candidate.included_generations} standard
                      15-minute documentaries / month
                    </li>
                    <li>
                      <Check size={13} aria-hidden /> {candidate.script_allowance} AI scripts ·{" "}
                      {candidate.thumbnail_allowance} AI thumbnails included
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
          <div className="billing-footnote">
            <p>
              Generations are charged for the finished minutes you
              actually receive, at your plan&apos;s per-minute rate —
              never for internal steps, and never for undelivered
              duration.
            </p>
            <p>
              Each delivered video also counts as one toward your
              plan&apos;s monthly documentary count, whatever its
              length.
            </p>
            <p>
              Script and thumbnail allowances are tracked separately
              and never use video credits; beyond them, a script costs{" "}
              {summary.credit_pricing.script_operations.draft ?? 3} credits
              and a thumbnail{" "}
              {summary.credit_pricing.thumbnail_operations.compose ?? 5} credits.
            </p>
            <p>
              Failed generations are always fully restored — credits
              and the monthly count.
            </p>
          </div>
        </div>

        <div className="panel">
          <h2 className="admin-section-title">Credit top-ups</h2>
          <div className="billing-plans">
            {summary.topup_packages.map((pack) => (
              <article key={pack.id} className="billing-plan-card">
                <h3>{pack.name}</h3>
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
