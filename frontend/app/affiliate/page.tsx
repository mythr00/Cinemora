"use client";

import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { ErrorState, LoadingState } from "@/components/states";
import { Check, Copy, HandCoins } from "lucide-react";
import {
  enrollAffiliate,
  getAffiliateDashboard,
  setAffiliatePayoutPreference,
  type AffiliateDashboard,
} from "@/lib/api";

/**
 * Affiliate programme (Product Improvements item 1).
 *
 * Founder terms: 12% LIFETIME commission on verified payments
 * by customers who registered through your link. Every number
 * here reads the real commission ledger — commission accrues
 * only when a referred customer's payment is verified by the
 * payment provider, and refunds reverse it. Payouts: no payout
 * provider is connected yet, so approved earnings are paid out
 * manually by the Cinemora team; the method selector records
 * your preference only.
 */

function money(cents: number | undefined): string {
  return `$${((cents || 0) / 100).toFixed(2)}`;
}

const PREFERENCE_LABELS: Record<string, string> = {
  bank_transfer: "Bank transfer",
  paypal: "PayPal",
  wise: "Wise",
};

export default function AffiliatePage() {
  const [dashboard, setDashboard] = useState<AffiliateDashboard | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const [working, setWorking] = useState("");
  const [copied, setCopied] = useState(false);

  const copyLink = async () => {
    if (!link) return;
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setNotice("Copy failed — select the link text and copy it manually.");
    }
  };

  const load = useCallback(async () => {
    setError(null);
    try {
      setDashboard(await getAffiliateDashboard());
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const link =
    dashboard?.enrolled && typeof window !== "undefined"
      ? `${window.location.origin}/register?ref=${dashboard.code}`
      : "";

  const enroll = async () => {
    setWorking("enroll");
    try {
      setDashboard(await enrollAffiliate());
      setNotice("You are enrolled — share your link to start earning.");
    } catch (caught) {
      setError(caught);
    } finally {
      setWorking("");
    }
  };

  const savePreference = async (preference: string) => {
    setWorking("preference");
    try {
      setDashboard(
        await setAffiliatePayoutPreference(preference || null),
      );
      setNotice("Payout preference saved.");
    } catch (caught) {
      setError(caught);
    } finally {
      setWorking("");
    }
  };

  const metrics = dashboard?.metrics;

  return (
    <main className="app-page affiliate-page">
      <AppNavigation />
      <section className="plan-content">
        <p className="eyebrow">ACCOUNT</p>
        <h1 className="affiliate-title">
          <HandCoins size={22} aria-hidden /> Affiliate programme
        </h1>
        <p className="plan-intro">
          Earn {dashboard?.rate_percent ?? 12}% lifetime commission on every
          verified payment by customers who join through your link —
          subscriptions and top-ups, for as long as they stay customers.
          Commission appears here only after the payment provider verifies
          a payment; refunds reverse it.
        </p>

        {busy && <LoadingState className="plan-message" label="Loading your affiliate dashboard…" />}
        {!busy && error != null && (
          <ErrorState
            error={error}
            onRetry={() => {
              setBusy(true);
              void load();
            }}
          />
        )}
        {notice && <p className="plan-meta">{notice}</p>}

        {!busy && !error && dashboard && !dashboard.enrolled && (
          <section className="panel">
            <p className="plan-intro">
              You are not enrolled yet. Enrolling creates your personal
              referral link and code — no approval wait, no cost.
            </p>
            <button type="button" className="button button-primary" disabled={working === "enroll"} onClick={() => void enroll()}>
              {working === "enroll" ? "Enrolling…" : "Enrol and get my link"}
            </button>
          </section>
        )}

        {!busy && !error && dashboard?.enrolled && metrics && (
          <>
            <section className="panel">
              <h2>Your referral link</h2>
              <p className="plan-meta">
                Code <strong>{dashboard.code}</strong>
                {dashboard.status !== "active"
                  ? " · This affiliate account is suspended — new referrals and commission are paused."
                  : ""}
              </p>
              <div className="affiliate-link-panel">
                <code>{link}</code>
                <button type="button" className="button button-secondary" onClick={() => void copyLink()}>
                  {copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />}
                  {copied ? "Copied" : "Copy link"}
                </button>
              </div>
              <p className="plan-meta">
                Commission rate: {dashboard.rate_percent}% lifetime ·
                Attribution: the customer is yours from registration, for
                life — if they cancel and later resubscribe, their payments
                earn for you again.
              </p>
            </section>

            <section className="panel">
              <h2>Performance</h2>
              <div className="affiliate-stats">
                <div className="affiliate-stat">
                  <span>Lifetime earnings</span>
                  <strong>{money(metrics.lifetime_earnings_cents)}</strong>
                </div>
                <div className="affiliate-stat">
                  <span>Pending review</span>
                  <strong>{money(metrics.pending_cents)}</strong>
                </div>
                <div className="affiliate-stat">
                  <span>Approved, awaiting payout</span>
                  <strong>{money(metrics.approved_cents)}</strong>
                </div>
                <div className="affiliate-stat">
                  <span>Paid out</span>
                  <strong>{money(metrics.paid_cents)}</strong>
                </div>
                <div className="affiliate-stat">
                  <span>Reversed (refunds)</span>
                  <strong>{money(metrics.reversed_cents)}</strong>
                </div>
              </div>
              <ul className="affiliate-funnel">
                <li>Recorded link visits <strong>{metrics.link_clicks}</strong></li>
                <li>Registrations <strong>{metrics.registrations}</strong></li>
                <li>Referred customers <strong>{metrics.referred_customers}</strong></li>
                <li>Currently subscribed <strong>{metrics.active_customers}</strong></li>
                <li>
                  Visit → registration{" "}
                  <strong>
                    {metrics.conversion_percent != null
                      ? `${metrics.conversion_percent}%`
                      : "—"}
                  </strong>
                </li>
              </ul>
            </section>

            <section className="panel">
              <h2>Payout method</h2>
              <p className="plan-intro">
                Payouts are arranged manually by the Cinemora team from
                approved earnings — no payout provider is connected yet.
                This selector records your preference for when payouts are
                prepared; it does not move money by itself.
              </p>
              <div className="team-invite">
                <label>
                  Preferred method
                  <select
                    value={dashboard.payout_preference || ""}
                    disabled={working === "preference"}
                    onChange={(e) => void savePreference(e.target.value)}
                  >
                    <option value="">No preference recorded</option>
                    {(dashboard.payout_preferences || []).map((option) => (
                      <option key={option} value={option}>
                        {PREFERENCE_LABELS[option] || option}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            </section>

            <section className="panel">
              <h2>Recent activity</h2>
              {(dashboard.recent_activity || []).length === 0 ? (
                <p className="plan-intro">
                  No commission yet — it appears here the moment a referred
                  customer&apos;s first payment is verified.
                </p>
              ) : (
                <ul className="team-people">
                  {(dashboard.recent_activity || []).map((entry) => (
                    <li key={entry.id}>
                      <span>
                        {entry.kind === "accrual" ? "Commission" : "Reversal"}
                        {" "}· {money(entry.commission_cents)} on{" "}
                        {money(entry.gross_cents)} payment
                      </span>
                      <span className="team-role">
                        {entry.status} · {entry.rate_percent}%
                        {entry.decision_reason ? ` · ${entry.decision_reason}` : ""}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section className="panel">
              <h2>Payout history</h2>
              {(dashboard.payout_history || []).length === 0 ? (
                <p className="plan-intro">No payouts yet.</p>
              ) : (
                <ul className="team-people">
                  {(dashboard.payout_history || []).map((entry) => (
                    <li key={entry.id}>
                      <span>{money(entry.commission_cents)}</span>
                      <span className="team-role">
                        paid · {entry.created_at?.slice(0, 10) || ""}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </>
        )}
      </section>
    </main>
  );
}
