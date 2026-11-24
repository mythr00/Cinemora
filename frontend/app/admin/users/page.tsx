"use client";

/** Users & accounts — search and inspect (Phase 7 on Phase 8 data). */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { formatDateTime } from "@/components/admin-format";
import { ErrorState, LoadingState } from "@/components/states";
import { listAdminUsers, type AdminUser } from "@/lib/api";
import { Search } from "lucide-react";

export default function AdminUsersPage() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [total, setTotal] = useState(0);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const load = useCallback(
    async (q: string, signal?: AbortSignal) => {
      setBusy(true);
      try {
        const data = await listAdminUsers(q || undefined, signal);
        setUsers(data.users);
        setTotal(data.total);
      } catch (cause) {
        if (!signal?.aborted) setError(cause);
      } finally {
        if (!signal?.aborted) setBusy(false);
      }
    },
    [],
  );

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => void load(query, controller.signal), 200);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [load, query]);

  return (
    <section>
      <div className="admin-heading">
        <p className="eyebrow">USERS & ACCOUNTS</p>
        <h1>Every account on the platform.</h1>
        <p className="admin-intro">
          Accounts, roles, plans and activity — from the accounts
          tables. Suspending an account revokes its sessions at
          once; its data is retained.
        </p>
      </div>
      <div className="admin-toolbar">
        <span>{busy ? "Loading users…" : `${total} accounts`}</span>
        <label className="admin-search">
          <Search size={14} strokeWidth={2} aria-hidden />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search by email"
            aria-label="Search users by email"
          />
        </label>
      </div>
      {!!error && <ErrorState error={error} onRetry={() => void load(query)} />}
      {busy && !error && <LoadingState label="Loading users…" />}
      {!busy && !error && users.length === 0 && (
        <p className="admin-empty">No accounts match.</p>
      )}
      {!busy && !error && users.length > 0 && (
        <table className="admin-table">
          <thead>
            <tr>
              <th>Email</th>
              <th>Role</th>
              <th>Status</th>
              <th>Plan</th>
              <th>Projects</th>
              <th>Runs</th>
              <th>Last login</th>
            </tr>
          </thead>
          <tbody>
            {users.map((user) => (
              <tr key={user.id}>
                <td>
                  <Link href={`/admin/users/${user.id}`}>{user.email}</Link>
                </td>
                <td>{user.role}</td>
                <td>{user.status}</td>
                <td>{user.plan ?? "—"}</td>
                <td>{user.project_count}</td>
                <td>{user.run_count}</td>
                <td>{formatDateTime(user.last_login_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
