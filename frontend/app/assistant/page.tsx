"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import {
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { AppNavigation } from "@/components/app-navigation";
import { PendingActionCard } from "@/components/assistant/pending-action-card";
import { ToolResultCards } from "@/components/assistant/result-cards";
import { EmptyState, ErrorState, LoadingState } from "@/components/states";
import {
  createAssistantConversation,
  deleteAssistantConversation,
  getAssistantConversation,
  listAssistantConversations,
  postAssistantMessage,
  type AssistantAction,
  type AssistantConversation,
  type AssistantMessage,
} from "@/lib/api";
import { CirclePlus, Send, Sparkles, Trash2 } from "lucide-react";

// Phase 6 workspace (design assistant/07): a dedicated page, not a
// floating widget. Everything rendered comes from the assistant
// API: messages and their recorded tool traces, cards built from
// the turn's actual tool results, and pending-action cards wired
// to the confirm/reject endpoints. Nothing is pre-computed or
// suggested beyond example prompts drawn from real tool
// capabilities.

const EXAMPLE_PROMPTS = [
  "What is the status of my latest production?",
  "Why did my most recent run fail?",
  "Show me the recommendations for my latest project",
  "How do I use the Asset Manager?",
] as const;

function provenance(message: AssistantMessage): string | null {
  const tools = message.tool_trace?.tools || [];
  if (tools.length === 0) return null;
  return tools.map((entry) => entry.label).join(" · ");
}

function injectionNotice(message: AssistantMessage): boolean {
  return (message.tool_trace?.tools || []).some(
    (entry) =>
      Array.isArray(entry.injection_patterns_detected) &&
      entry.injection_patterns_detected.length > 0,
  );
}

