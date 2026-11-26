"use client";

/**
 * Affiliate admin (item 1): review commission entries
 * (approve / reject-with-reason / mark paid), suspend or
 * reinstate affiliates, and export the ledger CSV — the payout
 * worksheet until a payout provider is chosen. Every mutation
 * is super_admin-only and audit-logged on the server.
 */

import { useCallback, useEffect, useState } from "react";
import { AuthenticatedDownloadLink } from "@/components/authenticated-media";
import { ErrorState, LoadingState } from "@/components/states";
import {
  adminAffiliateExportPath,
  decideAffiliateEntry,
  getAdminAffiliateEntries,
  getAdminAffiliates,
  setAdminAffiliateStatus,
  type AdminAffiliateRow,
  type AffiliateEntry,
} from "@/lib/api";

function money(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}

export default function AdminAffiliatesPage() {
  const [affiliates, setAffiliates] = useState<AdminAffiliateRow[]>([]);
  const [entries, setEntries] = useState<AffiliateEntry[]>([]);
  const [rate, setRate] = useState(12);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const [reasons, setReasons] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    setError(null);
    try {
      const [overview, ledger] = await Promise.all([
        getAdminAffiliates(),
        getAdminAffiliateEntries(),
      ]);
      setAffiliates(overview.affiliates || []);
      setRate(overview.rate_percent);
      setEntries(ledger.entries || []);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const decide = async (
    entry: AffiliateEntry,
    decision: "approve" | "reject" | "paid",
  ) => {
    try {
      await decideAffiliateEntry(
        entry.id,
        decision,
        decision === "reject" ? reasons[entry.id] : undefined,
      );
      setNotice(`Entry ${decision === "paid" ? "marked paid" : decision + "d"}.`);
      await load();
    } catch (caught) {
      setError(caught);
    }
  };

  const toggleStatus = async (row: AdminAffiliateRow) => {
    try {
      await setAdminAffiliateStatus(
        row.id,
        row.status === "active" ? "suspended" : "active",
      );
      setNotice(`Affiliate ${row.code} is now ${row.status === "active" ? "suspended" : "active"}.`);
      await load();
    } catch (caught) {
      setError(caught);
    }
  };

  return (
    <main className="app-page">
      <section className="plan-content">
        <p className="eyebrow">ADMIN</p>
        <h1>Affiliates</h1>
        <p className="plan-intro">
          Commission rate {rate}% lifetime, accrued only from verified
          payments. No payout provider is integrated — prepare payouts
          from the CSV export and mark entries paid here once money has
          actually been sent.
        </p>
        {busy && <LoadingState className="plan-message" label="Loading affiliates…" />}
        {!busy && error != null && <ErrorState error={error} onRetry={() => { setBusy(true); void load(); }} />}
        {notice && <p className="plan-meta">{notice}</p>}

        {!busy && !error && (
          <>
            <p>
              <AuthenticatedDownloadLink path={adminAffiliateExportPath()} filename="affiliate-ledger.csv">
                Export ledger CSV
              </AuthenticatedDownloadLink>
            </p>
            <section>
              <h2>Affiliates</h2>
              {affiliates.length === 0 ? (
                <p className="plan-intro">No affiliates enrolled yet.</p>
              ) : (
                <ul className="team-people">
                  {affiliates.map((row) => (
                    <li key={row.id}>
                      <span>
                        <strong>{row.code}</strong> · {row.email || row.user_id}
                        <span className="plan-meta">
                          {" "}· accrued {money(row.accrued_cents)} · reversed{" "}
                          {money(row.reversed_cents)} · pending{" "}
                          {money(row.pending_cents)}
                          {row.payout_preference
                            ? ` · prefers ${row.payout_preference}`
                            : ""}
                        </span>
                      </span>
                      <span className="team-actions">
                        <span className="team-role">{row.status}</span>
                        <button type="button" onClick={() => void toggleStatus(row)}>
                          {row.status === "active" ? "Suspend" : "Reinstate"}
                        </button>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section>
              <h2>Commission ledger</h2>
              {entries.length === 0 ? (
                <p className="plan-intro">No commission entries yet.</p>
              ) : (
                <ul className="team-people">
                  {entries.map((entry) => (
                    <li key={entry.id}>
                      <span>
                        {entry.kind} · {money(entry.commission_cents)} on{" "}
                        {money(entry.gross_cents)} · {entry.rate_percent}%
                        <span className="plan-meta">
                          {" "}· {entry.created_at?.slice(0, 10) || ""}
                        </span>
                      </span>
                      <span className="team-actions">
                        <span className="team-role">{entry.status}</span>
                        {entry.kind === "accrual" && entry.status === "pending" && (
                          <>
                            <button type="button" onClick={() => void decide(entry, "approve")}>
                              Approve
                            </button>
                            <input
                              aria-label="Rejection reason"
                              placeholder="Reason (required to reject)"
                              value={reasons[entry.id] || ""}
                              onChange={(e) =>
                                setReasons({ ...reasons, [entry.id]: e.target.value })
                              }
                            />
                            <button
                              type="button"
                              disabled={!reasons[entry.id]}
                              onClick={() => void decide(entry, "reject")}
                            >
                              Reject
                            </button>
                          </>
                        )}
                        {entry.kind === "accrual" && entry.status === "approved" && (
                          <button type="button" onClick={() => void decide(entry, "paid")}>
                            Mark paid
                          </button>
                        )}
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
