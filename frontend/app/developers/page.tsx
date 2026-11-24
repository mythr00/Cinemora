"use client";

import { useCallback, useEffect, useState } from "react";
import { AppNavigation } from "@/components/app-navigation";
import { ErrorState, LoadingState } from "@/components/states";
import { formatDateTime } from "@/components/admin-format";
import {
  Check,
  Copy,
  KeyRound,
  Plus,
  ShieldAlert,
  Trash2,
  Webhook,
  X,
} from "lucide-react";
import {
  createApiKey,
  createWebhook,
  disableWebhook,
  getDeveloperDocs,
  listApiKeys,
  listWebhookDeliveries,
  listWebhooks,
  revokeApiKey,
  type ApiKeyList,
  type CreatedApiKey,
  type CreatedWebhook,
  type DeveloperDocs,
  type WebhookDeliveryView,
  type WebhookEndpointView,
} from "@/lib/api";

/**
 * Developers (founder priority #16): per-user API keys, run
 * webhooks, and the API documentation. The documentation section
 * renders the backend's generated payload — the endpoint list,
 * scopes and curl examples come from the server's live route
 * table, so what is documented here is what exists.
 *
 * Secrets are shown exactly once: a created key's secret (or a
 * webhook's signing secret) appears in the show-once panel below
 * and nowhere else — after dismissal it cannot be displayed
 * again, because the server stores only its hash.
 */

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="button button-quiet"
      onClick={() => {
        void navigator.clipboard
          .writeText(text)
          .then(() => setCopied(true))
          .catch(() => setCopied(false));
      }}
    >
      {copied ? <Check size={14} /> : <Copy size={14} />}
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function ShowOnceSecret({
  title,
  secret,
  onDismiss,
}: {
  title: string;
  secret: string;
  onDismiss: () => void;
}) {
  return (
    <div className="developers-secret" role="alert">
      <div className="developers-secret-head">
        <ShieldAlert size={16} />
        <strong>{title}</strong>
      </div>
      <p>
        Copy this secret now and store it somewhere safe.{" "}
        <strong>You will not see this again</strong> — Cinemora stores
        only a hash of it, so it cannot be recovered or re-displayed.
        If you lose it, revoke the credential and create a new one.
      </p>
      <div className="developers-secret-row">
        <code>{secret}</code>
        <CopyButton text={secret} />
      </div>
      <button type="button" className="button button-secondary" onClick={onDismiss}>
        <Check size={14} /> I have saved this secret
      </button>
    </div>
  );
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : "Request failed.";
}