function Workspace() {
  const searchParams = useSearchParams();
  const projectParam = searchParams.get("project");

  const [conversations, setConversations] = useState<AssistantConversation[]>([]);
  const [listBusy, setListBusy] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [boundProjectId, setBoundProjectId] = useState<string | null>(null);
  const [actions, setActions] = useState<AssistantAction[]>([]);
  const [liveCards, setLiveCards] = useState<Record<string, Record<string, unknown>>>({});
  const [detailBusy, setDetailBusy] = useState(false);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const boundProjectHandled = useRef(false);
  const threadEndRef = useRef<HTMLDivElement | null>(null);

  const refreshList = useCallback(async (signal?: AbortSignal) => {
    const data = await listAssistantConversations(signal);
    setConversations(data.conversations || []);
    return data.conversations || [];
  }, []);

  const openConversation = useCallback(async (id: string) => {
    setSelectedId(id);
    setDetailBusy(true);
    setError(null);
    try {
      const detail = await getAssistantConversation(id);
      setMessages(detail.messages || []);
      setActions(detail.pending_actions || []);
      setBoundProjectId(detail.conversation.project_id);
    } catch (caught) {
      setError(caught);
    } finally {
      setDetailBusy(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const list = await refreshList(controller.signal);
        if (projectParam && !boundProjectHandled.current) {
          boundProjectHandled.current = true;
          const existing = list.find((row) => row.project_id === projectParam);
          if (existing) {
            await openConversation(existing.id);
          } else {
            const created = await createAssistantConversation(projectParam);
            setConversations((current) => [created, ...current]);
            await openConversation(created.id);
          }
        } else if (list.length > 0) {
          await openConversation(list[0].id);
        }
      } catch (caught) {
        if (!controller.signal.aborted) setError(caught);
      } finally {
        if (!controller.signal.aborted) setListBusy(false);
      }
    })();
    return () => controller.abort();
    // Mount-only bootstrap; selection changes go through openConversation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    threadEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, actions, sending]);

  async function startConversation() {
    setError(null);
    try {
      const created = await createAssistantConversation(
        projectParam || undefined,
      );
      setConversations((current) => [created, ...current]);
      setMessages([]);
      setActions([]);
      setLiveCards({});
      setBoundProjectId(created.project_id);
      setSelectedId(created.id);
    } catch (caught) {
      setError(caught);
    }
  }

  async function removeConversation(id: string) {
    try {
      await deleteAssistantConversation(id);
      setConversations((current) => current.filter((row) => row.id !== id));
      if (id === selectedId) {
        setSelectedId(null);
        setMessages([]);
        setActions([]);
        setBoundProjectId(null);
      }
    } catch (caught) {
      setError(caught);
    }
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || sending) return;
    let conversationId = selectedId;
    setSending(true);
    setError(null);
    try {
      if (!conversationId) {
        const created = await createAssistantConversation(
          projectParam || undefined,
        );
        conversationId = created.id;
        setConversations((current) => [created, ...current]);
        setSelectedId(created.id);
        setBoundProjectId(created.project_id);
      }
      setDraft("");
      const optimistic: AssistantMessage = {
        id: `local-${Date.now()}`,
        conversation_id: conversationId,
        role: "user",
        content,
        tool_trace: null,
        created_at: new Date().toISOString(),
      };
      setMessages((current) => [...current, optimistic]);
      const result = await postAssistantMessage(conversationId, content);
      setMessages((current) => [
        ...current.filter((row) => row.id !== optimistic.id),
        { ...optimistic, id: `sent-${Date.now()}` },
        result.message,
      ]);
      if (result.tool_results) {
        setLiveCards((current) => ({
          ...current,
          [result.message.id]: result.tool_results,
        }));
      }
      if (result.pending_action) {
        const pending = result.pending_action;
        setActions((current) => [
          ...current.filter((row) => row.id !== pending.id),
          pending,
        ]);
      }
      void refreshList().catch(() => undefined);
    } catch (caught) {
      setError(caught);
    } finally {
      setSending(false);
    }
  }

  const actionsByMessage = new Map<string, AssistantAction[]>();
  for (const action of actions) {
    const key = action.message_id || "";
    const bucket = actionsByMessage.get(key) || [];
    bucket.push(action);
    actionsByMessage.set(key, bucket);
  }
  const orphanActions = actionsByMessage.get("") || [];

  return (
    <main className="app-page">
      <AppNavigation />
      <section className="assistant-shell">
        <aside className="assistant-sidebar" aria-label="Conversations">
          <div className="assistant-sidebar-head">
            <p className="eyebrow">AI ASSISTANT</p>
            <button
              type="button"
              className="button button-primary"
              onClick={() => void startConversation()}
            >
              <CirclePlus size={14} strokeWidth={2} aria-hidden /> New conversation
            </button>
          </div>
          {listBusy ? (
            <LoadingState label="Loading conversations…" />
          ) : conversations.length === 0 ? (
            <p className="assistant-sidebar-note">
              No conversations yet. The assistant answers from your real projects, and asks before it changes anything.
            </p>
          ) : (
            <ul className="assistant-conversation-list">
              {conversations.map((conversation) => (
                <li
                  key={conversation.id}
                  className={`assistant-conversation-row${conversation.id === selectedId ? " is-active" : ""}`}
                >
                  <button
                    type="button"
                    className="assistant-conversation-open"
                    aria-current={conversation.id === selectedId}
                    onClick={() => void openConversation(conversation.id)}
                  >
                    <span className="assistant-conversation-title">
                      {conversation.title || "Untitled conversation"}
                    </span>
                    <span className="assistant-conversation-meta">
                      {conversation.project_id ? "Project-bound · " : ""}
                      {conversation.updated_at
                        ? new Date(conversation.updated_at).toLocaleString()
                        : ""}
                    </span>
                  </button>
                  <button
                    type="button"
                    className="assistant-conversation-delete"
                    aria-label="Delete conversation"
                    onClick={() => void removeConversation(conversation.id)}
                  >
                    <Trash2 size={14} strokeWidth={2} aria-hidden />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>

        <div className="assistant-main">
          <header className="assistant-head">
            <h1>
              <Sparkles size={20} strokeWidth={2} aria-hidden /> Assistant
            </h1>
            {boundProjectId && (
              <p className="assistant-bound">
                Bound to project{" "}
                <Link className="text-link" href={`/projects/${boundProjectId}`}>
                  {boundProjectId}
                </Link>{" "}
                — answers use this project&apos;s real records.
              </p>
            )}
          </header>

          {error ? (
            <ErrorState
              error={error}
              onRetry={() => {
                setError(null);
                if (selectedId) void openConversation(selectedId);
              }}
            />
          ) : null}

          <div className="assistant-thread" aria-live="polite">
            {detailBusy ? (
              <LoadingState label="Loading conversation…" />
            ) : messages.length === 0 ? (
              <div className="assistant-empty">
                <EmptyState
                  eyebrow="Assistant"
                  title="Ask about your productions"
                  description="Status, failures, scripts, assets, recommendations — answered from your account's real data. Actions that change anything ask for your confirmation first."
                />
                <div className="assistant-examples">
                  {EXAMPLE_PROMPTS.map((prompt) => (
                    <button
                      key={prompt}
                      type="button"
                      className="assistant-example"
                      onClick={() => setDraft(prompt)}
                    >
                      {prompt}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              messages.map((message) => (
                <article
                  key={message.id}
                  className={`assistant-message is-${message.role}`}
                >
                  <p className="assistant-message-role">
                    {message.role === "user" ? "You" : "Assistant"}
                  </p>
                  <div className="assistant-message-content">{message.content}</div>
                  {message.role === "assistant" && provenance(message) && (
                    <p className="assistant-provenance">{provenance(message)}</p>
                  )}
                  {message.role === "assistant" && injectionNotice(message) && (
                    <p className="assistant-injection-note" role="note">
                      Suspicious instructions found in retrieved content were
                      treated as data and ignored.
                    </p>
                  )}
                  {liveCards[message.id] && (
                    <ToolResultCards toolResults={liveCards[message.id]} />
                  )}
                  {(actionsByMessage.get(message.id) || []).map((action) => (
                    <PendingActionCard
                      key={action.id}
                      action={action}
                      onDecided={(updated) =>
                        setActions((current) =>
                          current.map((row) =>
                            row.id === updated.id ? updated : row,
                          ),
                        )
                      }
                    />
                  ))}
                </article>
              ))
            )}
            {orphanActions.map((action) => (
              <PendingActionCard
                key={action.id}
                action={action}
                onDecided={(updated) =>
                  setActions((current) =>
                    current.map((row) => (row.id === updated.id ? updated : row)),
                  )
                }
              />
            ))}
            {sending && (
              <p className="assistant-sending" role="status">
                Working — checking your real records…
              </p>
            )}
            <div ref={threadEndRef} />
          </div>

          <form className="assistant-composer" onSubmit={(event) => void send(event)}>
            <label className="assistant-composer-label" htmlFor="assistant-input">
              Message the assistant
            </label>
            <textarea
              id="assistant-input"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
              placeholder="Ask about a project, a run, or how a feature works…"
              rows={3}
              disabled={sending}
            />
            <button
              type="submit"
              className="button button-primary"
              disabled={sending || draft.trim().length === 0}
            >
              <Send size={14} strokeWidth={2} aria-hidden />
              {sending ? "Sending…" : "Send"}
            </button>
          </form>
        </div>
      </section>
    </main>
  );
}

export default function AssistantPage() {
  return (
    <Suspense>
      <Workspace />
    </Suspense>
  );
}
