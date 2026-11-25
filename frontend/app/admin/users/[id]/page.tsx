"use client";

/** User detail: usage, suspend/reactivate, plan assignment. */

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { formatBytes, formatDateTime } from "@/components/admin-format";
import { ErrorState, LoadingState } from "@/components/states";
import {
  assignAdminUserPlan,
  generateAdminUserResetLink,
  getAdminSubscriptions,
  getAdminUser,
  setAdminUserStatus,
  type AdminUser,
} from "@/lib/api";

export default function AdminUserDetailPage() {
  const params = useParams<{ id: string }>();
  const userId = params.id;
  const [user, setUser] = useState<(AdminUser & { usage: unknown }) | null>(
    null,
  );
  const [planNames, setPlanNames] = useState<string[]>([]);
  const [selectedPlan, setSelectedPlan] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [acting, setActing] = useState(false);
  // A-16: the generated reset link lives only in this state — shown
  // once, never persisted, cleared on dismiss or on leaving.
  const [resetLink, setResetLink] = useState<string | null>(null);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const [detail, subs] = await Promise.all([
          getAdminUser(userId, signal),
          getAdminSubscriptions(signal),
        ]);
        setUser(detail);
        setPlanNames(subs.plans.map((plan) => plan.name));
        setSelectedPlan(detail.plan ?? subs.plans[0]?.name ?? "");
      } catch (cause) {
        if (!signal?.aborted) setError(cause);
      }
    },
    [userId],
  );

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => { void load(controller.signal); }, 0);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [load]);

  if (error) return <ErrorState error={error} onRetry={() => void load()} />;
  if (!user) return <LoadingState label="Loading account…" />;

  const usage = user.usage as {
    plan: { name: string } | null;
    concurrent_runs: number;
    runs_in_period: number;
    storage_bytes: number;
  };

  async function onToggleStatus() {
    if (!user) return;
    const next = user.status === "active" ? "disabled" : "active";
    if (
      next === "disabled" &&
      !window.confirm(
        `Suspend ${user.email}? Their sessions are revoked immediately; their projects and data are retained.`,
      )
    )
      return;
    setActing(true);
    setNotice(null);
    try {
      const result = await setAdminUserStatus(userId, next);
      setUser({ ...user, ...result.user });
      setNotice(
        next === "disabled"
          ? "Account suspended and sessions revoked. Recorded in the audit log."
          : "Account reactivated. Recorded in the audit log.",
      );
    } catch (cause) {
      setError(cause);
    } finally {
      setActing(false);
    }
  }

  async function onGenerateResetLink() {
    if (!user) return;
    if (
      !window.confirm(
        `Generate a one-time password reset link for ${user.email}? Any previous reset link for this account stops working. The new link is shown once — deliver it to the user out of band.`,
      )
    )
      return;
    setActing(true);
    setNotice(null);
    setResetLink(null);
    try {
      const result = await generateAdminUserResetLink(userId);
      setResetLink(result.reset_link);
      setNotice(
        "Reset link generated and recorded in the audit log. It is shown once below and expires in 1 hour.",
      );
    } catch (cause) {
      setError(cause);
    } finally {
      setActing(false);
    }
  }

  async function onAssignPlan() {
    if (!selectedPlan) return;
    setActing(true);
    setNotice(null);
    try {
      const result = await assignAdminUserPlan(userId, selectedPlan);
      setNotice(
        `${user?.email} is now on the ${result.plan} plan. No payment is collected — plans are operator-granted records. Recorded in the audit log.`,
      );
      await load();
    } catch (cause) {
      setError(cause);
    } finally {
      setActing(false);
    }
  }

  return (
    <section>
      <div className="admin-heading">
        <p className="eyebrow">ACCOUNT DETAIL</p>
        <h1>{user.email}</h1>
        <p className="admin-intro">
          Role {user.role} · status {user.status} · joined{" "}
          {formatDateTime(user.created_at)}.
        </p>
      </div>
      <dl className="admin-detail-grid">
        <div><dt>Plan</dt><dd>{user.plan ?? "—"}</dd></div>
        <div><dt>Projects</dt><dd>{user.project_count}</dd></div>
        <div><dt>Runs</dt><dd>{user.run_count}</dd></div>
        <div>
          <dt>Concurrent runs now</dt>
          <dd>{usage.concurrent_runs}</dd>
        </div>
        <div><dt>Runs this period</dt><dd>{usage.runs_in_period}</dd></div>
        <div>
          <dt>Storage used</dt>
          <dd>{formatBytes(usage.storage_bytes)}</dd>
        </div>
        <div>
          <dt>Last login</dt>
          <dd>{formatDateTime(user.last_login_at)}</dd>
        </div>
        <div>
          <dt>Email verified</dt>
          <dd>{user.email_verified_at ? formatDateTime(user.email_verified_at) : "No"}</dd>
        </div>
      </dl>
      <h2 className="admin-section-title">Account status</h2>
      <div className="admin-actions">
        <button
          type="button"
          className={
            user.status === "active" ? "button button-danger" : "button button-primary"
          }
          onClick={() => void onToggleStatus()}
          disabled={acting}
        >
          {user.status === "active" ? "Suspend account" : "Reactivate account"}
        </button>
      </div>
      <h2 className="admin-section-title">Password reset</h2>
      <p className="admin-intro">
        For a locked-out user: generate a one-time reset link and
        deliver it out of band. Email delivery is not configured —
        this operator path is the deliberate alternative.
      </p>
      <div className="admin-actions">
        <button
          type="button"
          className="button button-primary"
          onClick={() => void onGenerateResetLink()}
          disabled={acting}
        >
          Generate reset link
        </button>
        {resetLink && (
          <button
            type="button"
            className="button"
            onClick={() => setResetLink(null)}
          >
            Dismiss link
          </button>
        )}
      </div>
      {resetLink && (
        <p className="admin-notice">
          Shown once — copy it now: <code>{resetLink}</code>
        </p>
      )}
      <h2 className="admin-section-title">Plan assignment</h2>
      <div className="admin-actions">
        <label className="admin-filter">
          <span>Plan</span>
          <select
            value={selectedPlan}
            onChange={(event) => setSelectedPlan(event.target.value)}
            aria-label="Plan to assign"
          >
            {planNames.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className="button button-primary"
          onClick={() => void onAssignPlan()}
          disabled={acting || !selectedPlan || selectedPlan === user.plan}
        >
          Assign plan
        </button>
      </div>
      {notice && <p className="admin-notice">{notice}</p>}
    </section>
  );
}
