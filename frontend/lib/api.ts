/**
 * Central API client for the Cinemora studio (Stage 4).
 *
 * This module is the ONLY place that knows the backend's base URL.
 * Pages and components must never build backend URLs themselves
 * (AUD-04: the base URL was previously duplicated, with a hardcoded
 * 127.0.0.1 fallback, in eight separate files).
 *
 * Two surfaces:
 * - The /api/v1 contract (apiRequest + the typed helpers below):
 *   per-user session auth (Phase 8.5 — the httpOnly session cookie
 *   issued at sign-in, carried via credentials:"include"),
 *   consistent {error: {code, message}} failures surfaced as
 *   ApiClientError.
 * - Legacy endpoints that predate v1 (legacyRequest): NO active
 *   page calls these any more. The legacy surfaces (scene editor,
 *   render-options, voice/music/background catalogues, thumbnails,
 *   channel research, viral clips, transcription, voiceover) were
 *   quarantined in Brief Stage 3 (C-02) — every endpoint they used
 *   is absent from the committed backend (all 23 re-verified 404
 *   under TestClient). Their original implementations are preserved
 *   under frontend/preserved/ and are the only remaining
 *   legacyRequest callers; the helper stays so that preserved code
 *   keeps compiling, and so the base URL stays in one place if a
 *   legacy surface is ever restored with a real backend.
 *
 * Configuration (build-time, NEXT_PUBLIC_*):
 * - NEXT_PUBLIC_API_BASE_URL: backend origin (default
 *   http://localhost:8000). The special value "same-origin" selects
 *   the deployed mode: the browser calls this origin's /api/*
 *   routes and the server-side proxy (app/api/[...path]/route.ts)
 *   forwards them to the real backend together with the visitor's
 *   session cookie (Phase 8.5). No credential of any kind is
 *   shipped in the public bundle.
 */

const RAW_API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL || "";

const API_BASE_URL =
  RAW_API_BASE_URL === "same-origin"
    ? ""
    : (RAW_API_BASE_URL || "http://localhost:8000").replace(/\/$/, "");


/**
 * True when the client runs in the deployed same-origin mode
 * (F5): API calls go to this origin's /api/* routes and the
 * server-side proxy forwards the visitor's session cookie. In
 * this mode a bare media URL is authenticated by the proxy, so
 * <video>, <img> and download anchors may use it directly and
 * the media endpoint's Range support streams playback — no
 * whole-file blob fetch. In direct mode (local development
 * against a backend origin) the cookie only exists in the
 * browser's store for that origin, so media still goes through
 * the blob helpers below (which send credentials the same way).
 */
export const SAME_ORIGIN_API = RAW_API_BASE_URL === "same-origin";

export class ApiClientError extends Error {
  status: number;
  code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "ApiClientError";
    this.status = status;
    this.code = code;
  }
}

/** Absolute URL for a backend path (media files, downloads, previews). */
export function apiUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  if (!API_BASE_URL) {
    // Same-origin mode: a relative URL resolves against the page's
    // own origin in the browser, which is where the proxy lives.
    return `/${path.replace(/^\/+/, "")}`;
  }
  return new URL(path.replace(/^\/+/, ""), `${API_BASE_URL}/`).toString();
}

type RequestOptions = RequestInit & { signal?: AbortSignal };

async function parseError(response: Response): Promise<ApiClientError> {
  let message = `Request failed (${response.status}).`;
  let code: string | undefined;
  try {
    const body = (await response.json()) as {
      error?: { code?: string; message?: string };
      detail?: string;
    };
    if (body.error?.message) {
      message = body.error.message;
      code = body.error.code;
    } else if (typeof body.detail === "string") {
      message = body.detail;
    }
  } catch {
    // Non-JSON error body: keep the status-based message.
  }
  return new ApiClientError(message, response.status, code);
}

async function request<T>(
  path: string,
  init: RequestOptions,
  authenticated: boolean,
): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  // Phase 8.5: authentication is the user's session cookie
  // (httpOnly, issued by /api/v1/auth/login), carried by
  // credentials:"include" — same-origin requests send it through
  // the proxy, direct-mode requests send it to the backend origin
  // (whose CORS allows credentials from the studio origin). The
  // pre-8.5 shared X-API-Key header is gone: one shared token
  // would make every visitor the same principal. The
  // `authenticated` flag now only distinguishes the legacy surface
  // (which shares the same session) and is kept for signature
  // compatibility with preserved callers.
  void authenticated;
  const response = await fetch(apiUrl(path), {
    cache: "no-store",
    credentials: "include",
    ...init,
    headers,
  });
  if (!response.ok) throw await parseError(response);
  return (await response.json()) as T;
}

/** Call a /api/v1 endpoint (session-authenticated). */
export function apiRequest<T>(path: string, init: RequestOptions = {}): Promise<T> {
  return request<T>(path, init, true);
}

// ---------------------------------------------------------------------
// Accounts + sessions (Phase 8.5)
// ---------------------------------------------------------------------

export type AccountUser = {
  id: string;
  email: string;
  display_name: string | null;
  role: "user" | "admin" | "super_admin";
  status: string;
  email_verified_at: string | null;
  created_at: string;
  last_login_at: string | null;
};

export type AccountUsage = {
  plan: {
    name: string;
    max_concurrent_runs: number | null;
    max_runs_per_period: number | null;
    period: string;
    max_script_chars: number | null;
    max_storage_bytes: number | null;
    max_upload_bytes: number | null;
    priority_tier: number;
  } | null;
  period_start: string;
  concurrent_runs: number;
  runs_in_period: number;
  provider: Record<string, unknown>;
  storage_bytes: number;
};

/** The signed-in user, or null when there is no valid session.
 * Only a definitive 401 maps to null; network/server failures
 * throw, so callers never mistake an outage for a sign-out. */
export async function getCurrentUser(
  signal?: AbortSignal,
): Promise<AccountUser | null> {
  try {
    const data = await apiRequest<{ user: AccountUser }>("/api/v1/auth/me", {
      signal,
    });
    return data.user;
  } catch (error) {
    if (error instanceof ApiClientError && error.status === 401) {
      return null;
    }
    throw error;
  }
}

export async function login(
  email: string,
  password: string,
): Promise<AccountUser> {
  const data = await apiRequest<{ user: AccountUser }>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
  return data.user;
}

export async function register(
  email: string,
  password: string,
  displayName?: string,
  referralCode?: string,
): Promise<AccountUser> {
  const data = await apiRequest<{ user: AccountUser }>(
    "/api/v1/auth/register",
    {
      method: "POST",
      body: JSON.stringify({
        email,
        password,
        display_name: displayName || undefined,
        referral_code: referralCode || undefined,
      }),
    },
  );
  return data.user;
}

export async function logout(): Promise<void> {
  await apiRequest("/api/v1/auth/logout", { method: "POST" });
}

/** Ask for a password-reset link. The API answers 202 identically
 * whether or not the email exists; `delivery` reports how the link
 * reaches the user ("smtp" when mail is configured, "manual" when
 * the operator provides it out of band). */
