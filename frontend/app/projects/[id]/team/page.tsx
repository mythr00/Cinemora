"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { ProjectTabs } from "@/components/project-tabs";
import { ErrorState, LoadingState } from "@/components/states";
import { ArrowLeft, MailPlus, MessageSquare, Trash2, UserMinus } from "lucide-react";
import {
  changeProjectMemberRole,
  createProjectComment,
  deleteProjectComment,
  editProjectComment,
  getCurrentUser,
  getProjectActivity,
  getProjectMembers,
  inviteProjectMember,
  listProjectComments,
  removeProjectMember,
  type ActivityEvent,
  type AccountUser,
  type ProjectComment,
  type ProjectMembers,
} from "@/lib/api";

/**
 * Team (Collaboration, founder priority #11) — the project's
 * people, discussion and recorded activity.
 *
 * Roles are enforced by the backend on every route; this page
 * only shows the controls the caller's role may use (the members
 * response carries your_role). Invitations are IN-APP ONLY, and
 * the page says so plainly: no email service is configured, so
 * nothing is emailed — an invited person finds the invitation in
 * their account when they sign in. No presence indicators: who is
 * "online" is not recorded anywhere, so it is not shown.
 */

function formatTime(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone: "UTC",
  }).format(date);
}

function userLabel(user: { email?: string | null; display_name?: string | null } | null) {
  if (!user) return "Unknown account";
  return user.display_name || user.email || "Unknown account";
}