export default function DevelopersPage() {
  const [keyList, setKeyList] = useState<ApiKeyList | null>(null);
  const [webhookList, setWebhookList] = useState<WebhookEndpointView[] | null>(null);
  const [docs, setDocs] = useState<DeveloperDocs | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [newKey, setNewKey] = useState<CreatedApiKey | null>(null);
  const [newWebhook, setNewWebhook] = useState<CreatedWebhook | null>(null);

  const [keyName, setKeyName] = useState("");
  const [keyScopes, setKeyScopes] = useState<string[]>(["read"]);
  const [keyRateLimit, setKeyRateLimit] = useState("");
  const [keyExpiry, setKeyExpiry] = useState("");
  const [creatingKey, setCreatingKey] = useState(false);

  const [webhookUrl, setWebhookUrl] = useState("");
  const [webhookEvents, setWebhookEvents] = useState<string[]>(["run.completed"]);
  const [creatingWebhook, setCreatingWebhook] = useState(false);

  const [deliveries, setDeliveries] = useState<
    Record<string, WebhookDeliveryView[]>
  >({});

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const [keys, webhooks, documentation] = await Promise.all([
        listApiKeys(signal),
        listWebhooks(signal),
        getDeveloperDocs(signal),
      ]);
      setKeyList(keys);
      setWebhookList(webhooks.endpoints);
      setDocs(documentation);
      setLoadError(null);
    } catch (cause) {
      if (!signal?.aborted) setLoadError(cause);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void load(controller.signal);
    }, 0);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [load]);

  const toggleScope = (scope: string) =>
    setKeyScopes((current) =>
      current.includes(scope)
        ? current.filter((item) => item !== scope)
        : [...current, scope],
    );

  const toggleEvent = (event: string) =>
    setWebhookEvents((current) =>
      current.includes(event)
        ? current.filter((item) => item !== event)
        : [...current, event],
    );

  const submitKey = async () => {
    setActionError(null);
    setCreatingKey(true);
    try {
      const created = await createApiKey({
        name: keyName.trim(),
        scopes: keyScopes,
        expires_at: keyExpiry
          ? new Date(keyExpiry).toISOString()
          : null,
        rate_limit_per_minute: keyRateLimit
          ? Number(keyRateLimit)
          : null,
      });
      setNewKey(created);
      setKeyName("");
      setKeyRateLimit("");
      setKeyExpiry("");
      await load();
    } catch (cause) {
      setActionError(errorMessage(cause));
    } finally {
      setCreatingKey(false);
    }
  };

  const submitWebhook = async () => {
    setActionError(null);
    setCreatingWebhook(true);
    try {
      const created = await createWebhook({
        url: webhookUrl.trim(),
        events: webhookEvents,
      });
      setNewWebhook(created);
      setWebhookUrl("");
      await load();
    } catch (cause) {
      setActionError(errorMessage(cause));
    } finally {
      setCreatingWebhook(false);
    }
  };

  const revoke = async (keyId: string) => {
    setActionError(null);
    try {
      await revokeApiKey(keyId);
      await load();
    } catch (cause) {
      setActionError(errorMessage(cause));
    }
  };

  const disable = async (endpointId: string) => {
    setActionError(null);
    try {
      await disableWebhook(endpointId);
      await load();
    } catch (cause) {
      setActionError(errorMessage(cause));
    }
  };

  const toggleDeliveries = async (endpointId: string) => {
    if (deliveries[endpointId]) {
      setDeliveries((current) => {
        const next = { ...current };
        delete next[endpointId];
        return next;
      });
      return;
    }
    try {
      const result = await listWebhookDeliveries(endpointId);
      setDeliveries((current) => ({
        ...current,
        [endpointId]: result.deliveries,
      }));
    } catch (cause) {
      setActionError(errorMessage(cause));
    }
  };

  return (
    <main className="app-page developers-page">
      <AppNavigation />
      <div className="developers-content">
        <header className="developers-header">
          <p className="eyebrow">DEVELOPER API</p>
          <h1>Build on Cinemora with your own API keys.</h1>
          <p className="developers-intro">
            Keys authenticate the public API at{" "}
            <code>/api/public/v1</code> with an{" "}
            <code>X-API-Key</code> header. A key only ever reaches
            your own projects, its scopes are enforced on every
            route, and its usage below is counted from real requests.
          </p>
        </header>

        {actionError ? (
          <p className="developers-message" role="alert">
            {actionError}
          </p>
        ) : null}

        {loadError ? (
          <ErrorState error={loadError} onRetry={() => void load()} />
        ) : !keyList || !webhookList || !docs ? (
          <LoadingState label="Loading developer settings…" />
        ) : (
          <>
            <section className="panel developers-panel">
              <div className="developers-section-row">
                <h2>
                  <KeyRound size={16} /> API keys
                </h2>
                <span className="developers-note">
                  {keyList.keys.filter((k) => k.status === "active").length}{" "}
                  active of {keyList.max_keys_per_user} allowed
                </span>
              </div>

              {newKey ? (
                <ShowOnceSecret
                  title={`Key "${newKey.name}" created`}
                  secret={newKey.secret}
                  onDismiss={() => setNewKey(null)}
                />
              ) : null}

              <form
                className="developers-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  void submitKey();
                }}
              >
                <label className="developers-field">
                  Key name
                  <input
                    value={keyName}
                    onChange={(event) => setKeyName(event.target.value)}
                    placeholder="e.g. CI pipeline"
                    required
                    maxLength={120}
                  />
                </label>
                <fieldset className="developers-field">
                  <legend>Scopes</legend>
                  {["read", "write"].map((scope) => (
                    <label key={scope} className="developers-check">
                      <input
                        type="checkbox"
                        checked={keyScopes.includes(scope)}
                        onChange={() => toggleScope(scope)}
                      />
                      {scope}
                    </label>
                  ))}
                </fieldset>
                <label className="developers-field">
                  Rate limit (requests/min, optional)
                  <input
                    type="number"
                    min={1}
                    max={600}
                    value={keyRateLimit}
                    onChange={(event) => setKeyRateLimit(event.target.value)}
                    placeholder={`Default ${keyList.default_rate_limit_per_minute}`}
                  />
                </label>
                <label className="developers-field">
                  Expires (optional)
                  <input
                    type="datetime-local"
                    value={keyExpiry}
                    onChange={(event) => setKeyExpiry(event.target.value)}
                  />
                </label>
                <button
                  type="submit"
                  className="button button-primary"
                  disabled={creatingKey || !keyName.trim() || keyScopes.length === 0}
                >
                  <Plus size={14} /> Create key
                </button>
              </form>

              <div className="developers-list">
                {keyList.keys.length === 0 ? (
                  <p className="developers-note">No API keys yet.</p>
                ) : (
                  keyList.keys.map((key) => (
                    <article key={key.id} className="developers-card">
                      <div className="developers-card-main">
                        <strong>{key.name}</strong>
                        <code>{key.key_prefix}…</code>
                        <span className={`developers-status is-${key.status}`}>
                          {key.status}
                        </span>
                      </div>
                      <p className="developers-note">
                        Scopes: {key.scopes.join(", ")} ·{" "}
                        {key.rate_limit_per_minute} requests/min
                        {key.rate_limit_is_default ? " (default)" : ""} ·{" "}
                        {key.request_count} requests recorded · last used{" "}
                        {key.last_used_at
                          ? formatDateTime(key.last_used_at)
                          : "never"}{" "}
                        · created {formatDateTime(key.created_at)}
                        {key.expires_at
                          ? ` · expires ${formatDateTime(key.expires_at)}`
                          : ""}
                      </p>
                      {key.status === "active" ? (
                        <button
                          type="button"
                          className="button button-quiet"
                          onClick={() => void revoke(key.id)}
                        >
                          <Trash2 size={14} /> Revoke
                        </button>
                      ) : null}
                    </article>
                  ))
                )}
              </div>
            </section>

            <section className="panel developers-panel">
              <div className="developers-section-row">
                <h2>
                  <Webhook size={16} /> Webhooks
                </h2>
                <span className="developers-note">
                  Signed notifications when your runs finish
                </span>
              </div>

              {newWebhook ? (
                <ShowOnceSecret
                  title="Webhook registered — signing secret"
                  secret={newWebhook.signing_secret}
                  onDismiss={() => setNewWebhook(null)}
                />
              ) : null}

              <form
                className="developers-form"
                onSubmit={(event) => {
                  event.preventDefault();
                  void submitWebhook();
                }}
              >
                <label className="developers-field">
                  Endpoint URL
                  <input
                    type="url"
                    value={webhookUrl}
                    onChange={(event) => setWebhookUrl(event.target.value)}
                    placeholder="https://your-server.example/hooks/cinemora"
                    required
                    maxLength={2048}
                  />
                </label>
                <fieldset className="developers-field">
                  <legend>Events</legend>
                  {docs.webhooks.events.map((event) => (
                    <label key={event} className="developers-check">
                      <input
                        type="checkbox"
                        checked={webhookEvents.includes(event)}
                        onChange={() => toggleEvent(event)}
                      />
                      {event}
                    </label>
                  ))}
                </fieldset>
                <button
                  type="submit"
                  className="button button-primary"
                  disabled={
                    creatingWebhook ||
                    !webhookUrl.trim() ||
                    webhookEvents.length === 0
                  }
                >
                  <Plus size={14} /> Register webhook
                </button>
              </form>

              <div className="developers-list">
                {webhookList.length === 0 ? (
                  <p className="developers-note">No webhooks registered.</p>
                ) : (
                  webhookList.map((endpoint) => (
                    <article key={endpoint.id} className="developers-card">
                      <div className="developers-card-main">
                        <code>{endpoint.url}</code>
                        <span
                          className={`developers-status is-${endpoint.status}`}
                        >
                          {endpoint.status}
                        </span>
                      </div>
                      <p className="developers-note">
                        Events: {endpoint.events.join(", ")} · registered{" "}
                        {formatDateTime(endpoint.created_at)}
                      </p>
                      <div className="developers-actions">
                        <button
                          type="button"
                          className="button button-quiet"
                          onClick={() => void toggleDeliveries(endpoint.id)}
                        >
                          {deliveries[endpoint.id] ? (
                            <X size={14} />
                          ) : null}
                          {deliveries[endpoint.id]
                            ? "Hide deliveries"
                            : "View deliveries"}
                        </button>
                        {endpoint.status === "active" ? (
                          <button
                            type="button"
                            className="button button-quiet"
                            onClick={() => void disable(endpoint.id)}
                          >
                            <Trash2 size={14} /> Disable
                          </button>
                        ) : null}
                      </div>
                      {deliveries[endpoint.id] ? (
                        <div className="developers-deliveries">
                          {deliveries[endpoint.id].length === 0 ? (
                            <p className="developers-note">
                              No deliveries recorded yet.
                            </p>
                          ) : (
                            deliveries[endpoint.id].map((delivery) => (
                              <p
                                key={delivery.id}
                                className="developers-note"
                              >
                                {formatDateTime(delivery.created_at)} ·{" "}
                                {delivery.event} · attempt {delivery.attempt}{" "}
                                ·{" "}
                                {delivery.response_status
                                  ? `HTTP ${delivery.response_status}`
                                  : `failed: ${delivery.error ?? "transport error"}`}
                              </p>
                            ))
                          )}
                        </div>
                      ) : null}
                    </article>
                  ))
                )}
              </div>
            </section>

            <section className="panel developers-panel">
              <div className="developers-section-row">
                <h2>API documentation</h2>
                <span className="developers-note">
                  Generated from the live route table
                </span>
              </div>
              <p className="developers-note">
                Set <code>CINEMORA_API_BASE</code> to this
                deployment&rsquo;s origin and{" "}
                <code>CINEMORA_API_KEY</code> to a key secret,
                then the examples below run as written. {docs.auth.notes}
              </p>
              <p className="developers-note">
                Rate limit: {docs.rate_limit.default_per_minute} requests
                per {docs.rate_limit.window_seconds}s per key by default.{" "}
                {docs.rate_limit.per_key_override} {docs.rate_limit.overage}
              </p>
              <div className="developers-list">
                {docs.endpoints.map((endpoint) => (
                  <article
                    key={`${endpoint.method} ${endpoint.path}`}
                    className="developers-card"
                  >
                    <div className="developers-card-main">
                      <code>
                        {endpoint.method} {endpoint.path}
                      </code>
                      <span className="developers-status is-active">
                        {endpoint.scope}
                      </span>
                    </div>
                    <p className="developers-note">{endpoint.summary}</p>
                    <div className="developers-secret-row">
                      <code>{endpoint.curl}</code>
                      <CopyButton text={endpoint.curl} />
                    </div>
                  </article>
                ))}
              </div>
              <h3 className="developers-subtitle">Webhook signatures</h3>
              <p className="developers-note">{docs.webhooks.signature}</p>
              <p className="developers-note">{docs.webhooks.retries}</p>
              <div className="developers-secret-row">
                <code>
                  {JSON.stringify(docs.webhooks.payload_example, null, 2)}
                </code>
              </div>
            </section>
          </>
        )}
      </div>
    </main>
  );
}
