"use client";

/** Subscriptions & plans — read-only overview; records only, no
 * payment collection.
 *
 * Phase 6 (A-15): no backend endpoint creates or edits plans or
 * subscriptions, so this page is labelled honestly as a read-only
 * overview. The one real management action — assigning a plan to
 * a user — lives on the user detail page, where it is wired to
 * POST /admin/users/{id}/plan. */

import { useCallback, useEffect, useState } from "react";
import { formatBytes, formatDateTime } from "@/components/admin-format";
import { ErrorState, LoadingState } from "@/components/states";
import {
  getAdminBillingPlans,
  getAdminBillingReports,
  getAdminBillingRetention,
  getAdminSubscriptions,
  retryAdminRetention,
  updateAdminBillingPlan,
  type AdminBillingReports,
  type AdminRetentionSummary,
  type AdminSubscriptions,
  type BillingPlan,
} from "@/lib/api";

function limit(value: number | null): string {
  return value === null ? "Unlimited" : String(value);
}

export default function AdminSubscriptionsPage() {
  const [data, setData] = useState<AdminSubscriptions | null>(null);
  const [error, setError] = useState<unknown>(null);
  // Phase 8 MON (§12): the monetization dashboard — commercial
  // plans, financial reports and the §21 retention registry.
  const [billingPlans, setBillingPlans] = useState<BillingPlan[]>([]);
  const [reports, setReports] = useState<AdminBillingReports | null>(null);
  const [retention, setRetention] = useState<AdminRetentionSummary | null>(null);
  const [planNote, setPlanNote] = useState<string | null>(null);
  const [priceEdits, setPriceEdits] = useState<Record<string, string>>({});
  const [referenceEdits, setReferenceEdits] = useState<Record<string, string>>({});

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setData(await getAdminSubscriptions(signal));
    } catch (cause) {
      if (!signal?.aborted) setError(cause);
    }
    // Monetization sections are best-effort: a failure leaves
    // them empty rather than breaking the overview.
    getAdminBillingPlans(signal)
      .then((result) => setBillingPlans(result.plans))
      .catch(() => undefined);
    getAdminBillingReports(signal).then(setReports).catch(() => undefined);
    getAdminBillingRetention(signal)
      .then(setRetention)
      .catch(() => undefined);
  }, []);

  const savePrice = async (plan: BillingPlan) => {
    const raw = priceEdits[plan.id];
    const cents = raw === undefined ? null : Math.round(Number(raw) * 100);
    if (cents === null || Number.isNaN(cents) || cents < 0) {
      setPlanNote("Enter a valid monthly price.");
      return;
    }
    try {
      const updated = await updateAdminBillingPlan(plan.id, {
        price_monthly_cents: cents,
      });
      setBillingPlans((current) =>
        current.map((p) => (p.id === updated.id ? updated : p)),
      );
      setPlanNote(`Price updated for ${updated.name}.`);
    } catch (cause) {
      setPlanNote(
        cause instanceof Error
          ? cause.message
          : "Price update failed (super-admin required).",
      );
    }
  };

  // Reference (strikethrough) price: stored on the plan record's
  // features and shown on pricing surfaces only when set and
  // above the selling price. Enter ONLY a genuine, founder-
  // approved standard price — never an invented anchor. Blank
  // clears it and disables the strikethrough.
  const saveReferencePrice = async (plan: BillingPlan) => {
    const raw = referenceEdits[plan.id];
    if (raw === undefined) return;
    const features: Record<string, unknown> = { ...(plan.features ?? {}) };
    if (raw.trim() === "") {
      delete features.reference_price_monthly_cents;
    } else {
      const cents = Math.round(Number(raw) * 100);
      if (Number.isNaN(cents) || cents < 0) {
        setPlanNote("Enter a valid reference price, or leave it blank to disable.");
        return;
      }
      features.reference_price_monthly_cents = cents;
    }
    try {
      const updated = await updateAdminBillingPlan(plan.id, { features });
      setBillingPlans((current) =>
        current.map((p) => (p.id === updated.id ? updated : p)),
      );
      setPlanNote(`Reference price updated for ${updated.name}.`);
    } catch (cause) {
      setPlanNote(
        cause instanceof Error
          ? cause.message
          : "Reference price update failed (super-admin required).",
      );
    }
  };

  const retryRetention = async (videoId: string) => {
    await retryAdminRetention(videoId).catch(() => undefined);
    getAdminBillingRetention()
      .then(setRetention)
      .catch(() => undefined);
  };

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => { void load(controller.signal); }, 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [load]);

  if (error) return <ErrorState error={error} onRetry={() => void load()} />;
  if (!data) return <LoadingState label="Loading subscriptions…" />;

  return (
    <section>
      <div className="admin-heading">
        <p className="eyebrow">SUBSCRIPTIONS, PLANS &amp; MONETIZATION</p>
        <h1>Plans and who holds them.</h1>
        <p className="admin-intro">{data.billing_note}</p>
        <p className="admin-intro">
          Plan limits below are the subscription records. Commercial
          values (prices, credits, allowances) are managed in the
          Monetization section — super-admin only, every change
          audit-logged. To move a user to another plan, open their
          account from the Users page and use Plan assignment.
        </p>
      </div>
      <h2 className="admin-section-title">Plans</h2>
      <table className="admin-table">
        <thead>
          <tr>
            <th>Plan</th>
            <th>Concurrent runs</th>
            <th>Runs / period</th>
            <th>Script chars</th>
            <th>Storage</th>
            <th>Upload</th>
            <th>Priority tier</th>
            <th>Active subscribers</th>
          </tr>
        </thead>
        <tbody>
          {data.plans.map((plan) => (
            <tr key={plan.id}>
              <td>
                {plan.name}
                {plan.is_default ? " (default)" : ""}
              </td>
              <td>{plan.max_concurrent_runs}</td>
              <td>{limit(plan.max_runs_per_period)}</td>
              <td>{limit(plan.max_script_chars)}</td>
              <td>
                {plan.max_storage_bytes === null
                  ? "Unlimited"
                  : formatBytes(plan.max_storage_bytes)}
              </td>
              <td>
                {plan.max_upload_bytes === null
                  ? "Unlimited"
                  : formatBytes(plan.max_upload_bytes)}
              </td>
              <td>{plan.priority_tier}</td>
              <td>{plan.active_subscribers}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h2 className="admin-section-title">Subscriptions</h2>
      {data.subscriptions.length === 0 ? (
        <p className="admin-empty">No subscriptions recorded.</p>
      ) : (
        <table className="admin-table">
          <thead>
            <tr>
              <th>User</th>
              <th>Plan</th>
              <th>Status</th>
              <th>Period start</th>
              <th>Period end</th>
              <th>Billing provider</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {data.subscriptions.map((sub) => (
              <tr key={sub.id}>
                <td>{sub.user_email ?? sub.user_id}</td>
                <td>{sub.plan ?? "—"}</td>
                <td>{sub.status}</td>
                <td>{formatDateTime(sub.current_period_start)}</td>
                <td>{formatDateTime(sub.current_period_end)}</td>
                <td>{sub.billing_provider ?? "—"}</td>
                <td>{formatDateTime(sub.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h2 className="admin-section-title">Monetization</h2>
      {reports && (
        <table className="admin-table">
          <tbody>
            <tr>
              <td>Contracted MRR / ARR</td>
              <td>
                ${(reports.mrr_contracted_cents / 100).toFixed(2)} / $
                {(reports.arr_contracted_cents / 100).toFixed(2)}
              </td>
            </tr>
            <tr>
              <td>Verified revenue (all time)</td>
              <td>
                ${(reports.revenue.total_verified_cents / 100).toFixed(2)}
              </td>
            </tr>
            <tr>
              <td>Credits outstanding (sub / purchased / promo)</td>
              <td>
                {reports.credits.outstanding.subscription} /{" "}
                {reports.credits.outstanding.purchased} /{" "}
                {reports.credits.outstanding.promo}
              </td>
            </tr>
            <tr>
              <td>Production cost</td>
              <td>
                {typeof reports.production_cost === "object" &&
                reports.production_cost !== null &&
                "status" in reports.production_cost &&
                reports.production_cost.status === "unknown"
                  ? "Unknown — no pricing rules configured"
                  : "See the billing reports API for the priced rollup"}
              </td>
            </tr>
          </tbody>
        </table>
      )}
      <p className="admin-intro">
        Reference price: the strikethrough shown beside a plan&apos;s
        price on the billing page. Enter only a genuine, approved
        standard price for that plan — never an invented anchor.
        Leave it blank to disable the strikethrough.
      </p>
      {planNote && <p className="admin-intro">{planNote}</p>}
      <table className="admin-table">
        <thead>
          <tr>
            <th>Plan</th>
            <th>Mode</th>
            <th>Monthly price</th>
            <th>Reference price</th>
            <th>Credits / month</th>
            <th>Included generations</th>
            <th>Script / thumbnail allowance</th>
            <th>Public</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {billingPlans.map((plan) => (
            <tr key={plan.id}>
              <td>{plan.display_name || plan.name}</td>
              <td>{plan.billing_mode}</td>
              <td>
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  defaultValue={
                    plan.price_monthly_cents !== null
                      ? (plan.price_monthly_cents / 100).toFixed(2)
                      : ""
                  }
                  onChange={(event) =>
                    setPriceEdits((current) => ({
                      ...current,
                      [plan.id]: event.target.value,
                    }))
                  }
                  aria-label={`Monthly price for ${plan.name} in dollars`}
                />
              </td>
              <td>
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  defaultValue={
                    typeof plan.features?.reference_price_monthly_cents === "number"
                      ? (plan.features.reference_price_monthly_cents / 100).toFixed(2)
                      : ""
                  }
                  placeholder="—"
                  onChange={(event) =>
                    setReferenceEdits((current) => ({
                      ...current,
                      [plan.id]: event.target.value,
                    }))
                  }
                  aria-label={`Reference price for ${plan.name} in dollars (blank disables the strikethrough)`}
                />
              </td>
              <td>{plan.monthly_credits}</td>
              <td>{plan.included_generations}</td>
              <td>
                {plan.script_allowance} / {plan.thumbnail_allowance}
              </td>
              <td>{plan.is_public ? "Yes" : "No"}</td>
              <td>
                <button
                  type="button"
                  className="button button-quiet"
                  onClick={() => void savePrice(plan)}
                >
                  Save price
                </button>{" "}
                <button
                  type="button"
                  className="button button-quiet"
                  onClick={() => void saveReferencePrice(plan)}
                >
                  Save reference
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2 className="admin-section-title">Generated-video retention (§21)</h2>
      {retention ? (
        <>
          <table className="admin-table">
            <tbody>
              <tr>
                <td>Registered videos</td>
                <td>
                  {retention.total_videos} ({formatBytes(retention.total_bytes)})
                </td>
              </tr>
              <tr>
                <td>Active / deleted</td>
                <td>
                  {retention.active} / {retention.deleted}
                </td>
              </tr>
              <tr>
                <td>Expired awaiting deletion</td>
                <td>{retention.expired_awaiting_deletion}</td>
              </tr>
              <tr>
                <td>Failed deletions</td>
                <td>{retention.delete_failed}</td>
              </tr>
            </tbody>
          </table>
          {retention.failures.length > 0 && (
            <table className="admin-table">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Attempts</th>
                  <th>Last error</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {retention.failures.map((failure) => (
                  <tr key={failure.id}>
                    <td>{failure.filename}</td>
                    <td>{failure.delete_attempts}</td>
                    <td>{failure.last_error ?? "—"}</td>
                    <td>
                      <button
                        type="button"
                        className="button button-quiet"
                        onClick={() => void retryRetention(failure.id)}
                      >
                        Retry
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      ) : (
        <p className="admin-empty">Retention registry unavailable.</p>
      )}
    </section>
  );
}