export default function ProjectTeamPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [me, setMe] = useState<AccountUser | null>(null);
  const [members, setMembers] = useState<ProjectMembers | null>(null);
  const [comments, setComments] = useState<ProjectComment[]>([]);
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState("viewer");
  const [draft, setDraft] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");

  const load = useCallback(async () => {
    setError(null);
    try {
      const [user, memberView, commentView, activityView] = await Promise.all([
        getCurrentUser(),
        getProjectMembers(projectId),
        listProjectComments(projectId),
        getProjectActivity(projectId),
      ]);
      setMe(user);
      setMembers(memberView);
      setComments(commentView.comments || []);
      setEvents(activityView.events || []);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }, [projectId]);

  useEffect(() => {
    const timer = setTimeout(() => {
      void load();
    }, 0);
    return () => clearTimeout(timer);
  }, [load]);

  const run = useCallback(
    async (action: () => Promise<unknown>) => {
      setActionError(null);
      try {
        await action();
        await load();
      } catch (caught) {
        setActionError(
          caught instanceof Error ? caught.message : "The action failed.",
        );
      }
    },
    [load],
  );

  const isOwner = members?.your_role === "owner";

  return (
    <main className="app-page team-page">
      <AppNavigation />
      <ProjectTabs projectId={projectId} />
      <section className="plan-content">
        <Link href={`/projects/${projectId}`} className="editor-back">
          <ArrowLeft size={14} strokeWidth={2} aria-hidden /> Project
        </Link>
        <p className="eyebrow">COLLABORATION</p>
        <h1>Team</h1>
        <p className="plan-intro">
          The people who can work on this project, the discussion, and the
          activity the system has recorded. Editors can produce and edit;
          viewers can look and comment. Only the owner manages membership.
          Invitations are not emailed — Cinemora has no email service
          configured — so an invited person sees the invitation in their
          account when they sign in.
        </p>

        {busy && (
          <LoadingState className="plan-message" label="Loading the team…" />
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

        {!busy && !error && members && (
          <>
            <section>
              <h2>People</h2>
              <ul className="team-people">
                <li>
                  <span>
                    <strong>{userLabel(members.owner)}</strong>
                    {members.owner?.email && (
                      <span className="plan-meta"> · {members.owner.email}</span>
                    )}
                  </span>
                  <span className="team-role">Owner</span>
                </li>
                {members.members.map((entry) => (
                  <li key={entry.user?.user_id || entry.invited_at || ""}>
                    <span>
                      <strong>{userLabel(entry.user)}</strong>
                      {entry.user?.email && (
                        <span className="plan-meta"> · {entry.user.email}</span>
                      )}
                    </span>
                    {isOwner && entry.user ? (
                      <span className="team-actions">
                        <select
                          value={entry.role}
                          aria-label={`Role for ${userLabel(entry.user)}`}
                          onChange={(event) =>
                            void run(() =>
                              changeProjectMemberRole(
                                projectId,
                                entry.user!.user_id,
                                event.target.value,
                              ),
                            )
                          }
                        >
                          <option value="editor">Editor</option>
                          <option value="viewer">Viewer</option>
                        </select>
                        <button
                          type="button"
                          onClick={() =>
                            void run(() =>
                              removeProjectMember(
                                projectId,
                                entry.user!.user_id,
                              ),
                            )
                          }
                        >
                          <UserMinus size={14} strokeWidth={2} aria-hidden />{" "}
                          Remove
                        </button>
                      </span>
                    ) : (
                      <span className="team-role">
                        {entry.role === "editor" ? "Editor" : "Viewer"}
                        {me && entry.user?.user_id === me.id && (
                          <button
                            type="button"
                            className="team-leave"
                            onClick={() =>
                              void run(() =>
                                removeProjectMember(projectId, me.id),
                              )
                            }
                          >
                            Leave project
                          </button>
                        )}
                      </span>
                    )}
                  </li>
                ))}
                {members.invitations.map((entry) => (
                  <li key={`inv-${entry.user?.user_id || ""}`}>
                    <span>
                      <strong>{userLabel(entry.user)}</strong>
                      {entry.user?.email && (
                        <span className="plan-meta"> · {entry.user.email}</span>
                      )}
                      <span className="plan-meta">
                        {" "}
                        · invited {formatTime(entry.invited_at)} — waiting for
                        them to sign in and respond
                      </span>
                    </span>
                    <span className="team-actions">
                      <span className="team-role">
                        Invited as {entry.role === "editor" ? "Editor" : "Viewer"}
                      </span>
                      {isOwner && entry.user && (
                        <button
                          type="button"
                          onClick={() =>
                            void run(() =>
                              removeProjectMember(
                                projectId,
                                entry.user!.user_id,
                              ),
                            )
                          }
                        >
                          <UserMinus size={14} strokeWidth={2} aria-hidden />{" "}
                          Withdraw
                        </button>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
              {members.members.length === 0 &&
                members.invitations.length === 0 && (
                  <p className="plan-meta">
                    No one else has access to this project yet.
                  </p>
                )}
            </section>

            {isOwner && (
              <section>
                <h2>Invite someone</h2>
                <p className="plan-meta">
                  The person must already have a Cinemora account under that
                  email address. They will see the invitation in their account
                  — no email is sent.
                </p>
                <form
                  className="team-invite"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void run(async () => {
                      await inviteProjectMember(
                        projectId,
                        inviteEmail.trim(),
                        inviteRole,
                      );
                      setInviteEmail("");
                    });
                  }}
                >
                  <label>
                    Email
                    <input
                      type="email"
                      required
                      value={inviteEmail}
                      onChange={(event) => setInviteEmail(event.target.value)}
                      placeholder="person@example.com"
                    />
                  </label>
                  <label>
                    Role
                    <select
                      value={inviteRole}
                      onChange={(event) => setInviteRole(event.target.value)}
                    >
                      <option value="viewer">Viewer — read and comment</option>
                      <option value="editor">
                        Editor — produce and edit
                      </option>
                    </select>
                  </label>
                  <button type="submit" className="button button-primary">
                    <MailPlus size={14} strokeWidth={2} aria-hidden /> Send
                    invitation
                  </button>
                </form>
              </section>
            )}

            <section>
              <h2>
                <MessageSquare size={16} strokeWidth={2} aria-hidden />{" "}
                Discussion
              </h2>
              {comments.length === 0 && (
                <p className="plan-meta">No comments yet.</p>
              )}
              <ul className="team-comments">
                {comments.map((comment) => (
                  <li key={comment.comment_id}>
                    <p className="plan-meta">
                      <strong>{userLabel(comment.author)}</strong> ·{" "}
                      {formatTime(comment.created_at)}
                      {comment.edited_at && " · edited"}
                      {comment.revision_id &&
                        ` · on revision ${comment.revision_id}`}
                    </p>
                    {editingId === comment.comment_id ? (
                      <form
                        onSubmit={(event) => {
                          event.preventDefault();
                          void run(async () => {
                            await editProjectComment(
                              projectId,
                              comment.comment_id,
                              editText,
                            );
                            setEditingId(null);
                          });
                        }}
                      >
                        <textarea
                          value={editText}
                          onChange={(event) => setEditText(event.target.value)}
                          rows={3}
                          required
                        />
                        <span className="team-actions">
                          <button type="submit">Save</button>
                          <button
                            type="button"
                            onClick={() => setEditingId(null)}
                          >
                            Cancel
                          </button>
                        </span>
                      </form>
                    ) : (
                      <>
                        <p>{comment.body}</p>
                        <span className="team-actions">
                          {me && comment.author?.user_id === me.id && (
                            <button
                              type="button"
                              onClick={() => {
                                setEditingId(comment.comment_id);
                                setEditText(comment.body);
                              }}
                            >
                              Edit
                            </button>
                          )}
                          {me &&
                            (comment.author?.user_id === me.id || isOwner) && (
                              <button
                                type="button"
                                onClick={() =>
                                  void run(() =>
                                    deleteProjectComment(
                                      projectId,
                                      comment.comment_id,
                                    ),
                                  )
                                }
                              >
                                <Trash2
                                  size={14}
                                  strokeWidth={2}
                                  aria-hidden
                                />{" "}
                                Delete
                              </button>
                            )}
                        </span>
                      </>
                    )}
                  </li>
                ))}
              </ul>
              <form
                className="team-comment-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  void run(async () => {
                    await createProjectComment(projectId, draft);
                    setDraft("");
                  });
                }}
              >
                <label>
                  Add a comment
                  <textarea
                    value={draft}
                    onChange={(event) => setDraft(event.target.value)}
                    rows={3}
                    required
                    placeholder="Notes for the team…"
                  />
                </label>
                <button type="submit" className="button button-primary">
                  Post comment
                </button>
              </form>
            </section>

            <section>
              <h2>Activity</h2>
              <p className="plan-meta">
                Assembled from the records themselves — production runs,
                editor revisions, plan decisions, membership changes and
                comments. Nothing is recorded here that did not happen.
              </p>
              {events.length === 0 && (
                <p className="plan-meta">No recorded activity yet.</p>
              )}
              <ul className="team-activity">
                {events.map((event, index) => (
                  <li key={`${event.at || ""}-${event.kind}-${index}`}>
                    <span className="plan-meta">{formatTime(event.at)}</span>{" "}
                    {event.summary}
                  </li>
                ))}
              </ul>
            </section>
          </>
        )}
      </section>
    </main>
  );
}