export async function requestPasswordReset(
  email: string,
): Promise<{ delivery: string }> {
  return apiRequest<{ delivery: string }>("/api/v1/auth/password-reset/request", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

export async function confirmPasswordReset(
  token: string,
  newPassword: string,
): Promise<void> {
  await apiRequest("/api/v1/auth/password-reset/confirm", {
    method: "POST",
    body: JSON.stringify({ token, new_password: newPassword }),
  });
}

export async function getAccountUsage(
  signal?: AbortSignal,
): Promise<AccountUsage> {
  return apiRequest<AccountUsage>("/api/v1/account/usage", { signal });
}

// ---------------------------------------------------------------------
// Billing, credits & subscriptions (Phase 8 MON — directive §§9, 14)
// ---------------------------------------------------------------------

export type BillingPlan = {
  id: string;
  name: string;
  display_name: string | null;
  price_monthly_cents: number | null;
  price_annual_cents: number | null;
  currency: string;
  monthly_credits: number;
  included_generations: number;
  script_allowance: number;
  thumbnail_allowance: number;
  billing_mode: "legacy" | "metered" | string;
  features: Record<string, unknown>;
  is_public: boolean;
  is_default: boolean;
  max_concurrent_runs: number | null;
  max_runs_per_period: number | null;
  priority_tier: number;
};

export type BillingSubscription = {
  id: string;
  plan_id: string;
  plan_name: string | null;
  status: string;
  billing_interval: string;
  current_period_start: string | null;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
  pending_plan_name: string | null;
  billing_provider: string | null;
} | null;

export type BillingPeriod = {
  period_start: string;
  period_end: string;
  generations_used: number;
  generations_included: number;
  generations_remaining: number;
  script_ops_used: number;
  script_allowance: number;
  script_remaining: number;
  thumbnail_ops_used: number;
  thumbnail_allowance: number;
  thumbnail_remaining: number;
} | null;

export type WalletBucket = {
  available: number;
  reserved: number;
  next_expiry_at: string | null;
};

export type BillingSummary = {
  plan: BillingPlan | null;
  subscription: BillingSubscription;
  period: BillingPeriod;
  wallet: {
    subscription: WalletBucket;
    purchased: WalletBucket;
    promo: WalletBucket;
    total_available: number;
  };
  available_plans: BillingPlan[];
  topup_packages: {
    id: string;
    name: string;
    credits: number;
    price_cents: number;
    currency: string;
  }[];
  payment_provider: {
    name: string | null;
    configured: boolean;
    public_config: { client_token: string | null; environment: string };
  };
  credit_pricing: {
    standard_generation_credits: number;
    durations: { minutes: number; credits: number }[];
  };
};

export type LedgerEntry = {
  id: string;
  entry_type: string;
  bucket: string;
  credits_delta: number;
  balance_after: number | null;
  project_id: string | null;
  run_id: string | null;
  description: string | null;
  details: Record<string, unknown> | null;
  expires_at: string | null;
  created_at: string;
};

export type BillingPayment = {
  id: string;
  kind: string;
  amount_cents: number;
  currency: string;
  status: string;
  provider: string | null;
  description: string | null;
  created_at: string;
};

export type UserNotification = {
  id: string;
  kind: string;
  title: string;
  body: string | null;
  read_at: string | null;
  created_at: string;
};

export type GenerationEstimate = {
  project_id: string;
  estimated_seconds: number;
  estimated_minutes: number;
  cost_credits: number;
  entitlement_available: boolean;
  entitlement_covers_seconds: number;
  credits_after_entitlement: number;
  wallet_available: number;
  sufficient: boolean;
  plan_name: string | null;
};

export function getBillingSummary(signal?: AbortSignal) {
  return apiRequest<BillingSummary>("/api/v1/account/billing", { signal });
}

export function getBillingTransactions(signal?: AbortSignal) {
  return apiRequest<{ entries: LedgerEntry[] }>(
    "/api/v1/account/billing/transactions",
    { signal },
  );
}

export function getBillingPayments(signal?: AbortSignal) {
  return apiRequest<{ payments: BillingPayment[] }>(
    "/api/v1/account/billing/payments",
    { signal },
  );
}

export function getNotifications(signal?: AbortSignal) {
  return apiRequest<{ notifications: UserNotification[]; unread_count: number }>(
    "/api/v1/account/notifications",
    { signal },
  );
}

export function markNotificationRead(notificationId: string) {
  return apiRequest<{ notification: UserNotification }>(
    `/api/v1/account/notifications/${notificationId}/read`,
    { method: "POST" },
  );
}

export function getGenerationEstimate(projectId: string, signal?: AbortSignal) {
  return apiRequest<GenerationEstimate>(
    `/api/v1/projects/${projectId}/generation-estimate`,
    { signal },
  );
}

export function startSubscriptionCheckout(planName: string, interval: string) {
  return apiRequest<{ transaction_id: string; provider: string }>(
    "/api/v1/account/billing/checkout/subscription",
    {
      method: "POST",
      body: JSON.stringify({ plan_name: planName, interval }),
    },
  );
}

export function startTopupCheckout(packageId: string) {
  return apiRequest<{ transaction_id: string; provider: string }>(
    "/api/v1/account/billing/checkout/topup",
    { method: "POST", body: JSON.stringify({ package_id: packageId }) },
  );
}

export function changeSubscription(planName: string, interval: string) {
  return apiRequest<{ status: string; effective: string }>(
    "/api/v1/account/billing/subscription/change",
    {
      method: "POST",
      body: JSON.stringify({ plan_name: planName, interval }),
    },
  );
}

export function cancelSubscription() {
  return apiRequest<{ status: string; current_period_end: string | null }>(
    "/api/v1/account/billing/subscription/cancel",
    { method: "POST" },
  );
}

export function getBillingManagementUrl() {
  return apiRequest<{ url: string }>(
    "/api/v1/account/billing/management-url",
    { method: "POST" },
  );
}

// §21 retention view attached to project outputs.
export type RetentionVideo = {
  id: string;
  filename: string;
  kind: string;
  size_bytes: number | null;
  created_at: string;
  delete_after: string;
  status: string;
  downloaded: boolean;
  download_count: number;
  deleted_at: string | null;
};

export type RetentionView = {
  headline: string;
  retention_hours: number;
  videos: RetentionVideo[];
  next_expiry_at: string | null;
} | null;

// ---------------------------------------------------------------------
// Admin monetization (Phase 8 MON — directive §12)
// ---------------------------------------------------------------------

export type AdminBillingReports = {
  revenue: {
    total_verified_cents: number;
    by_status: Record<string, number>;
  };
  mrr_contracted_cents: number;
  arr_contracted_cents: number;
  subscriptions_by_plan: Record<string, number>;
  arpu_cents: number | null;
  credits: {
    outstanding: { subscription: number; purchased: number; promo: number };
    consumed_total: number;
  };
  production_cost: Record<string, unknown>;
};

export type AdminRetentionSummary = {
  total_videos: number;
  total_bytes: number;
  active: number;
  deleted: number;
  delete_failed: number;
  expired_awaiting_deletion: number;
  by_user: { user_id: string; videos: number; bytes: number }[];
  by_project: { project_id: string; videos: number; bytes: number }[];
  failures: {
    id: string;
    project_id: string;
    filename: string;
    delete_attempts: number;
    last_error: string | null;
  }[];
};

export function getAdminBillingPlans(signal?: AbortSignal) {
  return apiRequest<{ plans: BillingPlan[] }>("/api/v1/admin/billing/plans", {
    signal,
  });
}

export function updateAdminBillingPlan(
  planId: string,
  patch: Record<string, unknown>,
) {
  return apiRequest<BillingPlan>(`/api/v1/admin/billing/plans/${planId}`, {
    method: "POST",
    body: JSON.stringify(patch),
  });
}

export function getAdminBillingReports(signal?: AbortSignal) {
  return apiRequest<AdminBillingReports>("/api/v1/admin/billing/reports", {
    signal,
  });
}

export function getAdminBillingRetention(signal?: AbortSignal) {
  return apiRequest<AdminRetentionSummary>(
    "/api/v1/admin/billing/retention",
    { signal },
  );
}

export function retryAdminRetention(videoId: string) {
  return apiRequest<{ video: RetentionVideo }>(
    `/api/v1/admin/billing/retention/${videoId}/retry`,
    { method: "POST" },
  );
}

export function getAdminBillingSubscriptions(signal?: AbortSignal) {
  return apiRequest<{ subscriptions: Record<string, unknown>[] }>(
    "/api/v1/admin/billing/subscriptions",
    { signal },
  );
}

export function getAdminBillingPayments(signal?: AbortSignal) {
  return apiRequest<{ payments: Record<string, unknown>[] }>(
    "/api/v1/admin/billing/payments",
    { signal },
  );
}

/**
 * Call a legacy (pre-v1) endpoint through the same client.
 *
 * IA-10 / F-06: KEPT deliberately. It has no active caller, but
 * the quarantined implementations under frontend/preserved/ call
 * it and are part of the typecheck; removing it would break
 * their compilation. (The other F-06 dead export, getHealth,
 * had no caller anywhere — preserved code included — and was
 * removed. listRevisionExports was removed in the same pass and
 * returned in redesign F2 with its caller, the revision panel —
 * see UI-001 at its definition.)
 */
export function legacyRequest<T>(path: string, init: RequestOptions = {}): Promise<T> {
  return request<T>(path, init, false);
}

// ---------------------------------------------------------------------
// v1 types (mirror API_CONTRACT.md)
// ---------------------------------------------------------------------

export type RunSummary = {
  run_id: string;
  project_id: string;
  status: string;
  stage?: string | null;
  script_hash?: string | null;
  error?: string | null;
  cancel_requested?: boolean;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
};

export type ProjectSummary = {
  id: string;
  title?: string | null;
  category: string;
  status: string;
  created_at: string;
  script_preview: string;
  current_stage?: string | null;
  video_url?: string | null;
  latest_run?: RunSummary | null;
  /** Collaboration (#11): the caller's role on this project —
   * "owner" for their own projects, the member role for shared
   * ones; null where the backend did not resolve it. */
  access_role?: string | null;
};

export type ProjectDetail = ProjectSummary & {
  script: string;
  options: Record<string, unknown>;
};

export type StageState = { status?: string; error?: string | null };

export type ProjectStatus = {
  id: string;
  title?: string | null;
  category: string;
  status: string;
  created_at: string;
  current_stage?: string | null;
  stages?: Record<string, StageState> | null;
  progress?: unknown;
  latest_run?: RunSummary | null;
  video_url?: string | null;
  error?: string | null;
};

export type RunSubmission = {
  run: RunSummary | null;
  deduplicated: boolean;
  job_id: string | null;
  phase: "analysis" | "production";
};

export type ProposedVisual = {
  description: string;
  kind?: string | null;
  specificity?: string | null;
  format_intent?: string | null;
  search_terms: string[];
} | null;

/** B-roll B1: the sentence's fallback concept set — what a camera
 * could show if the exact subject could not be filmed (activities,
 * objects, settings, place types, mood, era terms), recorded by the
 * planner and displayed, not yet searched. */
export type FallbackConcepts = {
  activities: string[];
  objects: string[];
  settings: string[];
  place_types: string[];
  mood: string[];
  era_terms: string[];
} | null;

/** Master Directive Phase 2 (§4.2): the sentence's visual-intention
 * record — what the viewer should see while the sentence is spoken,
 * decided by the planner (LLM or deterministic derivation) before
 * any retrieval. Seeded from the fallback concepts above. */
export type VisualIntents = {
  primary_visual_intent: string;
  secondary_visual_intents: string[];
  intent_queries: {
    literal: string[];
    activity_broll: string[];
    establishing: string[];
    contextual: string[];
    archival: string[];
  };
  graphic_fallback: { allowed: boolean; kind: string | null } | null;
  preferred_media_type?: string | null;
  specificity_requirement?: string | null;
  visual_misrepresentation_risk?: string | null;
  intents_source?: string | null;
} | null;

export type ChosenVisual = {
  type?: string | null;
  provider?: string | null;
  source_url?: string | null;
  title?: string | null;
  file_name?: string | null;
  assignment?: string | null;
  relevance_status?: string | null;
  media_key?: string | null;
  media_url?: string | null;
} | null;

export type PlanSentence = {
  sentence_number: number;
  scene_number?: number | null;
  text: string;
  role?: string | null;
  subject_id?: string | null;
  subject?: string | null;
  entities: string[];
  proposed_visual: ProposedVisual;
  fallback_concepts?: FallbackConcepts;
  concepts_source?: string | null;
  visual_intents?: VisualIntents;
  chosen_visual: ChosenVisual;
  evidence_class?: string | null;
  rights?: {
    license?: string;
    rights_status?: string | null;
    creator?: string | null;
    source?: string | null;
  } | null;
  coverage: "verified" | "rotation" | "graphic" | "missing" | "uncovered";
  decisions: PlanDecision[];
};

export type PlanDecision = {
  sentence_number: number;
  action: "approve" | "replace" | "regenerate";
  recorded_at: string;
  outcome?: string;
  outcome_note?: string;
  replacement?: { file_path?: string | null; source_url?: string | null; title?: string | null } | null;
  refreshed_asset?: Record<string, unknown> | null;
  [key: string]: unknown;
};

/** Master Directive Phase 7 / P0: the planner self-audit block the
 * plan document carries — which planner produced the plan, why it
 * degraded when it did, and its measured quality. */
export type PlannerBlock = {
  mode?: string | null;
  planner_version?: string | null;
  degraded_reason?: string | null;
  degraded_sentences?: number | null;
  sentences?: number | null;
  quality?: Record<string, unknown> | null;
  [key: string]: unknown;
};

export type PlanDocument = {
  project_id: string;
  title?: string | null;
  plan_source: "plan_json" | "story_cache";
  analysis_mode?: string | null;
  planner?: PlannerBlock | null;
  bible: {
    title?: string | null;
    story_type?: string | null;
    time_period?: string | null;
    primary_subject_id?: string | null;
    entity_count: number;
  };
  scenes: { scene_number: number; sentence_numbers: number[] }[];
  sentences: PlanSentence[];
  summary: {
    sentences: number;
    verified: number;
    rotation: number;
    graphic: number;
    missing: number;
    uncovered: number;
    decisions_recorded: number;
  };
};

export type AssetDecisionRow = {
  id: string;
  asset_key?: string | null;
  content_hash?: string | null;
  source_url?: string | null;
  provider?: string | null;
  scene_number?: number | null;
  sentence_number?: number | null;
  interval_start?: number | null;
  interval_duration?: number | null;
  evidence_class?: string | null;
  verification?: Record<string, unknown> | null;
  rights?: Record<string, unknown> | null;
  decision_type: string;
  previous_decision_id?: string | null;
  created_at?: string | null;
};

export type AssetsPage = {
  run_id: string | null;
  total: number;
  limit: number;
  offset: number;
  items: AssetDecisionRow[];
};

export type OutputEntry = {
  exists: boolean;
  filename: string;
  size_bytes?: number;
  media_key?: string | null;
  media_url?: string;
};

export type ProjectOutputs = {
  project_id: string;
  film: OutputEntry;
  captions: { json: OutputEntry; srt: OutputEntry; vtt: OutputEntry };
  asset_report: OutputEntry;
  rights_manifest: OutputEntry;
  run_manifest: OutputEntry;
  certificate_url: string | null;
  plan_url: string | null;
  // Phase 8 (§21): generated-video retention registry view.
  retention?: RetentionView;
};

/**
 * Quality Recovery Phase 5: the production quality section the
 * certificate endpoint attaches from the run's recorded
 * production_report.json + visual_quality.json (Phase 3). `state`
 * is "recorded" only when a report was actually recorded;
 * "not_recorded" (the production predates the report, or its
 * measurement failed and was recorded in the asset report instead)
 * and "unavailable" (the artefact exists but cannot be read) both
 * carry null sections — the UI shows those states honestly and
 * never renders them as a verdict. Verdict values are the
 * backend's own: PASS / QUALITY_WARNING / FAIL / UNKNOWN, or null
 * where a verdict was not computable.
 */
export type QualityVerdict = {
  verdict?: string | null;
  failures?: string[];
  warnings?: string[];
  fail_reasons?: string[];
  warning_reasons?: string[];
  unknown_metrics?: string[];
};

export type CertificateQuality = {
  state: string;
  schema?: string | null;
  verdicts?: {
    integrity?: QualityVerdict | null;
    quality?: QualityVerdict | null;
    reliability?: QualityVerdict | null;
  } | null;
  metrics?: {
    diversity?: {
      d1_unique_assets?: number | null;
      d1_unique_assets_per_minute?: number | null;
      d2_repeated_items?: number | null;
      d2_repeated_item_rate?: number | null;
      variety_ratio?: number | null;
    } | null;
    sentence_outcomes?: {
      sentence_count?: number | null;
      buckets?: Record<string, number> | null;
      shares?: Record<string, number | null> | null;
      reconciles?: boolean | null;
      sentences_without_verified_visual?: number | null;
    } | null;
    timeline_item_count?: number | null;
    max_reuse_count?: number | null;
  } | null;
  reconciliation?: {
    all_reconciled?: boolean | null;
    failures?: string[];
    checks?: Array<{ name?: string | null; status?: string | null }>;
  } | null;
};

export type Certificate = {
  certificate_version: number;
  /** Present on the run certificate only (not on revision certificates). */
  quality?: CertificateQuality | null;
  project: { id: string; title?: string | null; category?: string | null };
  run: {
    run_id: string | null;
    status?: string | null;
    script_hash?: string | null;
    created_at?: string | null;
    finished_at?: string | null;
  };
  summary: {
    by_type: Record<string, number>;
    rights_recorded: number;
    rights_unknown: number;
    total: number;
  };
  assets: Array<{
    sentence_number?: number | null;
    scene_number?: number | null;
    decision_type: string;
    asset_key?: string | null;
    content_hash?: string | null;
    provider?: string | null;
    source_url?: string | null;
    interval: { start?: number | null; duration?: number | null };
    evidence_class?: string | null;
    /** Phase 1 / AM-014: the asset's recorded title (null on pre-Phase-1 rows). */
    title?: string | null;
    verification?: {
      accepted?: boolean;
      reason?: string | null;
      transcript_check?: string | null;
      /** Phase 1 / AM-013: a swap applied despite a rejected fresh verdict. */
      swap_override?: {
        applied_despite_rejection?: boolean;
        rejection_reason?: string;
        override_reason?: string;
      } | null;
    } | null;
    rights?: {
      license: string;
      /** Phase 1 / AM-014: licence evidence URL + retrieval timestamp. */
      license_url?: string | null;
      rights_status?: string | null;
      creator?: string | null;
      source?: string | null;
      retrieved_at?: string | null;
    } | null;
    recorded_at?: string | null;
  }>;
  disclaimers: string[];
};

// ---------------------------------------------------------------------
// v1 endpoints
// ---------------------------------------------------------------------

export function listProjects(signal?: AbortSignal) {
  return apiRequest<{ projects: ProjectSummary[] }>("/api/v1/projects", { signal });
}

// ---------------------------------------------------------------------
// Catalogues (IA-06) — read-only views over the backend's real
// sources of truth: genre packs (services/genre_packs) and the
// installed voice catalogue (services/narration.VOICE_CATALOGUE).
// ---------------------------------------------------------------------

export type GenreCatalogueEntry = {
  id: string;
  name?: string | null;
  label?: string | null;
  description?: string | null;
  style_family?: string | null;
  graphics_motifs: string[];
  format_preferences: Record<string, number>;
  entity_handling: string[];
  research_query_templates: string[];
  pacing: Record<string, unknown>;
};

export type VoiceCatalogueEntry = {
  id: string;
  is_default: boolean;
  alias_of?: string | null;
  model?: string | null;
  model_file?: string | null;
  config_file?: string | null;
  installed: boolean;
};

export function getGenreCatalogue(signal?: AbortSignal) {
  return apiRequest<{ genres: GenreCatalogueEntry[] }>("/api/v1/catalogues/genres", { signal });
}

/** Item 6: a language and its honest support facts. */
export type LanguageCatalogueEntry = {
  code: string;
  name: string;
  narration_supported: boolean;
  voices: { id: string; language: string; label: string }[];
  script_supported: boolean;
  translation_supported: boolean;
};

export function getLanguageCatalogue(signal?: AbortSignal) {
  return apiRequest<{
    languages: LanguageCatalogueEntry[];
    default_language: string;
  }>("/api/v1/catalogues/languages", { signal });
}

export function getVoiceCatalogue(signal?: AbortSignal) {
  return apiRequest<{ voices: VoiceCatalogueEntry[]; default_voice_id: string }>(
    "/api/v1/catalogues/voices",
    { signal },
  );
}

export function getProject(projectId: string, signal?: AbortSignal) {
  return apiRequest<ProjectDetail>(`/api/v1/projects/${projectId}`, { signal });
}

export function getProjectStatus(projectId: string, signal?: AbortSignal) {
  return apiRequest<ProjectStatus>(`/api/v1/projects/${projectId}/status`, { signal });
}

export function createProject(body: {
  title?: string;
  script: string;
  category?: string;
  options?: Record<string, unknown>;
}) {
  return apiRequest<ProjectDetail>("/api/v1/projects", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function submitRun(projectId: string, options?: Record<string, unknown>) {
  return apiRequest<RunSubmission>(`/api/v1/projects/${projectId}/runs`, {
    method: "POST",
    body: JSON.stringify({ options: options ?? {} }),
  });
}

export function listRuns(projectId: string, signal?: AbortSignal) {
  return apiRequest<{ runs: RunSummary[] }>(`/api/v1/projects/${projectId}/runs`, { signal });
}

export function getRun(runId: string, signal?: AbortSignal) {
  return apiRequest<RunSummary>(`/api/v1/runs/${runId}`, { signal });
}

export function cancelRun(runId: string) {
  return apiRequest<{ run_id: string; cancel_requested: boolean; run: RunSummary | null }>(
    `/api/v1/runs/${runId}/cancel`,
    { method: "POST" },
  );
}

export function getPlan(projectId: string, signal?: AbortSignal) {
  return apiRequest<PlanDocument>(`/api/v1/projects/${projectId}/plan`, { signal });
}

export function generatePlan(projectId: string) {
  return apiRequest<PlanDocument>(`/api/v1/projects/${projectId}/plan/generate`, {
    method: "POST",
  });
}

export function postPlanDecision(
  projectId: string,
  body: {
    sentence_number: number;
    action: "approve" | "replace" | "regenerate";
    replacement?: { file_path?: string; source_url?: string; title?: string };
  },
) {
  return apiRequest<PlanDecision>(`/api/v1/projects/${projectId}/plan/decisions`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function approvePlan(projectId: string) {
  return apiRequest<{
    run: RunSummary | null;
    job_id: string | null;
    released: boolean;
    regenerate_sentences: number[];
  }>(`/api/v1/projects/${projectId}/plan/approve`, { method: "POST" });
}

export function listAssets(projectId: string, limit = 100, offset = 0, signal?: AbortSignal) {
  return apiRequest<AssetsPage>(
    `/api/v1/projects/${projectId}/assets?limit=${limit}&offset=${offset}`,
    { signal },
  );
}

export function getOutputs(projectId: string, signal?: AbortSignal) {
  return apiRequest<ProjectOutputs>(`/api/v1/projects/${projectId}/outputs`, { signal });
}

export function getCertificate(projectId: string, signal?: AbortSignal) {
  return apiRequest<Certificate>(`/api/v1/projects/${projectId}/certificate`, { signal });
}

// ---------------------------------------------------------------------
// QA Report (Phase F, founder priority #8) — the unified Documentary
// Quality Report over the artefacts a production recorded
// (services/qa_report.py). Assembly only: every value is the
// recorded value, every section carries its state
// ("recorded" / "not_recorded" / "unavailable"), and a metric that
// was not recorded is null — the UI shows those states honestly
// and never renders an absence as a pass.
// ---------------------------------------------------------------------

export type QaMetric = {
  value?: number | null;
  band?: string | null;
  basis?: string | null;
  thresholds?: string | null;
};

export type QaVerdicts = {
  integrity?: (QualityVerdict & Record<string, unknown>) | null;
  quality?:
    | (QualityVerdict & { metrics?: Record<string, QaMetric> | null })
    | null;
  reliability?:
    | { verdict?: string | null; basis?: Record<string, unknown> | null; facts?: Record<string, unknown> | null }
    | null;
};

export type QaSection = { state: string };

export type QaReport = {
  schema: string;
  project_id: string;
  run: {
    run_id: string | null;
    status?: string | null;
    stage?: string | null;
    error?: string | null;
    created_at?: string | null;
    started_at?: string | null;
    finished_at?: string | null;
  };
  artefacts: Record<string, string>;
  verdicts: QaSection & { schema?: string | null; verdicts?: QaVerdicts | null };
  reconciliation: QaSection & {
    all_reconciled?: boolean | null;
    failures?: string[] | null;
    checks?:
      | Array<{
          name?: string | null;
          status?: string | null;
          detail?: string | null;
          values?: Record<string, unknown> | null;
        }>
      | null;
  };
  questions: QaSection & {
    questions?:
      | Array<{
          id?: string | null;
          question?: string | null;
          answer?: unknown;
          state?: string | null;
        }>
      | null;
  };
  coverage: QaSection & {
    sentence_outcomes?:
      | {
          sentence_count?: number | null;
          buckets?: Record<string, number> | null;
          shares?: Record<string, number | null> | null;
          reconciles?: boolean | null;
          sentences_without_verified_visual?: number | null;
        }
      | null;
    asset_report_coverage?:
      | {
          total_sentences?: number | null;
          covered_sentences?: number | null;
          uncovered_sentences?: number[] | null;
          complete?: boolean | null;
        }
      | null;
    uncovered_beats?:
      | { count?: number | null; sentence_numbers?: number[] | null }
      | null;
  };
  tiers: QaSection & {
    tier_accounting?:
      | {
          state?: string | null;
          by_tier?: Record<
            string,
            { count?: number | null; runtime_seconds?: number | null; runtime_share?: number | null }
          > | null;
          broll_runtime_seconds?: number | null;
          broll_share?: number | null;
          unstamped_items?: number | null;
          band?:
            | {
                state?: string | null;
                genre_pack?: string | null;
                max?: number | null;
                basis?: string | null;
                measured?: number | null;
                evaluation?: string | null;
              }
            | null;
        }
      | null;
  };
  diversity: QaSection & { diversity?: Record<string, number | string | null> | null };
  relevance: QaSection & { relevance?: Record<string, unknown> | null };
  genre_expectations: QaSection & { genre_expectations?: Record<string, unknown> | null };
  runtime_share: QaSection & {
    runtime_share?: Record<string, { seconds?: number | null; fraction?: number | null }> | null;
    runtime_share_by_format?: Record<string, unknown> | null;
  };
  frame_verification: QaSection & {
    summary?:
      | {
          evaluated_assets?: number | null;
          rejected_events?: number | null;
          watermarked_preview_detected?: number | null;
          ocr_engine?: string | null;
        }
      | null;
    integrity_categories?: Record<string, unknown> | null;
    rejections?:
      | Array<{
          candidate_id?: string | null;
          scene?: number | null;
          sentence_number?: number | null;
          provider?: string | null;
          kind?: string | null;
          url?: string | null;
          outcome?: string | null;
          reason?: string | null;
          primary_rejection_reason?: string | null;
          verdict_reasons?: string[] | null;
        }>
      | null;
  };
  sentences: QaSection & {
    sentences: Array<{
      sentence_number: number;
      scene_number?: number | null;
      outcome?: string | null;
      tier?: string | null;
      missing_visual?:
        | { reason?: string | null; role?: string | null; graphics_outcome?: Record<string, unknown> | null }
        | null;
    }>;
  };
  candidates: QaSection & {
    totals?: Record<string, number | null> | null;
    funnel?: {
      by_outcome?: Record<string, number> | null;
      by_stage?: Record<string, number> | null;
      rejection_funnel?: Record<string, unknown> | null;
    } | null;
    rejection_reasons?:
      | { distribution?: Record<string, number> | null; total?: number | null }
      | null;
  };
  defects: QaSection & {
    substitutions?: Record<string, number> | null;
    authenticity?: Record<string, unknown> | null;
    visual_quality_error?: unknown;
    run_failure?: unknown;
  };
  audio_captions: QaSection & {
    final_audio?:
      | {
          integrated_lufs?: number | null;
          true_peak_dbtp?: number | null;
          target_lufs?: number | null;
          normalised?: boolean | null;
        }
      | null;
    timing_consistency?:
      | {
          divergence_seconds?: number | null;
          tolerance_seconds?: number | null;
          within_tolerance?: boolean | null;
          timing_source?: string | null;
        }
      | null;
    captions_alignment?:
      | { aligned?: number | null; low_alignment?: number | null; alignment_failed?: number | null }
      | null;
  };
  rights: QaSection & {
    recorded?: { recorded_licences?: number | null; unknown_licences?: number | null } | null;
    unknown_licensing?: number | null;
    note?: string | null;
  };
  delivery: QaSection & {
    status?: string | null;
    decode_validated?: boolean | null;
    duration_seconds?: number | null;
    profile?: string | null;
    master_bitrate_kbps?: number | null;
    delivery_bitrate_kbps?: number | null;
    size_ratio?: number | null;
  };
};

export function getQaReport(projectId: string, signal?: AbortSignal) {
  return apiRequest<QaReport>(`/api/v1/projects/${projectId}/qa-report`, { signal });
}

export function getRunQaReport(projectId: string, runId: string, signal?: AbortSignal) {
  return apiRequest<QaReport>(
    `/api/v1/projects/${projectId}/runs/${runId}/qa-report`,
    { signal },
  );
}

export function qaReportExportPath(projectId: string, runId: string) {
  return `/api/v1/projects/${projectId}/runs/${runId}/qa-report/export`;
}

// ---------------------------------------------------------------------
// Thumbnail Generator (founder priority #13) — compositional
// thumbnails over the project's own recorded material
// (services/thumbnail_studio.py): film frames, verified stills and
// uploads composed at YouTube's 1280x720 spec. The checks are
// mechanical measurements of the finished file — there is no
// performance prediction anywhere in this surface.
// ---------------------------------------------------------------------

export type ThumbnailBase = {
  base_id: string;
  kind: "film_frame" | "asset_still" | "upload";
  label: string;
  run_id?: string | null;
  timestamp_seconds?: number | null;
  decision_id?: string | null;
  upload_id?: string | null;
  file_name?: string | null;
  provider?: string | null;
  scene_number?: number | null;
  sentence_number?: number | null;
};

export type ThumbnailChecks = {
  dimensions: [number, number];
  format: string;
  file_size_bytes: number;
  under_youtube_size_limit: boolean;
  headline_words: number;
  headline_characters: number;
  headline_contrast_ratio: number;
  headline_font_height_px_at_10_percent_scale: number;
};

export type ThumbnailVariant = {
  id: string;
  created_at: string;
  file_name: string;
  base: {
    kind: string;
    run_id?: string | null;
    timestamp_seconds?: number | null;
    decision_id?: string | null;
    upload_id?: string | null;
    file_name?: string | null;
    provider?: string | null;
  };
  base_id: string;
  headline: string;
  layout: string;
  accent: string;
  checks: ThumbnailChecks;
  available: boolean;
};

export type ThumbnailStudioView = {
  schema: string;
  project_id: string;
  spec: {
    width: number;
    height: number;
    max_bytes: number;
    layouts: string[];
    accents: string[];
    max_headline_words: number;
    max_headline_characters: number;
  };
  brief: {
    title?: string | null;
    category?: string | null;
    story_type?: string | null;
    case_bible_state: string;
    primary_subject: { name?: string | null; type?: string | null; role?: string | null } | null;
    recorded_facts: Array<{ kind: string; display: string; sentence_number?: number | null }>;
    headline_suggestions: string[];
  };
  bases: {
    film: {
      state: string;
      run_id?: string | null;
      duration_seconds?: number | null;
      timestamps_source: string;
      frames: ThumbnailBase[];
    };
    stills: ThumbnailBase[];
    uploads: ThumbnailBase[];
  };
  variants: ThumbnailVariant[];
};

export type ThumbnailAnalysis = {
  base: Record<string, unknown>;
  base_id: string;
  label: string;
  dimensions: [number, number];
  ocr: { engine?: string | null; state: string; lines: string[]; word_count: number };
  palette: Array<{ hex: string; share: number }>;
  luminance: { mean: number; spread: number };
};

export function getThumbnails(projectId: string, signal?: AbortSignal) {
  return apiRequest<ThumbnailStudioView>(`/api/v1/projects/${projectId}/thumbnails`, { signal });
}

export function composeThumbnail(
  projectId: string,
  body: { base_id: string; headline?: string; layout: string; accent: string },
) {
  return apiRequest<ThumbnailVariant>(`/api/v1/projects/${projectId}/thumbnails`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function generateThumbnailVariants(projectId: string, headline?: string) {
  return apiRequest<{ created: ThumbnailVariant[]; skipped: Array<{ base_id: string; layout: string; code: string; message: string }> }>(
    `/api/v1/projects/${projectId}/thumbnails/variants`,
    { method: "POST", body: JSON.stringify({ headline: headline ?? null }) },
  );
}

export function analyseThumbnailBase(projectId: string, baseId: string) {
  return apiRequest<ThumbnailAnalysis>(`/api/v1/projects/${projectId}/thumbnails/analyse`, {
    method: "POST",
    body: JSON.stringify({ base_id: baseId }),
  });
}

export function suggestThumbnailHeadlines(projectId: string) {
  return apiRequest<{ proposals: string[]; basis: string }>(
    `/api/v1/projects/${projectId}/thumbnails/suggest-headlines`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function deleteThumbnail(projectId: string, thumbId: string) {
  return apiRequest<{ deleted: string }>(`/api/v1/projects/${projectId}/thumbnails/${thumbId}`, {
    method: "DELETE",
  });
}

export function thumbnailImagePath(projectId: string, thumbId: string) {
  return `/api/v1/projects/${projectId}/thumbnails/${thumbId}/image`;
}

// ---------------------------------------------------------------------
// Channel research (directive 2026-11-26): real channel data via
// the backend's yt-dlp extraction — statistics exactly as
// reported, analysis labelled by basis. Plus the Thumbnail
// Studio's channel style measurement.
// ---------------------------------------------------------------------

export type ChannelVideo = {
  id: string;
  title?: string | null;
  url?: string | null;
  view_count?: number | null;
  like_count?: number | null;
  duration_seconds?: number | null;
  upload_date?: string | null;
  thumbnail?: string | null;
};

export type ChannelResearchRecord = {
  id: string;
  channel: {
    id?: string | null;
    title?: string | null;
    url?: string | null;
    subscriber_count?: number | null;
  };
  videos: ChannelVideo[];
  ordering_basis: string;
  analysis: null | {
    at: string;
    basis: Record<string, string>;
    videos: Array<{
      id: string;
      title?: string | null;
      basis: string;
      view_count?: number | null;
      duration_seconds?: number | null;
    }>;
    patterns: Array<{ pattern: string; basis: string; evidence: string }>;
    ideas: Array<{ id: string; title: string; angle: string; why_it_fits: string }>;
  };
  created_at: string;
};

export function resolveChannelResearch(channelUrl: string) {
  return apiRequest<ChannelResearchRecord>(
    "/api/v1/script-studio/channel-research",
    { method: "POST", body: JSON.stringify({ channel_url: channelUrl }) },
  );
}

export function listChannelResearch(signal?: AbortSignal) {
  return apiRequest<{ records: ChannelResearchRecord[] }>(
    "/api/v1/script-studio/channel-research",
    { signal },
  );
}

export function analyseChannelResearch(researchId: string, videoIds: string[]) {
  return apiRequest<ChannelResearchRecord>(
    `/api/v1/script-studio/channel-research/${researchId}/analyse`,
    { method: "POST", body: JSON.stringify({ video_ids: videoIds }) },
  );
}

export type ChannelStyleVideo = ChannelVideo & {
  measurement: null | {
    dimensions: [number, number];
    ocr: { engine?: string | null; state: string; lines: string[]; word_count: number };
    palette: Array<{ hex: string; share: number }>;
    luminance: { mean: number; spread: number };
  };
  measurement_error?: string | null;
};

// ---------------------------------------------------------------------
// Affiliate programme (item 1): 12% lifetime commission on
// verified payments by referred customers. The dashboard reads
// the real ledger; no payout provider is connected.
// ---------------------------------------------------------------------

export type AffiliateEntry = {
  id: string;
  kind: string;
  gross_cents: number;
  rate_percent: number;
  commission_cents: number;
  status: string;
  referred_user_id: string;
  payment_id?: string | null;
  decision_reason?: string | null;
  created_at?: string | null;
};

export type AffiliateDashboard = {
  enrolled: boolean;
  code?: string;
  status?: string;
  rate_percent: number;
  lifetime: boolean;
  payout_preference?: string | null;
  payout_preferences?: string[];
  payout_provider_connected?: boolean;
  metrics?: {
    link_clicks: number;
    registrations: number;
    referred_customers: number;
    active_customers: number;
    conversion_percent: number | null;
    lifetime_earnings_cents: number;
    pending_cents: number;
    approved_cents: number;
    paid_cents: number;
    reversed_cents: number;
  };
  recent_activity?: AffiliateEntry[];
  payout_history?: AffiliateEntry[];
};

export type AdminAffiliateRow = {
  id: string;
  user_id: string;
  email?: string | null;
  code: string;
  status: string;
  payout_preference?: string | null;
  accrued_cents: number;
  reversed_cents: number;
  pending_cents: number;
};

export function getAdminAffiliates(signal?: AbortSignal) {
  return apiRequest<{ affiliates: AdminAffiliateRow[]; rate_percent: number }>(
    "/api/v1/admin/affiliates",
    { signal },
  );
}

export function getAdminAffiliateEntries(signal?: AbortSignal) {
  return apiRequest<{ entries: AffiliateEntry[] }>(
    "/api/v1/admin/affiliates/entries",
    { signal },
  );
}

export function decideAffiliateEntry(
  entryId: string,
  decision: "approve" | "reject" | "paid",
  reason?: string,
) {
  return apiRequest<{ id: string; status: string }>(
    `/api/v1/admin/affiliates/entries/${entryId}/decision`,
    {
      method: "POST",
      body: JSON.stringify({ decision, reason: reason || undefined }),
    },
  );
}

export function setAdminAffiliateStatus(
  affiliateId: string,
  status: "active" | "suspended",
) {
  return apiRequest<{ id: string; status: string }>(
    `/api/v1/admin/affiliates/${affiliateId}/status`,
    { method: "POST", body: JSON.stringify({ status }) },
  );
}

export function adminAffiliateExportPath() {
  return "/api/v1/admin/affiliates/export";
}

export function getAffiliateDashboard(signal?: AbortSignal) {
  return apiRequest<AffiliateDashboard>("/api/v1/account/affiliate", {
    signal,
  });
}

export function enrollAffiliate() {
  return apiRequest<AffiliateDashboard>("/api/v1/account/affiliate/enroll", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function setAffiliatePayoutPreference(preference: string | null) {
  return apiRequest<AffiliateDashboard>(
    "/api/v1/account/affiliate/payout-preference",
    {
      method: "POST",
      body: JSON.stringify({ payout_preference: preference }),
    },
  );
}

export function recordAffiliateClick(code: string) {
  return apiRequest<{ recorded: boolean }>("/api/v1/affiliates/click", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
}

export function analyseChannelThumbnails(channelUrl: string) {
  return apiRequest<{
    channel: ChannelResearchRecord["channel"];
    ordering_basis: string;
    videos: ChannelStyleVideo[];
    label: string;
  }>("/api/v1/thumbnails/channel-analysis", {
    method: "POST",
    body: JSON.stringify({ channel_url: channelUrl }),
  });
}

export function thumbnailDownloadPath(projectId: string, thumbId: string) {
  return `/api/v1/projects/${projectId}/thumbnails/${thumbId}/download`;
}

// ---------------------------------------------------------------------
// Localization Studio (founder priority #14) — sub-phase 1 of the
// audit's phasing (services/localization.py): the project's
// recorded captions and its script translated through the LLM
// layer, caption timings preserved exactly, the English narration
// retained and labelled as retained. Dubbing is reported
// unavailable with its reasons; there is no dub action.
// ---------------------------------------------------------------------

export type LocalizationLanguage = {
  code: string;
  name: string;
};

export type LocalizationWarning = {
  segment_index: number;
  start: number;
  end: number;
  source_characters: number;
  translated_characters: number;
  reason: string;
  characters_per_second: number | null;
};

export type LocalizationArtefact = {
  id: string;
  kind: "captions" | "script";
  language: string;
  language_name: string;
  source_language: string;
  created_at: string;
  source: {
    kind: string;
    revision_id?: string | null;
    revision_number?: number | null;
    script_sha256?: string;
  };
  warnings: LocalizationWarning[];
  downloads: string[];
  files_available: Record<string, boolean>;
  segment_count?: number;
  paragraph_count?: number;
  script_draft_id?: string;
  script_text?: string;
};

export type LocalizationStudioView = {
  schema: string;
  project_id: string;
  project_title: string;
  source_language: string;
  source_language_name: string;
  ai_available: boolean;
  languages: LocalizationLanguage[];
  source: {
    captions: {
      state: string;
      source: string | null;
      revision_number: number | null;
      segment_count: number;
      language: string | null;
    };
    script: {
      state: string;
      paragraph_count: number;
      word_count: number;
    };
  };
  dubbing: {
    state: string;
    narration_language: string;
    voices: Array<{ voice_id: string; installed: boolean }>;
    reason: string;
  };
  artefacts: LocalizationArtefact[];
};

export function getLocalization(projectId: string, signal?: AbortSignal) {
  return apiRequest<LocalizationStudioView>(`/api/v1/projects/${projectId}/localization`, { signal });
}

export function localizeCaptions(projectId: string, language: string) {
  return apiRequest<LocalizationArtefact>(`/api/v1/projects/${projectId}/localization/captions`, {
    method: "POST",
    body: JSON.stringify({ language }),
  });
}

export function localizeScript(projectId: string, language: string) {
  return apiRequest<LocalizationArtefact>(`/api/v1/projects/${projectId}/localization/script`, {
    method: "POST",
    body: JSON.stringify({ language }),
  });
}

export function deleteLocalizationArtefact(projectId: string, artefactId: string) {
  return apiRequest<{ deleted: string; kind: string; script_draft_kept?: boolean }>(
    `/api/v1/projects/${projectId}/localization/${artefactId}`,
    { method: "DELETE" },
  );
}

export function localizationDownloadPath(projectId: string, artefactId: string, kind: string) {
  return `/api/v1/projects/${projectId}/localization/${artefactId}/download?kind=${kind}`;
}

// ---------------------------------------------------------------------
// Project Intelligence (founder priority #10) — the assembly layer
// over the persistent project record (services/project_intelligence
// .py). Recorded data passes through as recorded (bible entities,
// QA verdicts, provider funnels); derived data (entities shared
// across Case Bibles, timeline mentions) carries its derivation
// text; recommendations carry the recorded figures they rest on.
// ---------------------------------------------------------------------

export type IntelligenceProject = {
  project_id: string;
  title?: string | null;
  category?: string | null;
  status?: string | null;
  created_at?: string | null;
};

export type IntelligenceVerdicts = {
  state: string;
  integrity?: string | null;
  quality?: string | null;
  reliability?: string | null;
  delivery_status?: string | null;
};

export type IntelligenceEntity = {
  entity_id?: string | null;
  canonical?: string | null;
  type?: string | null;
  effective_canonical?: string | null;
  effective_type?: string | null;
  role?: string | null;
  support?: string | null;
  is_primary_subject: boolean;
  corrections: number;
  kb_check?: { status?: string | null; conflict: boolean; kb_type?: string | null } | null;
};

export type IntelligenceSharedEntity = {
  canonical?: string | null;
  type?: string | null;
  project_count: number;
  derivation: string;
  this_project?: Record<string, unknown> | null;
  other_projects?: { project_id: string; project_title?: string | null; entity_id?: string | null }[];
  appearances?: Record<string, unknown>[];
};

export type IntelligenceFinding = {
  finding: string;
  evidence: Record<string, unknown>;
  suggestion: string;
  link?: string | null;
};

export type IntelligenceRule = {
  rule: string;
  state: string;
  run_id?: string;
  findings: IntelligenceFinding[];
  findings_total?: number;
  note?: string | null;
};

export type ProjectIntelligence = {
  schema: string;
  project: IntelligenceProject | null;
  record: {
    runs: { total: number; production: number; by_status: Record<string, number> };
    latest_production_run?: { run_id: string; status?: string | null; created_at?: string | null } | null;
    bible_recorded: boolean;
    story_type?: string | null;
    research_counts?: Record<string, number> | null;
  };
  quality: IntelligenceVerdicts;
  entities: IntelligenceEntity[];
  connections: {
    primary_subject_id?: string | null;
    shared_entities: IntelligenceSharedEntity[];
    timeline_mentions: {
      entity_id?: string | null;
      canonical?: string | null;
      type?: string | null;
      events: { when?: string | null; what?: string | null; support?: string | null; matched_name?: string | null }[];
      derivation: string;
    }[];
    kb_conflicts: IntelligenceEntity[];
  };
  recommendations: { rules: IntelligenceRule[]; findings_total: number };
};

export type AccountIntelligence = {
  schema: string;
  totals: {
    projects: number;
    runs: number;
    projects_with_bible: number;
    distinct_entities: number;
    shared_entities: number;
  };
  projects_by_category: { category: string; projects: number }[];
  quality_by_category: {
    category: string;
    projects: number;
    with_recorded_verdict: number;
    quality_verdicts: Record<string, number>;
  }[];
  providers: {
    state: string;
    projects_with_funnel: number;
    providers: {
      provider: string;
      discovered: number;
      verified: number;
      rejected: number;
      downloaded: number;
      download_failed: number;
      project_count: number;
    }[];
    derivation: string;
  };
  entity_connections: IntelligenceSharedEntity[];
  projects: (IntelligenceProject & {
    run_count: number;
    bible_recorded: boolean;
    entity_count: number;
    quality?: IntelligenceVerdicts | null;
  })[];
};

export function getProjectIntelligence(projectId: string, signal?: AbortSignal) {
  return apiRequest<ProjectIntelligence>(`/api/v1/projects/${projectId}/intelligence`, { signal });
}

export function getAccountIntelligence(signal?: AbortSignal) {
  return apiRequest<AccountIntelligence>(`/api/v1/intelligence`, { signal });
}

// ---------------------------------------------------------------------
// Research Studio (Phase D, founder priority #6) — the workspace
// over services/research_studio.py. Every type mirrors the recorded
// shapes the service returns; nothing here invents a field.
// ---------------------------------------------------------------------

export type ResearchSourceState = "candidate" | "kept" | "discarded";

export type ResearchSource = {
  id: string;
  session_ids: string[];
  title: string;
  url: string;
  snippet: string;
  provider: string;
  research_type?: string | null;
  query?: string | null;
  domain?: string | null;
  tier: string;
  state: ResearchSourceState;
  created_at: string;
};

export type ResearchSession = {
  id: string;
  topic: string;
  created_at: string;
  queries: { type: string; query: string }[];
  source_ids: string[];
  sources: ResearchSource[];
  kept_count: number;
  kb_verdict: {
    status?: string | null;
    type?: string | null;
    confidence?: number | null;
    type_source?: string | null;
    description?: string | null;
    evidence?: string | null;
  } | null;
  record: {
    results: number;
    new_sources: number;
    reused_sources: number;
    query_errors: { query?: string; error?: string }[];
  };
};

export type ResearchGap = { count: number; basis: string } & Record<string, unknown>;

export type ResearchOverview = {
  project_id: string;
  bible_recorded: boolean;
  counts: {
    sessions: number;
    sources: number;
    sources_kept: number;
    citations: number;
    corrections: number;
    summaries: number;
    entities: number;
    facts: number;
    facts_research_supported: number;
  };
  sessions: ResearchSession[];
  gaps: Record<string, ResearchGap>;
  tier_rubric: Record<string, string>;
};

export type ResearchEntityCheck = {
  id: string;
  entity_id: string;
  canonical?: string | null;
  bible_type?: string | null;
  kb: {
    status?: string | null;
    type?: string | null;
    confidence?: number | null;
    type_source?: string | null;
    description?: string | null;
    evidence?: string | null;
  };
  conflict: boolean;
  created_at: string;
};

export type ResearchCorrection = {
  id: string;
  entity_id: string;
  canonical?: string | null;
  field: "type" | "canonical";
  original?: string | null;
  corrected: string;
  created_at: string;
  applied_at?: string | null;
  applications: number;
};

export type ResearchEntity = {
  id: string;
  canonical?: string | null;
  type?: string | null;
  role?: string | null;
  aliases?: string[];
  description?: string | null;
  type_confidence?: number | null;
  type_source?: string | null;
  type_low_confidence?: boolean | null;
  effective_type?: string | null;
  effective_canonical?: string | null;
  corrections: ResearchCorrection[];
  kb_check?: ResearchEntityCheck | null;
};

export type ResearchTimelineEvent = {
  origin: "case_bible" | "studio";
  id?: string;
  when?: string | null;
  what?: string | null;
  support?: string | null;
  fact_basis?: string | null;
  fact_id?: string | null;
  sentence_number?: number | null;
  source_ids?: string[];
  created_at?: string;
};

export type ResearchFact = {
  fact_id: string;
  kind: string;
  when?: string | null;
  what?: string | null;
  display?: string | null;
  year?: number | null;
  sentence_number?: number | null;
  basis: string;
  source?: Record<string, unknown> | null;
  kb_check?: {
    query?: string;
    status?: string | null;
    source?: string | null;
    confidence?: number | null;
  } | null;
};

export type ResearchBible = {
  recorded: boolean;
  note?: string;
  title?: string | null;
  story_type?: string | null;
  story_era?: string | null;
  mode?: string | null;
  primary_subject_id?: string | null;
  entities: ResearchEntity[];
  timeline: ResearchTimelineEvent[];
  facts: ResearchFact[];
  fact_record?: Record<string, number> | null;
};

export type ResearchCitation = {
  id: string;
  claim: {
    kind: string;
    fact_id?: string | null;
    sentence_number?: number | null;
    label: string;
  };
  source_id: string;
  source?: ResearchSource | null;
  note?: string | null;
  created_at: string;
};

export type ResearchSummary = {
  id: string;
  scope: string;
  session_id?: string | null;
  topic: string;
  text: string;
  source_ids: string[];
  sources: ResearchSource[];
  created_at: string;
};

export function getResearchOverview(projectId: string, signal?: AbortSignal) {
  return apiRequest<ResearchOverview>(`/api/v1/projects/${projectId}/research`, { signal });
}

export function getResearchBible(projectId: string, signal?: AbortSignal) {
  return apiRequest<ResearchBible>(`/api/v1/projects/${projectId}/research/bible`, { signal });
}

export function listResearchSources(
  projectId: string,
  params: { state?: string; q?: string } = {},
  signal?: AbortSignal,
) {
  const query = new URLSearchParams();
  if (params.state) query.set("state", params.state);
  if (params.q) query.set("q", params.q);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiRequest<{
    sources: ResearchSource[];
    counts: Record<ResearchSourceState, number>;
    tiers: Record<string, number>;
    total: number;
  }>(`/api/v1/projects/${projectId}/research/sources${suffix}`, { signal });
}

export function createResearchSession(projectId: string, topic: string) {
  return apiRequest<ResearchSession>(`/api/v1/projects/${projectId}/research/sessions`, {
    method: "POST",
    body: JSON.stringify({ topic }),
  });
}

export function setResearchSourceState(
  projectId: string,
  sourceId: string,
  state: ResearchSourceState,
) {
  return apiRequest<ResearchSource>(
    `/api/v1/projects/${projectId}/research/sources/${sourceId}/state`,
    { method: "POST", body: JSON.stringify({ state }) },
  );
}

export function addResearchExternalSource(
  projectId: string,
  body: { title?: string; url: string },
) {
  return apiRequest<ResearchSource>(`/api/v1/projects/${projectId}/research/sources/external`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function runResearchFactCheck(projectId: string) {
  return apiRequest<{
    run: { id: string; created_at: string; record: Record<string, number> };
    facts: ResearchFact[];
    bases: Record<string, number>;
  }>(`/api/v1/projects/${projectId}/research/fact-check`, { method: "POST" });
}

export function checkResearchEntity(projectId: string, entityId: string) {
  return apiRequest<ResearchEntityCheck>(
    `/api/v1/projects/${projectId}/research/entities/${entityId}/check`,
    { method: "POST" },
  );
}

export function createResearchCorrection(
  projectId: string,
  body: { entity_id: string; field: "type" | "canonical"; value: string },
) {
  return apiRequest<ResearchCorrection>(`/api/v1/projects/${projectId}/research/corrections`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function deleteResearchCorrection(projectId: string, correctionId: string) {
  return apiRequest<{ deleted: boolean }>(
    `/api/v1/projects/${projectId}/research/corrections/${correctionId}`,
    { method: "DELETE" },
  );
}

export function listResearchCitations(projectId: string, signal?: AbortSignal) {
  return apiRequest<{ citations: ResearchCitation[] }>(
    `/api/v1/projects/${projectId}/research/citations`,
    { signal },
  );
}

export function createResearchCitation(
  projectId: string,
  body: {
    claim: { kind: string; fact_id?: string; sentence_number?: number; label?: string };
    source_id?: string;
    external?: { title?: string; url: string };
    note?: string;
  },
) {
  return apiRequest<ResearchCitation>(`/api/v1/projects/${projectId}/research/citations`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function deleteResearchCitation(projectId: string, citationId: string) {
  return apiRequest<{ deleted: boolean }>(
    `/api/v1/projects/${projectId}/research/citations/${citationId}`,
    { method: "DELETE" },
  );
}

export function getResearchBibliography(projectId: string, signal?: AbortSignal) {
  return apiRequest<{
    entries: {
      number: number;
      claim_label: string;
      claim_kind: string;
      source_title?: string | null;
      source_url?: string | null;
      provider?: string | null;
      tier?: string | null;
    }[];
    text: string;
  }>(`/api/v1/projects/${projectId}/research/bibliography`, { signal });
}

export function createResearchTimelineEvent(
  projectId: string,
  body: { when: string; what: string; source_ids?: string[] },
) {
  return apiRequest<ResearchTimelineEvent>(
    `/api/v1/projects/${projectId}/research/timeline-events`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function updateResearchTimelineEvent(
  projectId: string,
  eventId: string,
  body: { when?: string; what?: string; source_ids?: string[] },
) {
  return apiRequest<ResearchTimelineEvent>(
    `/api/v1/projects/${projectId}/research/timeline-events/${eventId}`,
    { method: "PUT", body: JSON.stringify(body) },
  );
}

export function deleteResearchTimelineEvent(projectId: string, eventId: string) {
  return apiRequest<{ deleted: boolean }>(
    `/api/v1/projects/${projectId}/research/timeline-events/${eventId}`,
    { method: "DELETE" },
  );
}

export function listResearchSummaries(projectId: string, signal?: AbortSignal) {
  return apiRequest<{ summaries: ResearchSummary[] }>(
    `/api/v1/projects/${projectId}/research/summaries`,
    { signal },
  );
}

export function createResearchSummary(projectId: string, sessionId?: string) {
  return apiRequest<ResearchSummary>(`/api/v1/projects/${projectId}/research/summaries`, {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId ?? null }),
  });
}

/** Absolute URL for a project's recorded media key. */
export function mediaUrl(projectId: string, mediaKey: string): string {
  return apiUrl(`/api/v1/projects/${projectId}/media/${mediaKey}`);
}

/**
 * Direct URL for streaming a recorded media file, or null when
 * streaming is unavailable (direct mode — see SAME_ORIGIN_API).
 * The URL carries no token: in same-origin mode the server-side
 * proxy injects it. The token never appears in a URL.
 */
export function streamMediaUrl(
  projectId: string,
  mediaKey: string,
): string | null {
  return SAME_ORIGIN_API ? mediaUrl(projectId, mediaKey) : null;
}

/** streamMediaUrl for a backend media path (video_url / media_url). */
export function streamMediaPathUrl(path: string): string | null {
  if (!SAME_ORIGIN_API) return null;
  const ref = parseProjectMediaPath(path);
  if (ref) return mediaUrl(ref.projectId, ref.mediaKey);
  return path.startsWith("/api/") ? apiUrl(path) : null;
}

/**
 * The content type of a recorded media file, via a HEAD request —
 * lets thumbnail components choose <video> vs <img> for a streamed
 * URL without downloading the file itself.
 */
export async function probeMediaContentType(
  projectId: string,
  mediaKey: string,
  signal?: AbortSignal,
): Promise<string> {
  const headers = new Headers();
  const response = await fetch(mediaUrl(projectId, mediaKey), {
    method: "HEAD",
    cache: "no-store",
    credentials: "include",
    headers,
    signal,
  });
  if (!response.ok) throw await parseError(response);
  return response.headers.get("content-type") || "";
}

/**
 * Fetch a recorded media file as an object URL (Brief Stage 5,
 * C-07). The media endpoint is authenticated like every v1 route,
 * and a bare <img src> cannot send the X-API-Key header — it would
 * receive the 401 JSON body, not the media. Thumbnails therefore
 * fetch through this helper (which uses mediaUrl for the address)
 * and render the resulting blob URL. Callers own revoking the URL.
 */
export async function fetchMediaObjectUrl(
  projectId: string,
  mediaKey: string,
  signal?: AbortSignal,
): Promise<{ url: string; contentType: string }> {
  const headers = new Headers();
  const response = await fetch(mediaUrl(projectId, mediaKey), {
    cache: "no-store",
    credentials: "include",
    headers,
    signal,
  });
  if (!response.ok) throw await parseError(response);
  const blob = await response.blob();
  return { url: URL.createObjectURL(blob), contentType: blob.type };
}

// ---------------------------------------------------------------------
// Authenticated media by path + downloads (IA-01, audit F-02)
// ---------------------------------------------------------------------

/**
 * Parse a backend media path of the form the API returns in
 * `video_url` / `media_url` fields
 * ("/api/v1/projects/{projectId}/media/{mediaKey}", relative or
 * absolute) into its project id and media key. Returns null for
 * any other path.
 */
export function parseProjectMediaPath(
  path: string,
): { projectId: string; mediaKey: string } | null {
  const match = path.match(/\/api\/v1\/projects\/([^/]+)\/media\/([^/?#]+)/);
  if (!match) return null;
  try {
    return {
      projectId: decodeURIComponent(match[1]),
      mediaKey: decodeURIComponent(match[2]),
    };
  } catch {
    return null;
  }
}

/**
 * Fetch a backend media path (a `video_url` / `media_url` value)
 * as an object URL, through the same authenticated path as
 * fetchMediaObjectUrl: recorded-media paths are delegated to
 * fetchMediaObjectUrl; any other backend path is fetched from
 * apiUrl with the token as the X-API-Key header only. The token
 * never appears in a URL or a log. Callers own revoking the URL.
 *
 * Recorded limitation: this downloads the whole file as a blob
 * before playback, because a bare <video src> cannot send the
 * header and the token must not travel in a URL — so the media
 * endpoint's Range support cannot be used for these previews and
 * very large films cost their full size in memory.
 */
export async function fetchAuthenticatedMediaObjectUrl(
  path: string,
  signal?: AbortSignal,
): Promise<{ url: string; contentType: string }> {
  const ref = parseProjectMediaPath(path);
  if (ref) return fetchMediaObjectUrl(ref.projectId, ref.mediaKey, signal);
  const headers = new Headers();
  const response = await fetch(apiUrl(path), {
    cache: "no-store",
    credentials: "include",
    headers,
    signal,
  });
  if (!response.ok) throw await parseError(response);
  const blob = await response.blob();
  return { url: URL.createObjectURL(blob), contentType: blob.type };
}

/**
 * A meaningful download filename derived from a media path: the
 * last path segment when it carries a file extension. Recorded
 * media keys are bare hashes with no name to derive, so those
 * fall back to `fallback` (callers that know the real filename —
 * e.g. an outputs entry — pass it as the fallback).
 */
export function filenameFromMediaPath(
  path: string,
  fallback = "cinemora-download",
): string {
  const clean = path.split(/[?#]/)[0];
  const last = clean.split("/").filter(Boolean).pop() || "";
  if (!last || !last.includes(".")) return fallback;
  try {
    return decodeURIComponent(last) || fallback;
  } catch {
    return last || fallback;
  }
}

/**
 * Download a recorded media file (IA-01): fetch it as a blob
 * through the authenticated media path (token as header only),
 * point a temporary anchor at the object URL with a meaningful
 * filename, click it, and revoke the URL once the browser has
 * taken the download over — the same pattern the editor's
 * revision-export download already uses. Throws the ApiClientError
 * from the fetch on failure so callers can say so honestly.
 */
export async function downloadAuthenticatedMedia(options: {
  projectId?: string;
  mediaKey?: string;
  path?: string;
  filename?: string;
}): Promise<void> {
  // Same-origin mode: hand the browser the streamed URL and let
  // it download natively (progress, resume, no whole-file blob
  // held in memory first). The proxy injects the token and the
  // media endpoint's Range support does the rest.
  const streamUrl =
    options.projectId && options.mediaKey
      ? streamMediaUrl(options.projectId, options.mediaKey)
      : options.path
        ? streamMediaPathUrl(options.path)
        : null;
  const filename =
    options.filename ||
    (options.path
      ? filenameFromMediaPath(options.path)
      : "cinemora-download");
  if (streamUrl) {
    const anchor = document.createElement("a");
    anchor.href = streamUrl;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    return;
  }
  let url: string;
  if (options.projectId && options.mediaKey) {
    url = (await fetchMediaObjectUrl(options.projectId, options.mediaKey)).url;
  } else if (options.path) {
    url = (await fetchAuthenticatedMediaObjectUrl(options.path)).url;
  } else {
    throw new Error("A media key or media path is required to download.");
  }
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

// ---------------------------------------------------------------------
// Authenticated connection status (Brief Stage 5, C-06)
// ---------------------------------------------------------------------

/**
 * The honest connection states the studio can be in. They are
 * distinguished because each needs a different action:
 * - "connected": an authenticated v1 call succeeded.
 * - "signed-out": the API answered 401 — there is no valid
 *   session (the visitor is signed out, or the session expired
 *   or was revoked). This is a NEUTRAL state, not an error:
 *   signing in is the fix (Phase 6, A-02; replaces the old
 *   "auth" / "Session rejected" classification, retained in the
 *   union only for state-shape compatibility).
 * - "proxy-not-configured": the same-origin proxy answered 503
 *   api_proxy_not_configured — CINEMORA_BACKEND_URL is not set
 *   on this deployment, so no request reached the backend.
 * - "frontend-not-configured": retained for state-shape
 *   compatibility; the session model has no build-time credential
 *   to be missing, so checkApiConnection no longer produces it.
 * - "backend-not-configured": the BACKEND itself answered 503
 *   api_not_configured — it has no CINEMORA_API_TOKEN set and
 *   fails closed for credential-less requests. Only this exact
 *   error code may carry that diagnosis (A-02: previously ANY
 *   503 was blamed on the retired token story).
 * - "unavailable": the API answered with another server error.
 * - "network": the API could not be reached at all — including
 *   the proxy's 502 api_proxy_unreachable (backend unreachable).
 */
export type ApiConnectionState =
  | "connected"
  | "auth"
  | "signed-out"
  | "frontend-not-configured"
  | "backend-not-configured"
  | "proxy-not-configured"
  | "unavailable"
  | "network";

export type ApiConnectionStatus = {
  state: ApiConnectionState;
  message: string;
};

/**
 * Probe the connection the way the studio actually uses it: one
 * authenticated v1 call (the project list, under the visitor's
 * session). The unauthenticated /health route is deliberately NOT
 * used here — it can report "ok" while every real call fails
 * 401/503 (audit D-05).
 */
export async function checkApiConnection(
  signal?: AbortSignal,
): Promise<ApiConnectionStatus> {
  try {
    await listProjects(signal);
    return { state: "connected", message: "Authenticated API connection confirmed." };
  } catch (error) {
    if (error instanceof ApiClientError) {
      if (error.status === 401) {
        return {
          state: "signed-out",
          message:
            "You are signed out. Sign in to continue.",
        };
      }
      if (error.code === "api_proxy_unreachable" || error.status === 502) {
        return {
          state: "network",
          message:
            "The studio API could not be reached: the backend is unreachable. Check that it is running, then try again.",
        };
      }
      if (error.status === 503 && error.code === "api_proxy_not_configured") {
        return {
          state: "proxy-not-configured",
          message:
            "The API proxy is not configured on this deployment (CINEMORA_BACKEND_URL is not set), so requests never reach the backend.",
        };
      }
      if (error.status === 503 && error.code === "api_not_configured") {
        return {
          state: "backend-not-configured",
          message:
            "The studio API is running but not configured: CINEMORA_API_TOKEN is not set on the server, so it refuses credential-less calls.",
        };
      }
      return {
        state: "unavailable",
        message: `The studio API answered with an error (${error.status}). ${error.message}`,
      };
    }
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    return {
      state: "network",
      message: "The studio API could not be reached. Check that it is running, then try again.",
    };
  }
}

// ---------------------------------------------------------------------
// Stage 6 editor surface used by Brief Stage 5 (uploads + revision
// certificate). The editor UI itself is C-03; these helpers are its
// contract, typed now so C-04 and C-03 share them.
// ---------------------------------------------------------------------

export type UploadRecord = {
  upload_id: string;
  /** Server-side stored path. The plan-decision replace contract
   * consumes exactly this value as replacement.file_path (see
   * API_CONTRACT.md, C-04); it is passed through programmatically
   * and never typed by a user. */
  file_path: string;
  file_name: string;
  type: string;
  source: string;
  provider?: string | null;
  source_url?: string | null;
  title?: string | null;
  source_duration?: number | null;
  size_bytes?: number | null;
  decision_id?: string | null;
  evidence_class?: string | null;
  verification?: Record<string, unknown> | null;
  rights?: {
    license: string;
    rights_status?: string | null;
    creator?: string | null;
    source?: string | null;
  } | null;
  user_supplied?: boolean;
  generated?: boolean;
};

/**
 * Upload one file to a project (POST /projects/{id}/uploads,
 * multipart). Uses XMLHttpRequest because fetch exposes no upload
 * progress; the token travels as the X-API-Key header only.
 * The backend probes the file: an unprobeable upload fails 422
 * and is not stored.
 */
export function uploadAsset(
  projectId: string,
  file: File,
  onProgress?: (fraction: number) => void,
): Promise<UploadRecord> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", apiUrl(`/api/v1/projects/${projectId}/uploads`));
    xhr.withCredentials = true;
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(event.loaded / event.total);
      }
    };
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        // Non-JSON body: fall through to the status-based error.
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as UploadRecord);
        return;
      }
      const errorBody = body as {
        error?: { code?: string; message?: string };
        detail?: string;
      } | null;
      reject(
        new ApiClientError(
          errorBody?.error?.message ||
            (typeof errorBody?.detail === "string" ? errorBody.detail : "") ||
            `Upload failed (${xhr.status}).`,
          xhr.status,
          errorBody?.error?.code,
        ),
      );
    };
    xhr.onerror = () => reject(new TypeError("The upload could not reach the studio API."));
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}

export type RevisionCertificate = Certificate & {
  certificate_version: 2;
  revision: {
    revision_id: string;
    revision_number?: number | null;
    parent_revision_id?: string | null;
    chain_revision_ids: string[];
    status?: string | null;
    source_run_id?: string | null;
  };
  final_revision_assets: Array<{
    item_id: string;
    kind: string;
    classification?: string | null;
    sentence_number?: number | null;
    scene_number?: number | null;
    file_name?: string | null;
    provider?: string | null;
    source_url?: string | null;
    evidence_class?: string | null;
    verification?: { accepted?: boolean; reason?: string | null; transcript_check?: string | null } | null;
    rights?: { license: string; rights_status?: string | null; creator?: string | null; source?: string | null } | null;
    timeline: { start?: number | null; duration?: number | null };
  }>;
};

export function getRevisionCertificate(
  projectId: string,
  revisionId: string,
  signal?: AbortSignal,
) {
  return apiRequest<RevisionCertificate>(
    `/api/v1/projects/${projectId}/revisions/${revisionId}/certificate`,
    { signal },
  );
}

// ---------------------------------------------------------------------
// Stage 7 editor (C-03) — the revision editor surface.
//
// Types mirror services/editor.py's revision document exactly as
// the API returns it (see API_CONTRACT.md, "Editor (Stage 6)").
// The document is the server's truth: the UI renders what these
// endpoints return and re-reads the revision after every applied
// batch — it keeps no local copy of timeline state.
// ---------------------------------------------------------------------

export type RevisionView = {
  revision_id: string;
  project_id: string;
  run_id: string;
  parent_revision_id: string | null;
  revision_number: number;
  status: string;
  document_path: string;
  document_hash: string;
  created_at: string;
  updated_at: string;
};

export type EditorRights = {
  license: string;
  license_url?: string | null;
  rights_status?: string | null;
  creator?: string | null;
  source?: string | null;
  retrieved_at?: string | null;
} | null;

export type EditorVerification = {
  accepted?: boolean | null;
  confidence?: number | null;
  specificity?: string | null;
  reason?: string | null;
  rejection_reason?: string | null;
  [key: string]: unknown;
} | null;

export type EditorAsset = {
  file_path: string | null;
  type: string;
  source?: string | null;
  provider?: string | null;
  source_url?: string | null;
  title?: string | null;
  clip_start: number;
  source_duration?: number | null;
  decision_id?: string | null;
  evidence_class?: string | null;
  verification?: EditorVerification;
  rights?: EditorRights;
  user_supplied?: boolean;
  generated?: boolean;
  swapped_from?: { file_path?: string | null; decision_id?: string | null } | null;
} | null;

/**
 * The complete regeneration recipe the backend records for a
 * graphic (MG-03) and serves inside the editor document's
 * graphic payload (services/editor.py). Fields are optional
 * because graphics produced before recipes were recorded carry
 * none — regeneration then honestly refuses.
 */
export type GraphicRecipe = {
  family?: string | null;
  renderer?: string | null;
  template?: string | null;
  template_version?: string | null;
  inputs?: Record<string, unknown> | null;
  style?: string | null;
  duration?: number | null;
  purpose?: string | null;
  scene_number?: number | null;
  sentence_number?: number | null;
  source_references?: unknown[] | null;
  regenerable?: boolean | null;
  non_regenerable_reason?: string | null;
};

export type EditorGraphic = {
  template?: string | null;
  family?: string | null;
  style?: string | null;
  inputs?: Record<string, unknown> | null;
  engine_metadata?: Record<string, unknown> | null;
  recipe?: GraphicRecipe | null;
} | null;

export type VisualItem = {
  item_id: string;
  kind: "clip" | "graphic" | "gap";
  sentence_number: number;
  scene_number: number;
  start: number;
  end: number;
  duration: number;
  assignment?: string | null;
  transition?: string | null;
  asset: EditorAsset;
  graphic: EditorGraphic;
  classification: string;
  flags: string[];
  split_group?: string;
  removed_item_id?: string;
};

export type NarrationSentence = {
  sentence_number: number;
  scene_number: number;
  text: string;
  start: number;
  end: number;
};

export type CaptionSegment = {
  start: number;
  end: number;
  text: string;
  words?: Array<{ start: number; end: number; word?: string; text?: string }> | null;
  edited?: boolean;
};

export type EditorOpLogEntry = {
  op_id: string;
  type: string;
  actor: string;
  at: string;
  summary: string;
  params?: Record<string, unknown>;
};

export type RevisionExport = {
  export_id: string;
  file: string;
  file_name: string;
  sha256: string;
  at: string;
  run_id: string;
  allow_gaps: boolean;
  gaps_exported: Array<{ item_id: string; sentence_number: number; duration: number }>;
  holdovers: Array<{
    gap_item_id: string;
    sentence_number: number;
    duration: number;
    held_file?: string | null;
  }>;
  audit: {
    evaluated: boolean;
    passed: boolean;
    failures: string[];
    warnings: string[];
    duration_seconds?: number | null;
    exported_with_gaps: number[];
  };
  render_plan: RenderPlan;
};

export type RenderPlan = {
  visual: {
    changed_item_ids: string[];
    reused_item_ids: string[];
    new_item_ids: string[];
    removed_item_ids: string[];
    gap_item_ids: string[];
    reuse_level: string;
  };
  narration_rebuild: boolean;
  narration_dirty_sentences: number[];
  voice_changed: boolean;
  captions_rebuild: boolean;
  soundtrack_rebuild: boolean;
  assembly: boolean;
  retiming: string;
  rules: string[];
};

export type EditorDocument = {
  version: number;
  revision_id: string;
  project_id: string;
  run_id: string;
  parent_revision_id: string | null;
  revision_number: number;
  status: string;
  created_at: string;
  created_from: string;
  duration_seconds: number;
  tracks: {
    visual: VisualItem[];
    narration: {
      file: string;
      voice_id?: string | null;
      sentences: NarrationSentence[];
      dirty_sentences: number[];
    };
    music: {
      file: string | null;
      source?: string | null;
      mixed?: string | null;
      state?: string | null;
      license?: string | null;
    };
    captions: {
      file: string;
      mode: string;
      timing_source?: string | null;
      provisional: boolean;
      segments: CaptionSegment[];
      stale: boolean;
      stale_reason?: string | null;
    };
  };
  volumes: { narration: number; music: number };
  op_log: EditorOpLogEntry[];
  exports: RevisionExport[];
  render_state: {
    run_id: string;
    allow_gaps: boolean;
    plan: RenderPlan;
    requested_at: string;
    completed_at?: string;
  } | null;
  last_render_error: string | null;
};

export type RevisionWithDocument = RevisionView & { document: EditorDocument };

/** An asset spec for add_clip / replace_clip, exactly as
 * services/editor.py's `_resolve_asset_spec` accepts it. The UI
 * never offers `file_path` (a server path is not a user concept —
 * C-04); uploads, ledger decisions and URLs are the user-facing
 * sources. */
export type AssetSpec =
  | { upload_id: string }
  | { library_decision_id: string }
  | { source_url: string };

/** The 14 editor operations, with the backend's exact payload
 * fields (services/editor.py `_OP_HANDLERS`). */
export type EditorOperation =
  | { type: "remove_clip"; item_id: string; gap_policy: "leave_gap" | "close_gap" }
  | { type: "replace_clip"; item_id: string; replacement: AssetSpec }
  | {
      type: "add_clip";
      asset: AssetSpec;
      sentence_number: number;
      after_item_id?: string;
      duration?: number;
    }
  | { type: "trim_clip"; item_id: string; clip_start?: number; duration?: number }
  | { type: "move_clip"; item_id: string; to_sentence_number?: number; after_item_id?: string }
  | { type: "split_clip"; item_id: string; at_offset: number }
  | { type: "set_graphic_duration"; item_id: string; duration: number }
  | { type: "regenerate_graphic"; item_id: string }
  | { type: "edit_caption"; segment_index: number; text?: string; start?: number; end?: number }
  | {
      type: "regenerate_narration_sentence";
      sentence_number: number;
      text?: string;
      voice_id?: string;
    }
  | { type: "set_music"; music_file?: string | null; upload_id?: string }
  | { type: "set_volumes"; narration?: number; music?: number }
  | {
      type: "set_transition";
      item_id: string;
      transition: string;
      reason?: string;
      window_frames?: number;
    }
  | {
      type: "assign_graphic";
      item_id: string;
      template?: string;
      inputs?: Record<string, unknown>;
      style?: string;
    };

export type RevisionRenderResult = {
  revision_id: string;
  run_id: string;
  status: string;
  allow_gaps: boolean;
  render_plan: RenderPlan;
  deduplicated?: boolean;
};

/** Editor document for the latest revision; creates revision 0
 * from the latest completed run when none exists (404
 * no_completed_run when the project has none). */
export function getEditor(projectId: string, signal?: AbortSignal) {
  return apiRequest<RevisionWithDocument>(`/api/v1/projects/${projectId}/editor`, { signal });
}

export function listRevisions(projectId: string, signal?: AbortSignal) {
  return apiRequest<{ revisions: RevisionView[] }>(
    `/api/v1/projects/${projectId}/revisions`,
    { signal },
  );
}

export function getRevision(projectId: string, revisionId: string, signal?: AbortSignal) {
  return apiRequest<RevisionWithDocument>(
    `/api/v1/projects/${projectId}/revisions/${revisionId}`,
    { signal },
  );
}

/**
 * The exports recorded for one revision, from the dedicated
 * route (GET /projects/{id}/revisions/{rev}/exports — the
 * backend has served it since Stage 7; the document's embedded
 * copy can lag a just-completed render). Redesign F2 / UI-001:
 * this helper existed once before and was removed as dead
 * (IA-10) because nothing called it; it returns WITH its caller
 * — the revision panel's export list — which is the fix.
 */
export function listRevisionExports(projectId: string, revisionId: string, signal?: AbortSignal) {
  return apiRequest<{ exports: RevisionExport[] }>(
    `/api/v1/projects/${projectId}/revisions/${revisionId}/exports`,
    { signal },
  );
}

/** Branch a new draft from a revision (deep copy, next number,
 * exports reset), or import a run as a revision 0. */
export function createRevision(
  projectId: string,
  body: { from_revision_id?: string; from_run_id?: string },
) {
  return apiRequest<RevisionView>(`/api/v1/projects/${projectId}/revisions`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/**
 * Apply one atomic operation batch to a revision. On success the
 * batch becomes the NEXT revision and its view is returned (the
 * document itself is re-read with getRevision). Any invalid
 * operation rejects the whole batch with 422 and persists
 * nothing.
 */
export function applyEditorOperations(
  projectId: string,
  revisionId: string,
  operations: EditorOperation[],
) {
  return apiRequest<RevisionView>(
    `/api/v1/projects/${projectId}/revisions/${revisionId}/operations`,
    { method: "POST", body: JSON.stringify({ operations }) },
  );
}

/** Request a revision render (202). Unresolved gaps block the
 * render with 422 unresolved_gaps unless allowGaps is set, in
 * which case each gap is covered by a recorded holdover. */
export function requestRevisionRender(projectId: string, revisionId: string, allowGaps: boolean) {
  return apiRequest<RevisionRenderResult>(
    `/api/v1/projects/${projectId}/revisions/${revisionId}/render`,
    { method: "POST", body: JSON.stringify({ allow_gaps: allowGaps }) },
  );
}

// ---------------------------------------------------------------------
// Editor Studio (founder priority #5) — the multitrack projection
// of a revision (services/editor_studio.py). Reads only: edits go
// through applyEditorOperations, renders through
// requestRevisionRender, exactly like the classic editor.
// ---------------------------------------------------------------------

export type StudioItemAsset = {
  file_path: string | null;
  file_name: string | null;
  type: string | null;
  title?: string | null;
  source?: string | null;
  source_url?: string | null;
  clip_start: number;
  source_duration?: number | null;
  decision_id?: string | null;
  media_key: string | null;
};

export type StudioItem = {
  item_id: string;
  kind: "clip" | "graphic" | "gap";
  sentence_number: number;
  scene_number: number;
  start: number;
  end: number;
  duration: number;
  transition: string;
  transition_reason?: string | null;
  transition_window_frames?: number | null;
  assignment?: string | null;
  classification: string;
  flags: string[];
  graphic: {
    template?: string | null;
    family?: string | null;
    style?: string | null;
    inputs?: Record<string, unknown> | null;
    has_recipe: boolean;
  } | null;
  asset: StudioItemAsset;
};

export type StudioTrack =
  | { id: "video"; kind: "video"; name: string; items: StudioItem[] }
  | { id: "graphics"; kind: "graphics"; name: string; items: StudioItem[] }
  | {
      id: "captions";
      kind: "captions";
      name: string;
      segments: Array<{
        segment_index: number;
        start: number;
        end: number;
        text: string;
        edited: boolean;
      }>;
      stale: boolean;
      stale_reason?: string | null;
    }
  | {
      id: "narration";
      kind: "audio";
      name: string;
      volume: number;
      voice_id?: string | null;
      media_key: string | null;
      sentences: Array<{
        sentence_number: number;
        scene_number: number;
        start: number;
        end: number;
        text: string;
        dirty: boolean;
      }>;
    }
  | {
      id: "music";
      kind: "audio";
      name: string;
      volume: number;
      state?: string | null;
      file_name: string | null;
      media_key: string | null;
    };

export type StudioView = RevisionView & {
  duration_seconds: number;
  volumes: { narration: number; music: number };
  render_state: EditorDocument["render_state"];
  last_render_error: string | null;
  exports: RevisionExport[];
  tracks: StudioTrack[];
  /** Phase 6 (A-17): server-persisted locked track ids. */
  locked_tracks?: string[];
  catalogues: {
    transitions: string[];
    transition_reasons: string[];
    graphic_templates: string[];
  };
};

export function getTrackLocks(projectId: string, signal?: AbortSignal) {
  return apiRequest<{ locked_tracks: string[] }>(
    `/api/v1/projects/${projectId}/editor/track-locks`,
    { signal },
  );
}

export function setTrackLock(projectId: string, trackId: string, locked: boolean) {
  return apiRequest<{ locked_tracks: string[] }>(
    `/api/v1/projects/${projectId}/editor/track-locks/${trackId}`,
    { method: "PUT", body: JSON.stringify({ locked }) },
  );
}

export function getStudioView(projectId: string, revisionId: string, signal?: AbortSignal) {
  return apiRequest<StudioView>(
    `/api/v1/projects/${projectId}/revisions/${revisionId}/studio`,
    { signal },
  );
}

export type StudioPoolEntry = {
  pool_id: string;
  origin: "ledger" | "upload";
  decision_id: string | null;
  upload_id: string | null;
  file_path: string;
  file_name: string;
  type: string;
  classification: string;
  provider?: string | null;
  source_url?: string | null;
  scene_number?: number | null;
  sentence_number?: number | null;
  revision_id?: string | null;
  decision_type?: string | null;
  source_duration?: number | null;
  media_key: string | null;
  available: boolean;
  in_timeline: boolean;
};

export type StudioPool = {
  revision_id: string;
  run_id: string;
  total: number;
  limit: number;
  offset: number;
  items: StudioPoolEntry[];
};

export function getStudioPool(
  projectId: string,
  revisionId: string,
  limit = 100,
  offset = 0,
  signal?: AbortSignal,
) {
  return apiRequest<StudioPool>(
    `/api/v1/projects/${projectId}/revisions/${revisionId}/studio/pool?limit=${limit}&offset=${offset}`,
    { signal },
  );
}

export type StudioWaveform = {
  track: string;
  duration_seconds: number;
  bucket_count: number;
  bucket_seconds: number;
  peak: number;
  peaks: number[];
};

export function getStudioWaveform(
  projectId: string,
  revisionId: string,
  track: "narration" | "music",
  signal?: AbortSignal,
) {
  return apiRequest<StudioWaveform>(
    `/api/v1/projects/${projectId}/revisions/${revisionId}/studio/waveform?track=${track}`,
    { signal },
  );
}

/**
 * The Studio viewer's frame at a timeline instant: the REAL frame
 * the current (unrendered) revision document shows there, cut by
 * the backend from the covering item's source media. Fetched as an
 * object URL through the authenticated path (a bare <img> cannot
 * send credentials); callers own revoking the URL. Rejects when
 * the backend cannot produce the frame — the viewer then says so
 * instead of showing a placeholder.
 */
export function fetchStudioFrameObjectUrl(
  projectId: string,
  revisionId: string,
  t: number,
  signal?: AbortSignal,
): Promise<{ url: string; contentType: string }> {
  return fetchAuthenticatedMediaObjectUrl(
    `/api/v1/projects/${projectId}/revisions/${revisionId}/studio/frame?t=${t.toFixed(3)}`,
    signal,
  );
}

/**
 * The media key the backend derives for a project file
 * (services/plan_document.build_media_index: sha256 of the
 * project-relative POSIX path, first 20 hex chars). The editor
 * document carries absolute file paths, not media keys, so the
 * editor computes the key for a path under the project directory
 * and fetches through the authenticated media endpoint. The index
 * is built exclusively from recorded artefacts — timeline items,
 * uploads, revision-document media and revision exports among
 * them (Stage 6/7B, IA-04) — so a file no artefact records
 * answers 404 there; callers must treat that as "no in-app
 * preview" and fall back honestly, never fake one.
 */
export async function mediaKeyForProjectPath(
  projectId: string,
  filePath: string,
): Promise<string | null> {
  const marker = `/${projectId}/`;
  const index = filePath.lastIndexOf(marker);
  if (index < 0) return null;
  const relative = filePath.slice(index + marker.length);
  if (!relative || relative.split("/").includes("..")) return null;
  return sha256Hex20(relative);
}

/**
 * The media key for a file that sits at the project root, where
 * the project-relative path IS its file name (the recorded
 * layout for revision exports — backend test
 * test_media_serves_revision_export_and_upload derives the key
 * exactly this way). UI-001 fallback for export records whose
 * stored absolute path does not carry the /{projectId}/ segment
 * mediaKeyForProjectPath looks for.
 */
export async function mediaKeyForFileName(fileName: string): Promise<string> {
  return sha256Hex20(fileName);
}

async function sha256Hex20(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  const hex = [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  return hex.slice(0, 20);
}

// ---------------------------------------------------------------------
// Administration (Phase 7) — /api/v1/admin/*
//
// Every helper below talks to the admin router, which enforces the
// role ladder on the backend; the /admin pages additionally gate
// rendering on the signed-in user's role (components/admin-shell).
// Figures are the platform's real records — the backend fabricates
// nothing, and monetary cost is null (not instrumented), never 0.
// ---------------------------------------------------------------------

export type AdminOverview = {
  generated_at: string;
  window_days: number;
  projects_total: number;
  users_total: number;
  users_by_status: Record<string, number>;
  runs_total: number;
  runs_by_status: Record<string, number>;
  runs_window_by_status: Record<string, number>;
  completion_rate_window: number | null;
  median_wall_seconds: number | null;
  median_wall_seconds_window: number | null;
  serper_calls_window: number;
  llm_calls_window: number;
  active_runs: number;
  latest_failures: Array<{
    run_id: string;
    project_id: string;
    status: string;
    stage: string | null;
    error: string | null;
    created_at: string | null;
  }>;
};

export type AdminRunListEntry = {
  run_id: string;
  project_id: string;
  project_title: string | null;
  owner_id: string | null;
  status: string;
  stage: string | null;
  error: string | null;
  cancel_requested: boolean;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  wall_seconds: number | null;
  presence: {
    worker_id: string | null;
    heartbeat_at: string | null;
    age_seconds: number | null;
    live: boolean;
  } | null;
};

export type AdminRunDetail = AdminRunListEntry & {
  options: Record<string, unknown> | null;
  script_hash: string | null;
  execution: Record<string, unknown> | null;
  asset_report: Record<string, unknown> | null;
  production_state: {
    current_stage: string | null;
    stages: Record<string, string | null>;
  } | null;
  studio_project_path: string;
};

export type AdminHealth = {
  generated_at: string;
  components: Record<
    string,
    { status: string } & Record<string, unknown>
  >;
};

export type AdminProviderUsage = {
  generated_at: string;
  window_days: number;
  runs_with_execution: number;
  totals: {
    serper_calls: { total: number; web: number; images: number; video: number };
    llm_calls: { total: number; total_tokens: number };
    archive_org_searches: number;
    wikimedia_searches: number;
    candidates_discovered: number;
    downloads: {
      attempted: number;
      succeeded: number;
      failed: number;
      bytes_downloaded: number;
    };
  };
  per_run: Array<Record<string, unknown>>;
  monetary_cost_usd: null;
  monetary_cost_note: string;
};

export type AdminStorage = {
  generated_at: string;
  media_root: string;
  total_bytes: number;
  total_files: number;
  projects: Array<{
    project_id: string;
    title: string | null;
    status: string | null;
    in_database: boolean;
    bytes: number;
    files: number;
    runs: Array<{ run_id: string; bytes: number }>;
  }>;
  projects_missing_media: Array<{
    project_id: string;
    title: string | null;
    status: string | null;
  }>;
  largest_artefacts: Array<{
    path: string;
    project_id: string;
    bytes: number;
  }>;
};

export type AdminOrphans = {
  generated_at: string;
  media_directories_without_project: Array<{
    project_id: string;
    path: string;
    bytes: number;
  }>;
  projects_without_media_directory: string[];
  old_terminal_run_directories: Array<{
    run_id: string;
    project_id: string;
    status: string;
    finished_at: string;
    path: string;
    bytes: number;
  }>;
  old_terminal_threshold_days: number;
};

export type AdminUser = AccountUser & {
  plan: string | null;
  project_count: number;
  run_count: number;
};

export type AdminSubscriptions = {
  plans: Array<{
    id: string;
    name: string;
    max_concurrent_runs: number;
    max_runs_per_period: number | null;
    period: string;
    max_script_chars: number | null;
    max_storage_bytes: number | null;
    max_upload_bytes: number | null;
    priority_tier: number;
    is_default: boolean;
    active_subscribers: number;
  }>;
  subscriptions: Array<{
    id: string;
    user_id: string;
    user_email: string | null;
    plan: string | null;
    status: string;
    current_period_start: string | null;
    current_period_end: string | null;
    billing_provider: string | null;
    created_at: string;
  }>;
  billing_note: string;
};

export type AdminAuditEntry = {
  id: string;
  actor_identity: string;
  actor_role: string;
  auth_method: string;
  action: string;
  target_type: string | null;
  target_id: string | null;
  detail: Record<string, unknown> | null;
  result: string;
  error: string | null;
  created_at: string | null;
};

export type AdminSettings = {
  generated_at: string;
  note: string;
  groups: Record<
    string,
    Array<{
      name: string;
      value?: unknown;
      configured: boolean;
      secret: boolean;
      applies_on_restart: boolean;
      note?: string;
    }>
  >;
};

export function getAdminOverview(signal?: AbortSignal) {
  return apiRequest<AdminOverview>("/api/v1/admin/overview", { signal });
}

export function getAdminHealth(signal?: AbortSignal) {
  return apiRequest<AdminHealth>("/api/v1/admin/health", { signal });
}

export function getAdminWorkers(signal?: AbortSignal) {
  return apiRequest<{
    generated_at: string;
    workers: Record<string, unknown>;
    queues: Record<string, unknown>;
    providers: Record<string, unknown>;
  }>("/api/v1/admin/workers", { signal });
}

export function listAdminRuns(
  params: { status?: string; limit?: number; offset?: number } = {},
  signal?: AbortSignal,
) {
  const query = new URLSearchParams();
  if (params.status) query.set("status", params.status);
  if (params.limit) query.set("limit", String(params.limit));
  if (params.offset) query.set("offset", String(params.offset));
  const suffix = query.toString() ? `?${query}` : "";
  return apiRequest<{ total: number; runs: AdminRunListEntry[] }>(
    `/api/v1/admin/runs${suffix}`,
    { signal },
  );
}

export function getAdminRun(runId: string, signal?: AbortSignal) {
  return apiRequest<AdminRunDetail>(`/api/v1/admin/runs/${runId}`, {
    signal,
  });
}

export function cancelAdminRun(runId: string) {
  return apiRequest<{ run_id: string; cancel_requested: boolean }>(
    `/api/v1/admin/runs/${runId}/cancel`,
    { method: "POST" },
  );
}

export function submitAdminReplacement(runId: string) {
  return apiRequest<{ run: { run_id: string; status: string } | null }>(
    `/api/v1/admin/runs/${runId}/replacement`,
    { method: "POST" },
  );
}

export function getAdminProviderUsage(signal?: AbortSignal) {
  return apiRequest<AdminProviderUsage>("/api/v1/admin/providers/usage", {
    signal,
  });
}

export function getAdminStorage(signal?: AbortSignal) {
  return apiRequest<AdminStorage>("/api/v1/admin/storage", { signal });
}

export function getAdminOrphans(signal?: AbortSignal) {
  return apiRequest<AdminOrphans>("/api/v1/admin/storage/orphans", {
    signal,
  });
}

export function listAdminUsers(q?: string, signal?: AbortSignal) {
  const suffix = q ? `?q=${encodeURIComponent(q)}` : "";
  return apiRequest<{ total: number; users: AdminUser[] }>(
    `/api/v1/admin/users${suffix}`,
    { signal },
  );
}

export function getAdminUser(userId: string, signal?: AbortSignal) {
  return apiRequest<AdminUser & { usage: AccountUsage }>(
    `/api/v1/admin/users/${userId}`,
    { signal },
  );
}

export function setAdminUserStatus(userId: string, status: string) {
  return apiRequest<{ user: AdminUser }>(
    `/api/v1/admin/users/${userId}/status`,
    { method: "POST", body: JSON.stringify({ status }) },
  );
}

export function assignAdminUserPlan(userId: string, planName: string) {
  return apiRequest<{ user: AccountUser; plan: string }>(
    `/api/v1/admin/users/${userId}/plan`,
    { method: "POST", body: JSON.stringify({ plan_name: planName }) },
  );
}

/** Phase 6 (A-16): operator-mediated password reset. The response
 * carries the one-time reset link exactly once — the caller shows
 * it once for out-of-band delivery and never stores it. */
export function generateAdminUserResetLink(userId: string) {
  return apiRequest<{ reset_link: string; expires_at: string | null }>(
    `/api/v1/admin/users/${userId}/reset-link`,
    { method: "POST", body: JSON.stringify({}) },
  );
}

export function getAdminSubscriptions(signal?: AbortSignal) {
  return apiRequest<AdminSubscriptions>("/api/v1/admin/subscriptions", {
    signal,
  });
}

export function listAdminProjects(signal?: AbortSignal) {
  return apiRequest<{
    total: number;
    projects: Array<{
      id: string;
      title: string | null;
      category: string;
      status: string;
      owner_id: string | null;
      owner_email: string | null;
      created_at: string;
      latest_run: { run_id: string; status: string; stage: string | null } | null;
      studio_path: string;
    }>;
  }>("/api/v1/admin/projects", { signal });
}

export function listAdminAuditLog(
  params: { action?: string; actor?: string; target_id?: string } = {},
  signal?: AbortSignal,
) {
  const query = new URLSearchParams();
  if (params.action) query.set("action", params.action);
  if (params.actor) query.set("actor", params.actor);
  if (params.target_id) query.set("target_id", params.target_id);
  const suffix = query.toString() ? `?${query}` : "";
  return apiRequest<{ total: number; entries: AdminAuditEntry[] }>(
    `/api/v1/admin/audit-log${suffix}`,
    { signal },
  );
}

export function getAdminSettings(signal?: AbortSignal) {
  return apiRequest<AdminSettings>("/api/v1/admin/settings", { signal });
}

// ---------------------------------------------------------------------
// AI Product Assistant (Phase 6)
// ---------------------------------------------------------------------

export type AssistantConversation = {
  id: string;
  title: string | null;
  project_id: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type AssistantToolTraceEntry = {
  tool: string;
  label: string;
  status: string;
  injection_patterns_detected?: string[];
};

export type AssistantMessage = {
  id: string;
  conversation_id: string;
  role: "user" | "assistant";
  content: string;
  tool_trace: {
    state?: string;
    tools?: AssistantToolTraceEntry[];
    unavailable_reason?: string | null;
  } | null;
  created_at: string | null;
};

export type AssistantAction = {
  id: string;
  conversation_id: string;
  message_id: string | null;
  tool_name: string;
  action_class: "read" | "write" | "consequential";
  label: string;
  arguments: Record<string, unknown>;
  status: string;
  result: Record<string, unknown> | null;
  created_at: string | null;
  decided_at: string | null;
  executed_at: string | null;
  expires_at: string | null;
};

export type AssistantConversationDetail = {
  conversation: AssistantConversation;
  messages: AssistantMessage[];
  pending_actions: AssistantAction[];
};

export type AssistantTurnResult = {
  state: string;
  message: AssistantMessage;
  pending_action: AssistantAction | null;
  tool_results: Record<string, unknown>;
};

export function listAssistantConversations(signal?: AbortSignal) {
  return apiRequest<{ conversations: AssistantConversation[] }>(
    "/api/v1/assistant/conversations",
    { signal },
  );
}

export function createAssistantConversation(projectId?: string) {
  return apiRequest<AssistantConversation>("/api/v1/assistant/conversations", {
    method: "POST",
    body: JSON.stringify(projectId ? { project_id: projectId } : {}),
  });
}

export function getAssistantConversation(
  conversationId: string,
  signal?: AbortSignal,
) {
  return apiRequest<AssistantConversationDetail>(
    `/api/v1/assistant/conversations/${conversationId}`,
    { signal },
  );
}

export async function deleteAssistantConversation(conversationId: string) {
  await apiRequest(`/api/v1/assistant/conversations/${conversationId}`, {
    method: "DELETE",
  });
}

export function postAssistantMessage(conversationId: string, content: string) {
  return apiRequest<AssistantTurnResult>(
    `/api/v1/assistant/conversations/${conversationId}/messages`,
    { method: "POST", body: JSON.stringify({ content }) },
  );
}

export function confirmAssistantAction(actionId: string) {
  return apiRequest<{ action: AssistantAction }>(
    `/api/v1/assistant/actions/${actionId}/confirm`,
    { method: "POST" },
  );
}

export function rejectAssistantAction(actionId: string) {
  return apiRequest<{ action: AssistantAction }>(
    `/api/v1/assistant/actions/${actionId}/reject`,
    { method: "POST" },
  );
}

// ---------------------------------------------------------------------
// Asset Library (Phase E, founder priority #7): the account-level
// library over the asset ledger. Items are keyed by content hash;
// rights/verification are the ledger's records, carried verbatim.
// ---------------------------------------------------------------------

export type LibraryProjectRef = { id: string; title: string | null };

export type LibraryAsset = {
  id: string;
  content_hash: string;
  type: string;
  title: string;
  title_recorded: boolean;
  file_name: string | null;
  size_bytes: number | null;
  available: boolean;
  media_url: string | null;
  providers: string[];
  rights: Record<string, unknown> | null;
  licence_state: "recorded" | "unknown";
  verification_states: string[];
  decision_types: string[];
  usage_count: number;
  projects: LibraryProjectRef[];
  copies: number;
  duplicate: boolean;
  tags: string[];
  folder_id: string | null;
  first_seen: string | null;
  last_seen: string | null;
};

export type LibraryUsage = {
  decision_id: string;
  project_id: string;
  project: LibraryProjectRef;
  run_id: string;
  revision_id: string | null;
  scene_number: number | null;
  sentence_number: number | null;
  decision_type: string;
  evidence_class: string | null;
  verification: Record<string, unknown> | null;
  verification_state: string;
  rights: Record<string, unknown> | null;
  provider: string | null;
  source_url: string | null;
  previous_decision_id: string | null;
  created_at: string | null;
  file_path: string | null;
  file_present: boolean;
};

export type LibraryUploadSighting = {
  project_id: string;
  project: LibraryProjectRef;
  upload_id: string | null;
  title: string | null;
  file_name: string | null;
  file_path: string | null;
  type: string | null;
  provider: string | null;
  size_bytes: number | null;
  decision_id: string | null;
  rights: Record<string, unknown> | null;
  library_provenance: Record<string, unknown> | null;
};

export type LibraryAssetDetail = LibraryAsset & {
  usages: LibraryUsage[];
  upload_sightings: LibraryUploadSighting[];
  copies_detail: {
    file_path: string;
    project: LibraryProjectRef;
    size_bytes: number | null;
  }[];
  duplicate_reclaimable_bytes: number;
  home_project: LibraryProjectRef | null;
  home_media_key: string | null;
};

export type LibraryList = {
  total: number;
  limit: number;
  offset: number;
  items: LibraryAsset[];
  summary: {
    library_items: number;
    library_usages: number;
    projects_covered: number;
    unkeyed_records: { decisions: number; uploads: number };
  };
  folders: { id: string; name: string }[];
};

export type LibraryFolder = {
  id: string;
  name: string;
  created_at?: string;
  item_count: number;
};

export type LibraryDuplicates = {
  duplicates: {
    content_hash: string;
    title: string;
    type: string;
    size_bytes: number | null;
    copies: {
      file_path: string;
      project: LibraryProjectRef;
      size_bytes: number | null;
    }[];
    extra_copies: number;
    reclaimable_bytes: number;
  }[];
  total_items: number;
  total_reclaimable_bytes: number;
};

export type LibraryStorage = {
  media_root: string;
  total_bytes: number;
  total_files: number;
  projects: {
    project_id: string;
    title: string | null;
    status: string;
    bytes: number;
    files: number;
    in_media_tree: boolean;
  }[];
  library_by_type: Record<string, { items: number; bytes: number }>;
  duplicates: { items: number; reclaimable_bytes: number };
};

export type LibraryImportResult = {
  import: Record<string, unknown> & {
    upload_id: string;
    file_name: string;
    decision_id: string | null;
    library_provenance?: Record<string, unknown>;
  };
  content_hash: string;
  target_project_id: string;
  revision_id: string | null;
  pre_run: boolean;
};

export function listLibraryAssets(
  params: {
    q?: string;
    project_id?: string;
    type?: string;
    provider?: string;
    licence?: string;
    verification?: string;
    decision_type?: string;
    folder?: string;
    tag?: string;
    duplicates_only?: boolean;
    limit?: number;
    offset?: number;
  } = {},
  signal?: AbortSignal,
) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "" || value === false) continue;
    query.set(key, String(value));
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiRequest<LibraryList>(`/api/v1/library/assets${suffix}`, { signal });
}

export function getLibraryAsset(contentHash: string, signal?: AbortSignal) {
  return apiRequest<LibraryAssetDetail>(`/api/v1/library/assets/${contentHash}`, {
    signal,
  });
}

export function setLibraryAssetOrganisation(
  contentHash: string,
  body: { tags?: string[]; folder_id?: string | null; folder_set?: boolean },
) {
  return apiRequest<LibraryAsset>(
    `/api/v1/library/assets/${contentHash}/organisation`,
    { method: "PUT", body: JSON.stringify(body) },
  );
}

export function importLibraryAsset(contentHash: string, projectId: string) {
  return apiRequest<LibraryImportResult>(
    `/api/v1/library/assets/${contentHash}/imports`,
    { method: "POST", body: JSON.stringify({ project_id: projectId }) },
  );
}

export function listLibraryDuplicates(signal?: AbortSignal) {
  return apiRequest<LibraryDuplicates>(`/api/v1/library/duplicates`, { signal });
}

export function getLibraryStorage(signal?: AbortSignal) {
  return apiRequest<LibraryStorage>(`/api/v1/library/storage`, { signal });
}

export function listLibraryFolders(signal?: AbortSignal) {
  return apiRequest<{ folders: LibraryFolder[] }>(`/api/v1/library/folders`, {
    signal,
  });
}

export function createLibraryFolder(name: string) {
  return apiRequest<LibraryFolder>(`/api/v1/library/folders`, {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

export async function deleteLibraryFolder(folderId: string) {
  await apiRequest(`/api/v1/library/folders/${folderId}`, { method: "DELETE" });
}

export function uploadToLibrary(projectId: string, file: File) {
  const form = new FormData();
  form.set("project_id", projectId);
  form.set("file", file);
  return apiRequest<{ upload: Record<string, unknown>; content_hash: string | null }>(
    `/api/v1/library/uploads`,
    { method: "POST", body: form },
  );
}

// ---------------------------------------------------------------------
// Versioning (Phase G, founder priority #9) — the unified version
// story over services/versioning.py. Every type mirrors the
// recorded shapes the service returns; a script text or a diff
// exists here only where the backend recorded one.
// ---------------------------------------------------------------------

export type VersionLabel = {
  name?: string | null;
  note?: string | null;
  updated_at?: string | null;
  updated_by?: string | null;
};

export type TextDiff = {
  state: string;
  reason?: string;
  added_lines?: number | null;
  removed_lines?: number | null;
  diff?: string | null;
  diff_truncated?: boolean;
  diff_line_count?: number | null;
};

export type VersionOpSummary = {
  op_id?: string | null;
  type?: string | null;
  actor?: string | null;
  at?: string | null;
  summary?: string | null;
};

export type RunVerdicts = {
  state: string;
  integrity?: string | null;
  quality?: string | null;
  reliability?: string | null;
  delivery_state?: string | null;
  delivery_status?: string | null;
};

export type VersionEntryDetail = {
  // run entries
  run_id?: string | null;
  status?: string | null;
  stage?: string | null;
  script_hash?: string | null;
  error?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  options?: Record<string, unknown> | null;
  execution?: {
    wall_time_seconds?: number | null;
    stage_durations_seconds?: Record<string, number> | null;
  };
  verdicts?: RunVerdicts;
  // revision entries
  revision_id?: string | null;
  revision_number?: number | null;
  parent_revision_id?: string | null;
  document_hash?: string | null;
  created_from?: string | null;
  duration_seconds?: number | null;
  visual_items?: number | null;
  export_count?: number | null;
  // script entries
  is_current?: boolean;
  run_ids?: string[];
  run_count?: number;
  first_submitted_at?: string | null;
  last_submitted_at?: string | null;
  text_state?: string;
  text_source?: string | null;
  char_count?: number | null;
  // plan decision entries
  decision?: {
    sentence_number?: number | null;
    action?: string | null;
    outcome?: string | null;
    recorded_at?: string | null;
  };
  // current plan entry
  plan_source?: string | null;
  analysis_mode?: string | null;
  generated_at?: string | null;
  scene_count?: number | null;
  sentence_count?: number | null;
  bible_title?: string | null;
  bible_story_type?: string | null;
  // snapshot entries (the snapshot summary shape)
  id?: string;
  name?: string | null;
  note?: string | null;
  created_at?: string | null;
  created_by?: string | null;
  script?: { script_hash?: string | null };
  plan?: {
    state?: string | null;
    sha256?: string | null;
    generated_at?: string | null;
    decisions_recorded?: number | null;
  };
  revision?: {
    state?: string | null;
    revision_id?: string | null;
    revision_number?: number | null;
    document_hash?: string | null;
    run_id?: string | null;
    status?: string | null;
  };
};

export type VersionEntryChanges = {
  vs_entry_id?: string | null;
  script_changed?: boolean | null;
  options_changed?: string[] | null;
  operations_applied?: VersionOpSummary[];
  text_diff?: TextDiff;
};

export type VersionEntry = {
  entry_id: string;
  kind: string;
  at?: string | null;
  title: string;
  label?: VersionLabel | null;
  detail: VersionEntryDetail;
  changes?: VersionEntryChanges | null;
};

export type ScriptVersion = {
  script_hash: string;
  is_current: boolean;
  run_ids: string[];
  run_count: number;
  first_submitted_at?: string | null;
  last_submitted_at?: string | null;
  first_seen_at?: string | null;
  text_state: string;
  text_source?: string | null;
  char_count?: number | null;
};

export type ScriptVersionDetail = ScriptVersion & {
  project_id: string;
  text?: string | null;
};

export type SnapshotSummary = {
  id: string;
  name?: string | null;
  note?: string | null;
  created_at?: string | null;
  created_by?: string | null;
  script: { script_hash?: string | null };
  plan: {
    state?: string | null;
    sha256?: string | null;
    generated_at?: string | null;
    decisions_recorded?: number | null;
  };
  revision: {
    state?: string | null;
    revision_id?: string | null;
    revision_number?: number | null;
    document_hash?: string | null;
    run_id?: string | null;
    status?: string | null;
  };
};

export type RecordedPlanDocument = {
  generated_at?: string | null;
  analysis_mode?: string | null;
  bible?: { title?: string | null; story_type?: string | null } | null;
  scenes?: Array<Record<string, unknown>>;
  sentences?: Array<{
    sentence_number?: number | null;
    text?: string | null;
  }>;
  [key: string]: unknown;
};

export type SnapshotDetail = SnapshotSummary & {
  project_id: string;
  plan_document?: RecordedPlanDocument | null;
};

export type RestoreOutcome = {
  snapshot_id: string;
  snapshot_name?: string | null;
  restored_at: string;
  restored_by?: string | null;
  script: { state: string; script_hash?: string | null; reason?: string };
  plan: { state: string; sha256?: string | null };
  revision: {
    state: string;
    from_revision_id?: string;
    new_revision_id?: string;
    new_revision_number?: number;
  };
  not_restored: string[];
};

export type RestoreRecord = {
  kind?: string | null;
  snapshot_id?: string | null;
  script_hash?: string | null;
  new_revision_id?: string | null;
  at?: string | null;
  by?: string | null;
};

export type VersionsView = {
  schema: string;
  project_id: string;
  current_script_hash?: string | null;
  entries: VersionEntry[];
  script_versions: ScriptVersion[];
  snapshots: SnapshotSummary[];
  restores: RestoreRecord[];
};

export type VersionPlanDiff = {
  state: string;
  reason?: string;
  scene_count?: { a: number; b: number } | null;
  sentence_count?: { a: number; b: number } | null;
  bible?: {
    title: { a?: string | null; b?: string | null };
    story_type: { a?: string | null; b?: string | null };
  };
  sentence_text_changes?:
    | Array<{
        sentence_number: number;
        a_text?: string | null;
        b_text?: string | null;
      }>
    | null;
  sentences_only_in_a?: number[] | null;
  sentences_only_in_b?: number[] | null;
};

export type VersionDiff = {
  kind: string;
  state?: string;
  reason?: string;
  // script_text
  added_lines?: number | null;
  removed_lines?: number | null;
  diff?: string | null;
  diff_truncated?: boolean;
  diff_line_count?: number | null;
  // revision_ops
  common_ancestor_revision_id?: string | null;
  document_hash?: {
    a?: string | null;
    b?: string | null;
    equal?: boolean;
  };
  ops_only_in_a?: VersionOpSummary[];
  ops_only_in_b?: VersionOpSummary[];
  // run_fields
  fields?: Record<string, { a?: unknown; b?: unknown }>;
  options_changed?: string[] | null;
  options?: {
    a?: Record<string, unknown> | null;
    b?: Record<string, unknown> | null;
  };
  execution?: {
    a?: VersionEntryDetail["execution"];
    b?: VersionEntryDetail["execution"];
  };
  verdicts?: { a?: RunVerdicts; b?: RunVerdicts };
  // snapshot_components
  script?: {
    a_hash?: string | null;
    b_hash?: string | null;
    hash_equal?: boolean;
    text_diff?: TextDiff;
  };
  plan?: VersionPlanDiff;
  revision?: {
    a?: SnapshotSummary["revision"];
    b?: SnapshotSummary["revision"];
  };
  // plan_document
  scene_count?: { a: number; b: number } | null;
  sentence_count?: { a: number; b: number } | null;
  bible?: VersionPlanDiff["bible"];
  sentence_text_changes?: VersionPlanDiff["sentence_text_changes"];
  sentences_only_in_a?: number[] | null;
  sentences_only_in_b?: number[] | null;
  // side_by_side
  script_hash?: { a: string; b: string; equal: boolean };
};

export type VersionCompare = {
  schema: string;
  project_id: string;
  a: { entry: VersionEntry };
  b: { entry: VersionEntry };
  diff: VersionDiff;
};

export function getVersions(projectId: string, signal?: AbortSignal) {
  return apiRequest<VersionsView>(`/api/v1/projects/${projectId}/versions`, {
    signal,
  });
}

export function getScriptVersion(
  projectId: string,
  scriptHash: string,
  signal?: AbortSignal,
) {
  return apiRequest<ScriptVersionDetail>(
    `/api/v1/projects/${projectId}/versions/scripts/${scriptHash}`,
    { signal },
  );
}

export function restoreScriptVersion(projectId: string, scriptHash: string) {
  return apiRequest<{
    script_hash: string;
    restored_at: string;
    char_count: number;
    text_source?: string | null;
  }>(`/api/v1/projects/${projectId}/versions/scripts/${scriptHash}/restore`, {
    method: "POST",
  });
}

export function compareVersions(projectId: string, a: string, b: string) {
  const query = `a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`;
  return apiRequest<VersionCompare>(
    `/api/v1/projects/${projectId}/versions/compare?${query}`,
  );
}

export function createSnapshot(
  projectId: string,
  name: string,
  note?: string | null,
) {
  return apiRequest<SnapshotSummary>(
    `/api/v1/projects/${projectId}/versions/snapshots`,
    { method: "POST", body: JSON.stringify({ name, note: note ?? null }) },
  );
}

export function getSnapshot(projectId: string, snapshotId: string) {
  return apiRequest<SnapshotDetail>(
    `/api/v1/projects/${projectId}/versions/snapshots/${snapshotId}`,
  );
}

export async function deleteSnapshot(projectId: string, snapshotId: string) {
  await apiRequest(
    `/api/v1/projects/${projectId}/versions/snapshots/${snapshotId}`,
    { method: "DELETE" },
  );
}

export function restoreSnapshot(projectId: string, snapshotId: string) {
  return apiRequest<RestoreOutcome>(
    `/api/v1/projects/${projectId}/versions/snapshots/${snapshotId}/restore`,
    { method: "POST" },
  );
}

export function setVersionLabel(
  projectId: string,
  entryId: string,
  name: string | null,
  note: string | null,
) {
  return apiRequest<{ entry_id: string; label: VersionLabel | null }>(
    `/api/v1/projects/${projectId}/versions/labels`,
    { method: "PUT", body: JSON.stringify({ entry_id: entryId, name, note }) },
  );
}

// ---------------------------------------------------------------------
// Collaboration (founder priority #11)
// ---------------------------------------------------------------------
// Membership, in-app invitations, comments and the activity feed.
// Invitations are in-app only: no email is sent (none is configured),
// so an invitation waits in the invitee's account until they sign
// in and respond here.

export type MemberUser = {
  user_id: string;
  email?: string | null;
  display_name?: string | null;
};

export type ProjectMemberEntry = {
  user: MemberUser | null;
  role: string;
  status: string;
  invited_by?: MemberUser | null;
  invited_at?: string | null;
  responded_at?: string | null;
  removed_at?: string | null;
  removed_by?: MemberUser | null;
};

export type ProjectMembers = {
  owner: MemberUser | null;
  members: ProjectMemberEntry[];
  invitations: ProjectMemberEntry[];
  your_role?: string | null;
};

export type PendingInvitation = {
  project_id: string;
  project_title?: string | null;
  project_category?: string | null;
  role: string;
  invited_by?: MemberUser | null;
  invited_at?: string | null;
};

export type ProjectComment = {
  comment_id: string;
  project_id: string;
  revision_id?: string | null;
  author: MemberUser | null;
  body: string;
  created_at?: string | null;
  edited_at?: string | null;
};

export type ActivityEvent = {
  at?: string | null;
  kind: string;
  summary: string;
  [key: string]: unknown;
};

export function getProjectMembers(projectId: string) {
  return apiRequest<ProjectMembers>(`/api/v1/projects/${projectId}/members`);
}

export function inviteProjectMember(
  projectId: string,
  email: string,
  role: string,
) {
  return apiRequest<ProjectMemberEntry>(
    `/api/v1/projects/${projectId}/members`,
    { method: "POST", body: JSON.stringify({ email, role }) },
  );
}

export function changeProjectMemberRole(
  projectId: string,
  userId: string,
  role: string,
) {
  return apiRequest<ProjectMemberEntry>(
    `/api/v1/projects/${projectId}/members/${userId}`,
    { method: "PATCH", body: JSON.stringify({ role }) },
  );
}

export async function removeProjectMember(projectId: string, userId: string) {
  await apiRequest(`/api/v1/projects/${projectId}/members/${userId}`, {
    method: "DELETE",
  });
}

/** Item 4: the workspace-level Team Space aggregation. */
export type TeamProject = {
  project_id: string;
  project_title?: string | null;
  project_category?: string | null;
  your_role?: string | null;
  owner: MemberUser | null;
  members: ProjectMemberEntry[];
  invitations: ProjectMemberEntry[];
};

export type TeamPerson = {
  user: MemberUser;
  projects: {
    project_id: string;
    project_title?: string | null;
    role: string;
  }[];
};

export type TeamOverview = {
  projects: TeamProject[];
  people: TeamPerson[];
  invitations_in: PendingInvitation[];
};

export function getTeamOverview(signal?: AbortSignal) {
  return apiRequest<TeamOverview>("/api/v1/team/overview", { signal });
}

export function listInvitations() {
  return apiRequest<{ invitations: PendingInvitation[] }>("/api/v1/invitations");
}

export function respondToInvitation(projectId: string, accept: boolean) {
  return apiRequest<ProjectMemberEntry>(
    `/api/v1/invitations/${projectId}/respond`,
    { method: "POST", body: JSON.stringify({ accept }) },
  );
}

export function listProjectComments(projectId: string) {
  return apiRequest<{ comments: ProjectComment[] }>(
    `/api/v1/projects/${projectId}/comments`,
  );
}

export function createProjectComment(
  projectId: string,
  body: string,
  revisionId?: string | null,
) {
  return apiRequest<ProjectComment>(`/api/v1/projects/${projectId}/comments`, {
    method: "POST",
    body: JSON.stringify({ body, revision_id: revisionId ?? null }),
  });
}

export function editProjectComment(
  projectId: string,
  commentId: string,
  body: string,
) {
  return apiRequest<ProjectComment>(
    `/api/v1/projects/${projectId}/comments/${commentId}`,
    { method: "PUT", body: JSON.stringify({ body }) },
  );
}

export async function deleteProjectComment(
  projectId: string,
  commentId: string,
) {
  await apiRequest(`/api/v1/projects/${projectId}/comments/${commentId}`, {
    method: "DELETE",
  });
}

export function getProjectActivity(projectId: string) {
  return apiRequest<{ events: ActivityEvent[] }>(
    `/api/v1/projects/${projectId}/activity`,
  );
}

// ---------------------------------------------------------------------
// YouTube Script Studio (founder priority #12)
// ---------------------------------------------------------------------

export type ScriptSection = {
  id: string;
  heading: string;
  body: string;
  metrics?: ScriptMetrics;
};

export type ScriptMetrics = {
  word_count: number;
  sentence_count: number;
  estimated_minutes: number;
  words_per_minute?: number;
  target_minutes?: number | null;
  target_words?: number | null;
  words_vs_target?: number | null;
};

export type ScriptAnalysisSentence = {
  sentence_number: number;
  text: string;
  role: string | null;
  subject_id: string | null;
  degraded: boolean;
};

export type ScriptAnalysis = {
  mode: string;
  sentence_count: number;
  sentences: ScriptAnalysisSentence[];
  scenes: {
    scene_number: number;
    sentence_count: number;
    sentence_numbers: (number | null)[];
    text: string;
  }[];
  bible: {
    title: string | null;
    story_type: string | null;
    tone: string | null;
    primary_subject_id: string | null;
    entities: { canonical: string; type: string; aliases: string[] }[];
  };
  entities: string[];
  locations: string[];
  dates: string[];
  at: string;
};

export type ScriptFact = {
  fact_id: string;
  kind: string;
  basis: string;
  sentence_number?: number;
  when?: string;
  what?: string;
  display?: string;
  year?: number;
  subjects?: string[];
  source?: Record<string, unknown>;
  kb_check?: { query: string; status: string | null; source: string | null } | null;
};

export type ScriptFactCheck = {
  at: string;
  mode: string;
  record: {
    sentences: number;
    facts: number;
    dated_events: number;
    dates: number;
    quantities: number;
  };
  corroboration: {
    facts_considered: number;
    checked: number;
    corroborated: number;
    not_corroborated: number;
    skipped_no_subject_or_year: number;
    lookups: number;
  };
  facts: ScriptFact[];
};

export type ScriptDraft = {
  id: string;
  title: string;
  /** Item 3: the documentary's public video title — generated
   * automatically with the script, editable, carried into the
   * production project. Null until one exists. */
  video_title: string | null;
  /** Item 6: the script's language; "en" when unset. */
  language?: string;
  translated_from?: {
    script_id: string;
    source_language: string;
    at: string;
  } | null;
  category: string;
  target_minutes: number | null;
  project_id: string | null;
  productions: { project_id: string; at: string; via: string }[];
  created_at: string;
  updated_at: string;
  metrics: ScriptMetrics;
  section_count: number;
  has_analysis: boolean;
  has_fact_check: boolean;
  sections?: ScriptSection[];
  script_text?: string;
  generation?: {
    topic: string;
    angle: string;
    outline: { heading: string; intent: string; word_budget: number }[];
    basis: string;
    at: string;
  } | null;
  analysis?: ScriptAnalysis | null;
  fact_check?: ScriptFactCheck | null;
  revision_count?: number;
};

export type ScriptRevision = {
  id: string;
  at: string;
  source: string;
  title: string;
  category: string;
  target_minutes: number | null;
  metrics: ScriptMetrics;
};

export type ScriptExpandProposal = {
  script_id: string;
  proposal: { sections: (ScriptSection & { word_budget: number })[] };
  basis: string;
};

export type ScriptTightenProposal = {
  script_id: string;
  proposal: {
    section_id: string;
    original: string;
    tightened: string;
    original_words: number;
    tightened_words: number;
  };
  basis: string;
};

export function listScriptDrafts(signal?: AbortSignal) {
  return apiRequest<{ scripts: ScriptDraft[] }>("/api/v1/script-studio/scripts", { signal });
}

export function createScriptDraft(body: {
  title?: string;
  video_title?: string;
  category?: string;
  target_minutes?: number | null;
  sections?: { heading?: string; body?: string }[];
}) {
  return apiRequest<ScriptDraft>("/api/v1/script-studio/scripts", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getScriptDraft(scriptId: string, signal?: AbortSignal) {
  return apiRequest<ScriptDraft>(`/api/v1/script-studio/scripts/${scriptId}`, { signal });
}

export function updateScriptDraft(
  scriptId: string,
  body: {
    title?: string;
    video_title?: string | null;
    clear_video_title?: boolean;
    category?: string;
    target_minutes?: number | null;
    clear_target?: boolean;
    sections?: { id?: string; heading?: string; body?: string }[];
  },
) {
  return apiRequest<ScriptDraft>(`/api/v1/script-studio/scripts/${scriptId}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export async function deleteScriptDraft(scriptId: string) {
  await apiRequest(`/api/v1/script-studio/scripts/${scriptId}`, { method: "DELETE" });
}

export function listScriptRevisions(scriptId: string, signal?: AbortSignal) {
  return apiRequest<{ revisions: ScriptRevision[] }>(
    `/api/v1/script-studio/scripts/${scriptId}/revisions`,
    { signal },
  );
}

export function restoreScriptRevision(scriptId: string, revisionId: string) {
  return apiRequest<ScriptDraft>(
    `/api/v1/script-studio/scripts/${scriptId}/revisions/${revisionId}/restore`,
    { method: "POST" },
  );
}

export function runScriptAnalysis(scriptId: string) {
  return apiRequest<{ script_id: string; analysis: ScriptAnalysis }>(
    `/api/v1/script-studio/scripts/${scriptId}/analysis`,
    { method: "POST" },
  );
}

export function runScriptFactCheck(scriptId: string) {
  return apiRequest<{ script_id: string; fact_check: ScriptFactCheck }>(
    `/api/v1/script-studio/scripts/${scriptId}/fact-check`,
    { method: "POST" },
  );
}

export function generateScriptDraft(body: {
  topic: string;
  angle?: string;
  category?: string;
  target_minutes?: number | null;
  language?: string;
  research_id?: string;
}) {
  return apiRequest<ScriptDraft>("/api/v1/script-studio/ai/draft", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Item 6: translate a draft into a NEW draft in the target
 * language; the original is kept unchanged. */
export function translateScriptDraft(scriptId: string, language: string) {
  return apiRequest<ScriptDraft>(
    `/api/v1/script-studio/scripts/${scriptId}/translate`,
    { method: "POST", body: JSON.stringify({ language }) },
  );
}

export function expandScriptSections(scriptId: string) {
  return apiRequest<ScriptExpandProposal>(
    `/api/v1/script-studio/scripts/${scriptId}/ai/expand`,
    { method: "POST" },
  );
}

export function tightenScriptPassage(
  scriptId: string,
  body: { section_id: string; text?: string },
) {
  return apiRequest<ScriptTightenProposal>(
    `/api/v1/script-studio/scripts/${scriptId}/ai/tighten`,
    { method: "POST", body: JSON.stringify(body) },
  );
}

export function sendScriptToProduction(scriptId: string) {
  return apiRequest<{ script_id: string; project_id: string; draft: ScriptDraft }>(
    `/api/v1/script-studio/scripts/${scriptId}/send-to-production`,
    { method: "POST" },
  );
}

export function attachScriptToProject(scriptId: string, projectId: string) {
  return apiRequest<{ script_id: string; project_id: string; draft: ScriptDraft }>(
    `/api/v1/script-studio/scripts/${scriptId}/attach`,
    { method: "POST", body: JSON.stringify({ project_id: projectId }) },
  );
}

// ---------------------------------------------------------------------
// Templates & production presets (founder priority #15) — named
// bundles over the backend's template service. Built-ins are
// derived from the real genre packs (read-only; copy to edit);
// user templates live in the caller's own account. The options a
// template may carry are exactly the options the production
// pipeline consumes — the backend refuses anything else, and
// there is deliberately no narration-speed field anywhere.
// ---------------------------------------------------------------------

export type TemplateOptions = {
  genre?: string;
  voice_id?: string;
  captions?: "burn" | "sidecar" | "off";
  caption_sync?: "word" | "off";
  music_file?: string;
  music_enabled?: boolean;
  music_volume?: number;
  narration_volume?: number;
  music_fade_in_seconds?: number;
  music_fade_out_seconds?: number;
  music_loop_crossfade_seconds?: number;
  duck_threshold?: number;
  duck_ratio?: number;
  duck_attack_ms?: number;
  duck_release_ms?: number;
  require_plan_approval?: boolean;
};

export type TemplateRecord = {
  id: string;
  kind: "builtin" | "user";
  name: string;
  category: string;
  target_minutes: number | null;
  options: TemplateOptions;
  sections: { heading: string | null }[];
  source: {
    kind: string;
    pack_id?: string;
    label?: string | null;
    description?: string | null;
    style_family?: string | null;
    visual_theme?: string | null;
    project_id?: string;
    script_id?: string;
  } | null;
  created_at: string | null;
  updated_at: string | null;
};

export type TemplateDroppedOption = { key: string; reason: string };

export function listTemplates(signal?: AbortSignal) {
  return apiRequest<{ templates: TemplateRecord[]; builtins: TemplateRecord[] }>(
    "/api/v1/templates",
    { signal },
  );
}

export function createTemplate(body: {
  name?: string;
  category?: string;
  target_minutes?: number | null;
  options?: TemplateOptions;
  sections?: { heading?: string }[];
}) {
  return apiRequest<TemplateRecord>("/api/v1/templates", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function updateTemplate(
  templateId: string,
  body: {
    name?: string;
    category?: string;
    target_minutes?: number | null;
    clear_target?: boolean;
    options?: TemplateOptions;
    sections?: { heading?: string }[];
  },
) {
  return apiRequest<TemplateRecord>(`/api/v1/templates/${templateId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function deleteTemplate(templateId: string) {
  await apiRequest(`/api/v1/templates/${templateId}`, { method: "DELETE" });
}

export function copyBuiltinTemplate(packId: string, name?: string) {
  return apiRequest<TemplateRecord>(`/api/v1/templates/builtins/${packId}/copy`, {
    method: "POST",
    body: JSON.stringify({ name: name ?? null }),
  });
}

export function createTemplateFromProject(projectId: string, name?: string) {
  return apiRequest<{ template: TemplateRecord; dropped_options: TemplateDroppedOption[] }>(
    "/api/v1/templates/from-project",
    { method: "POST", body: JSON.stringify({ project_id: projectId, name: name ?? null }) },
  );
}

export function createTemplateFromDraft(scriptId: string, name?: string) {
  return apiRequest<TemplateRecord>("/api/v1/templates/from-draft", {
    method: "POST",
    body: JSON.stringify({ script_id: scriptId, name: name ?? null }),
  });
}

export function applyTemplateToDraft(templateId: string, title?: string) {
  return apiRequest<ScriptDraft>(`/api/v1/templates/${templateId}/apply-draft`, {
    method: "POST",
    body: JSON.stringify({ title: title ?? null }),
  });
}

export function applyTemplateToProject(
  templateId: string,
  body: { title?: string; script: string },
) {
  return apiRequest<ProjectDetail>(`/api/v1/templates/${templateId}/apply-project`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------------
// Developer API (Phase N, founder #16): per-user API keys, webhooks
// and the generated documentation. Key and signing secrets exist
// ONLY in the create responses below — no read function can return
// one, because the backend stores only their hashes.
// ---------------------------------------------------------------------

export type DeveloperApiKey = {
  id: string;
  name: string;
  key_prefix: string;
  scopes: string[];
  rate_limit_per_minute: number;
  rate_limit_is_default: boolean;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
  request_count: number;
  status: "active" | "revoked" | "expired";
};

export type CreatedApiKey = DeveloperApiKey & { secret: string };

export type ApiKeyList = {
  keys: DeveloperApiKey[];
  max_keys_per_user: number;
  default_rate_limit_per_minute: number;
};

export function listApiKeys(signal?: AbortSignal) {
  return apiRequest<ApiKeyList>("/api/v1/account/api-keys", { signal });
}

export function createApiKey(body: {
  name: string;
  scopes: string[];
  expires_at?: string | null;
  rate_limit_per_minute?: number | null;
}) {
  return apiRequest<CreatedApiKey>("/api/v1/account/api-keys", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function revokeApiKey(keyId: string) {
  return apiRequest<DeveloperApiKey>(`/api/v1/account/api-keys/${keyId}`, {
    method: "DELETE",
  });
}

export type WebhookEndpointView = {
  id: string;
  url: string;
  events: string[];
  created_at: string;
  disabled_at: string | null;
  status: "active" | "disabled";
};

export type CreatedWebhook = WebhookEndpointView & {
  signing_secret: string;
};

export type WebhookDeliveryView = {
  id: string;
  endpoint_id: string;
  delivery_group: string;
  event: string;
  run_id: string | null;
  project_id: string | null;
  attempt: number;
  response_status: number | null;
  error: string | null;
  created_at: string;
};

export function listWebhooks(signal?: AbortSignal) {
  return apiRequest<{ endpoints: WebhookEndpointView[]; events: string[] }>(
    "/api/v1/account/webhooks",
    { signal },
  );
}

export function createWebhook(body: { url: string; events: string[] }) {
  return apiRequest<CreatedWebhook>("/api/v1/account/webhooks", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function disableWebhook(endpointId: string) {
  return apiRequest<WebhookEndpointView>(
    `/api/v1/account/webhooks/${endpointId}`,
    { method: "DELETE" },
  );
}

export function listWebhookDeliveries(endpointId: string) {
  return apiRequest<{ deliveries: WebhookDeliveryView[] }>(
    `/api/v1/account/webhooks/${endpointId}/deliveries`,
  );
}

export type DeveloperDocEndpoint = {
  method: string;
  path: string;
  scope: string;
  summary: string;
  curl: string;
};

export type DeveloperDocs = {
  base_path: string;
  auth: { header: string; format: string; notes: string };
  scopes: Array<{ name: string; description: string }>;
  rate_limit: {
    default_per_minute: number;
    window_seconds: number;
    per_key_override: string;
    overage: string;
  };
  errors: { shape: unknown; codes: string[] };
  endpoints: DeveloperDocEndpoint[];
  webhooks: {
    events: string[];
    headers: Record<string, string>;
    signature: string;
    retries: string;
    payload_example: unknown;
  };
};

export function getDeveloperDocs(signal?: AbortSignal) {
  return apiRequest<DeveloperDocs>("/api/v1/developer/docs", { signal });
}

export type AdminDeveloperOverview = {
  api_keys: Array<DeveloperApiKey & { owner_email: string | null }>;
  webhooks: Array<
    WebhookEndpointView & { owner_email: string | null; delivery_count: number }
  >;
  totals: {
    api_keys: number;
    active_api_keys: number;
    api_requests_recorded: number;
    webhooks: number;
    webhook_deliveries: number;
  };
};

export function getAdminDeveloperOverview(signal?: AbortSignal) {
  return apiRequest<AdminDeveloperOverview>("/api/v1/admin/developer", {
    signal,
  });
}

// ---------------------------------------------------------------------
// Phase 9 — Pre-Production Configuration Studio (§§13–17). Every
// type mirrors services/production_config.py's recorded shapes;
// the catalogue is generated by the backend from the live
// systems, so the form offers only what the pipeline consumes.
// ---------------------------------------------------------------------

export type ProductionConfig = {
  version: number;
  preset: string;
  motion_graphics: { enabled: boolean; intensity: string };
  theme: {
    id: string | null;
    base?: string;
    tokens?: Record<string, number[]>;
  };
  music: {
    enabled: boolean;
    source: string;
    upload: ConfigUploadRecord | null;
    track_id: string | null;
    mood: string | null;
    volume: number | null;
    placement: string;
    sections: Array<{ start: number; end: number }>;
    mix: Record<string, number>;
  };
  narration: {
    source: string;
    voice_id: string | null;
    voice_recording: ConfigUploadRecord | null;
    /** Item 6: the production language (defaults to English). */
    language?: string;
  };
  captions: { mode: string; sync: string };
};

export type ConfigUploadRecord = {
  id: string | null;
  file_name: string | null;
  file_path: string;
  content_sha256: string | null;
  duration_seconds: number | null;
  codec: string | null;
  uploaded_at: string | null;
};

export type ConfigValidation = {
  errors: Array<{ code: string; message: string }>;
  warnings: Array<{ code: string; message: string }>;
};

export type ProjectConfigView = {
  project_id: string;
  stored: boolean;
  config: ProductionConfig;
  preset_match: string;
  flat_options: Record<string, unknown>;
  validation: ConfigValidation;
};

export type ConfigCatalogue = {
  themes: Array<{
    id: string;
    engine_name: string;
    swatch: Record<string, number[]>;
    background_pattern: string | null;
  }>;
  custom_theme: { bases: string[]; token_keys: string[] };
  presets: Array<{ id: string; label: string; config: ProductionConfig }>;
  caption_modes: string[];
  caption_syncs: string[];
  music_placements: string[];
  music_library: {
    configured: boolean;
    available: boolean;
    error?: string;
    tracks: Array<{
      track_id: string;
      title: string | null;
      composer: string | null;
      moods: string[];
      duration_seconds: number | null;
      licence_state: string;
      source_class: string;
    }>;
  };
  narration: {
    adapter: string;
    provider: string;
    voices: Array<{ id: string; is_default: boolean; installed: boolean }>;
    note: string;
  };
};

export type VoiceTranscript = {
  provider: string;
  language: string | null;
  duration: number | null;
  text: string;
  segments: Array<{
    start: number;
    end: number;
    text: string;
    unclear: boolean;
  }>;
  unclear_sections: number[];
  speakers: null;
  speaker_note: string;
};

export function getConfigCatalogue(signal?: AbortSignal) {
  return apiRequest<ConfigCatalogue>("/api/v1/config/catalogue", { signal });
}

export function getConfigDefaults(signal?: AbortSignal) {
  return apiRequest<{ defaults: ProductionConfig | null }>(
    "/api/v1/config/defaults",
    { signal },
  );
}

export function saveConfigDefaults(config: ProductionConfig) {
  return apiRequest<{ defaults: ProductionConfig }>("/api/v1/config/defaults", {
    method: "PUT",
    body: JSON.stringify({ config }),
  });
}

export function clearConfigDefaults() {
  return apiRequest<{ defaults: null }>("/api/v1/config/defaults", {
    method: "DELETE",
  });
}

export function getProjectConfig(projectId: string, signal?: AbortSignal) {
  return apiRequest<ProjectConfigView>(`/api/v1/projects/${projectId}/config`, {
    signal,
  });
}

export function saveProjectConfig(projectId: string, config: ProductionConfig) {
  return apiRequest<ProjectConfigView>(`/api/v1/projects/${projectId}/config`, {
    method: "PUT",
    body: JSON.stringify({ config }),
  });
}

export function validateProjectConfig(
  projectId: string,
  config: ProductionConfig,
) {
  return apiRequest<{
    config: ProductionConfig;
    flat_options: Record<string, unknown>;
    validation: ConfigValidation;
  }>(`/api/v1/projects/${projectId}/config/validate`, {
    method: "POST",
    body: JSON.stringify({ config }),
  });
}

export function uploadConfigFile(
  projectId: string,
  kind: "music" | "voice",
  file: File,
) {
  const form = new FormData();
  form.set("kind", kind);
  form.set("file", file);
  return apiRequest<{ upload: ConfigUploadRecord; config: ProjectConfigView }>(
    `/api/v1/projects/${projectId}/config/uploads`,
    { method: "POST", body: form },
  );
}

export function removeConfigMusicUpload(projectId: string) {
  return apiRequest<ProjectConfigView>(
    `/api/v1/projects/${projectId}/config/music-upload`,
    { method: "DELETE" },
  );
}

export function getVoiceTranscript(projectId: string, signal?: AbortSignal) {
  return apiRequest<VoiceTranscript>(
    `/api/v1/projects/${projectId}/config/voice/transcript`,
    { signal },
  );
}

export function transcribeVoice(projectId: string) {
  return apiRequest<VoiceTranscript>(
    `/api/v1/projects/${projectId}/config/voice/transcribe`,
    { method: "POST" },
  );
}

export function treatVoiceScript(
  projectId: string,
  mode: string,
  text?: string,
) {
  return apiRequest<{ mode: string; text: string; source: string }>(
    `/api/v1/projects/${projectId}/config/voice/treat`,
    { method: "POST", body: JSON.stringify({ mode, text: text ?? null }) },
  );
}

export function applyVoiceScript(projectId: string, text: string) {
  return apiRequest<{
    project_id: string;
    draft: { id: string };
    script_applied: boolean;
  }>(`/api/v1/projects/${projectId}/config/voice/apply`, {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}
