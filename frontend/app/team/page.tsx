"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { ErrorState, LoadingState } from "@/components/states";
import { MailPlus, UserCheck, Users } from "lucide-react";
import {
  getTeamOverview,
  inviteProjectMember,
  respondToInvitation,
  type TeamOverview,
} from "@/lib/api";

/**
 * Team Space (Product Improvements item 4) — the workspace-level
 * team page, in primary navigation. It brings together everyone
 * across the account's projects: people you work with, the
 * projects you share, invitations sent and received.
 *
 * Team membership never depends on a completed video: you can
 * invite collaborators to a brand-new project from here.
 * Membership itself is per project (the model the backend
 * enforces); each project's own Team page remains the place for
 * discussion and activity. Invitations are in-app only — no
 * email service is configured, so an invited person finds the
 * invitation in their account when they sign in, and this page
 * says so plainly.
 */

function userLabel(user: { email?: string | null; display_name?: string | null } | null | undefined) {
  if (!user) return "Unknown account";
  return user.display_name || user.email || "Unknown account";
}

function userInitials(user: { email?: string | null; display_name?: string | null } | null | undefined) {
  const label = userLabel(user).split("@")[0];
  const parts = label.split(/[\s._-]+/).filter(Boolean);
  return parts.slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "?";
}

