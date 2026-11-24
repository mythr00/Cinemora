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
): Promise<AccountUser> {
  const data = await apiRequest<{ user: AccountUser }>(
    "/api/v1/auth/register",
    {
      method: "POST",
      body: JSON.stringify({
        email,
        password,
        display_name: displayName || undefined,
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
  certificate_url: string;
  plan_url: string;
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
 * - "auth": the API answered but rejected the session (401) —
 *   the visitor is signed out, or the session expired or was
 *   revoked; signing in again is the fix.
 * - "frontend-not-configured": retained for state-shape
 *   compatibility; the session model has no build-time credential
 *   to be missing, so checkApiConnection no longer produces it.
 * - "backend-not-configured": the server itself has no
 *   CINEMORA_API_TOKEN set and fails closed (503
 *   api_not_configured) for credential-less requests.
 * - "unavailable": the API answered with another server error.
 * - "network": the API could not be reached at all.
 */
export type ApiConnectionState =
  | "connected"
  | "auth"
  | "frontend-not-configured"
  | "backend-not-configured"
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
          state: "auth",
          message:
            "The studio API rejected your session. Sign in again to continue.",
        };
      }
      if (error.status === 503) {
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
  catalogues: {
    transitions: string[];
    transition_reasons: string[];
    graphic_templates: string[];
  };
};

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