export default function TeamSpacePage() {
  const [overview, setOverview] = useState<TeamOverview | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [inviteProject, setInviteProject] = useState("");
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("viewer");
  const [working, setWorking] = useState("");

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await getTeamOverview();
      setOverview(data);
      const firstOwned = data.projects.find((p) => p.your_role === "owner");
      setInviteProject((current) => current || firstOwned?.project_id || "");
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const ownedProjects = (overview?.projects || []).filter(
    (p) => p.your_role === "owner",
  );

  const sendInvite = async () => {
    if (!inviteProject || !inviteEmail.trim()) return;
    setWorking("invite");
    setActionError(null);
    try {
      await inviteProjectMember(inviteProject, inviteEmail.trim(), inviteRole);
      setInviteEmail("");
      setNotice(
        "Invitation sent. It will appear in that person's account when they sign in — invitations are not emailed.",
      );
      await load();
    } catch (caught) {
      setActionError(
        caught instanceof Error ? caught.message : "The invitation could not be sent.",
      );
    } finally {
      setWorking("");
    }
  };

  const respond = async (projectId: string, accept: boolean) => {
    setWorking(`respond-${projectId}`);
    setActionError(null);
    try {
      await respondToInvitation(projectId, accept);
      setNotice(accept ? "Invitation accepted." : "Invitation declined.");
      await load();
    } catch (caught) {
      setActionError(
        caught instanceof Error ? caught.message : "That response could not be recorded.",
      );
    } finally {
      setWorking("");
    }
  };

  return (
    <main className="app-page team-page">
      <AppNavigation />
      <section className="plan-content">
        <p className="eyebrow">WORKSPACE</p>
        <h1>Team Space</h1>
        <p className="plan-intro">
          Everyone who works on your documentaries, across all your projects.
          Invite collaborators any time — a project does not need a finished
          video before people can join it.
        </p>

        {busy && (
          <LoadingState className="plan-message" label="Loading your team…" />
        )}
        {!busy && error != null && (
          <ErrorState
            error={error}
            onRetry={() => {
              setBusy(true);
              void load();
            }}
          />
        )}
        {actionError && (
          <p className="team-error" role="alert">
            {actionError}
          </p>
        )}
        {notice && <p className="plan-meta">{notice}</p>}

        {!busy && !error && overview && (
          <>
            {overview.invitations_in.length > 0 && (
              <section className="panel">
                <h2>
                  <MailPlus size={16} /> Invitations for you
                </h2>
                <ul className="team-people">
                  {overview.invitations_in.map((invitation) => (
                    <li key={invitation.project_id}>
                      <span>
                        <strong>{invitation.project_title || "Untitled production"}</strong>
                        {" — invited by "}
                        {userLabel(invitation.invited_by)}
                        {" as "}
                        {invitation.role}
                      </span>
                      <span className="team-actions">
                        <button
                          type="button"
                          disabled={working === `respond-${invitation.project_id}`}
                          onClick={() => void respond(invitation.project_id, true)}
                        >
                          Accept
                        </button>
                        <button
                          type="button"
                          disabled={working === `respond-${invitation.project_id}`}
                          onClick={() => void respond(invitation.project_id, false)}
                        >
                          Decline
                        </button>
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section className="panel">
              <h2>
                <MailPlus size={16} /> Invite a collaborator
              </h2>
              {ownedProjects.length === 0 ? (
                <p className="plan-intro">
                  You do not own a project yet.{" "}
                  <Link href="/create">Create a project</Link> first — you can
                  invite people to it straight away, before any video exists.
                </p>
              ) : (
                <div className="team-invite">
                  <label>
                    Project
                    <select
                      value={inviteProject}
                      onChange={(e) => setInviteProject(e.target.value)}
                    >
                      {ownedProjects.map((project) => (
                        <option key={project.project_id} value={project.project_id}>
                          {project.project_title || "Untitled production"}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Email of an existing Cinemora account
                    <input
                      type="email"
                      value={inviteEmail}
                      onChange={(e) => setInviteEmail(e.target.value)}
                      placeholder="teammate@example.com"
                    />
                  </label>
                  <label>
                    Role
                    <select
                      value={inviteRole}
                      onChange={(e) => setInviteRole(e.target.value)}
                    >
                      <option value="viewer">Viewer</option>
                      <option value="editor">Editor</option>
                    </select>
                  </label>
                  <span className="team-actions">
                    <button
                      type="button"
                      disabled={working === "invite" || !inviteEmail.trim()}
                      onClick={() => void sendInvite()}
                    >
                      Send invitation
                    </button>
                  </span>
                </div>
              )}
              <p className="plan-meta">
                Invitations appear in the invited person&apos;s account when
                they sign in — no email is sent.
              </p>
            </section>

            <section className="panel">
              <h2>
                <Users size={16} /> Your people
              </h2>
              {overview.people.length === 0 ? (
                <p className="plan-intro">
                  No collaborators yet. Invite someone above and they will
                  appear here.
                </p>
              ) : (
                <ul className="team-people">
                  {overview.people.map((person) => (
                    <li key={person.user.user_id}>
                      <span className="team-person">
                        <span className="team-avatar" aria-hidden>
                          {userInitials(person.user)}
                        </span>
                        <span>
                          <strong>{userLabel(person.user)}</strong>
                          {person.user.email && (
                            <span className="plan-meta"> · {person.user.email}</span>
                          )}
                        </span>
                      </span>
                      <span className="team-role">
                        {person.projects.map((entry) => (
                          <span className="team-chip" key={entry.project_id}>
                            {entry.project_title || "Untitled"} · {entry.role}
                          </span>
                        ))}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section className="panel">
              <h2>
                <UserCheck size={16} /> Projects and their teams
              </h2>
              {overview.projects.length === 0 ? (
                <p className="plan-intro">
                  No projects yet — teams form around projects, and a project
                  can have a team long before it has a video.
                </p>
              ) : (
                <ul className="team-people">
                  {overview.projects.map((project) => (
                    <li key={project.project_id}>
                      <span>
                        <strong>{project.project_title || "Untitled production"}</strong>
                        <span className="plan-meta">
                          {" — you are "}
                          {project.your_role || "a member"}
                          {project.members.length > 0
                            ? ` · ${project.members.length} member${project.members.length === 1 ? "" : "s"}`
                            : " · no members yet"}
                          {project.invitations.length > 0
                            ? ` · ${project.invitations.length} invitation${project.invitations.length === 1 ? "" : "s"} pending`
                            : ""}
                        </span>
                      </span>
                      <span className="team-actions">
                        <Link href={`/projects/${project.project_id}/team`}>
                          Manage team
                        </Link>
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
