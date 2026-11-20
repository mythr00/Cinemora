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
 *   single-operator token auth via the X-API-Key header, consistent
 *   {error: {code, message}} failures surfaced as ApiClientError.
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
 *   http://localhost:8000).
 * - NEXT_PUBLIC_CINEMORA_API_TOKEN: the operator token matching the
 *   backend's CINEMORA_API_TOKEN, for local use.
 */

const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000"
).replace(/\/$/, "");

const API_TOKEN = process.env.NEXT_PUBLIC_CINEMORA_API_TOKEN || "";

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
  if (authenticated && API_TOKEN) {
    headers.set("X-API-Key", API_TOKEN);
  }
  const response = await fetch(apiUrl(path), {
    cache: "no-store",
    ...init,
    headers,
  });
  if (!response.ok) throw await parseError(response);
  return (await response.json()) as T;
}

/** Call a /api/v1 endpoint (token-authenticated). */
export function apiRequest<T>(path: string, init: RequestOptions = {}): Promise<T> {
  return request<T>(path, init, true);
}

/**
 * Call a legacy (pre-v1) endpoint through the same client.
 *
 * IA-10 / F-06: KEPT deliberately. It has no active caller, but
 * the quarantined implementations under frontend/preserved/ call
 * it and are part of the typecheck; removing it would break
 * their compilation. (The other F-06 dead exports, getHealth and
 * listRevisionExports, had no caller anywhere — preserved code
 * included — and were removed.)
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

export type PlanDocument = {
  project_id: string;
  title?: string | null;
  plan_source: "plan_json" | "story_cache";
  analysis_mode?: string | null;
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
    verification?: { accepted?: boolean; reason?: string | null; transcript_check?: string | null } | null;
    rights?: { license: string; rights_status?: string | null; creator?: string | null; source?: string | null } | null;
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

/** Absolute URL for a project's recorded media key. */
export function mediaUrl(projectId: string, mediaKey: string): string {
  return apiUrl(`/api/v1/projects/${projectId}/media/${mediaKey}`);
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
  if (API_TOKEN) headers.set("X-API-Key", API_TOKEN);
  const response = await fetch(mediaUrl(projectId, mediaKey), {
    cache: "no-store",
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
  if (API_TOKEN) headers.set("X-API-Key", API_TOKEN);
  const response = await fetch(apiUrl(path), {
    cache: "no-store",
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
  let url: string;
  if (options.projectId && options.mediaKey) {
    url = (await fetchMediaObjectUrl(options.projectId, options.mediaKey)).url;
  } else if (options.path) {
    url = (await fetchAuthenticatedMediaObjectUrl(options.path)).url;
  } else {
    throw new Error("A media key or media path is required to download.");
  }
  const filename =
    options.filename ||
    (options.path
      ? filenameFromMediaPath(options.path)
      : "cinemora-download");
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
 * distinguished because each needs a different operator action:
 * - "connected": an authenticated v1 call succeeded.
 * - "auth": the API answered but rejected this build's token
 *   (401) — the token in NEXT_PUBLIC_CINEMORA_API_TOKEN does not
 *   match the server's CINEMORA_API_TOKEN.
 * - "frontend-not-configured": this frontend build carries no
 *   API token at all, so no authenticated call can succeed.
 * - "backend-not-configured": the server itself has no
 *   CINEMORA_API_TOKEN set and fails closed (503
 *   api_not_configured).
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
 * authenticated v1 call (the project list). The unauthenticated
 * /health route is deliberately NOT used here — it can report "ok"
 * while every real call fails 401/503 (audit D-05). The token is
 * only ever sent as the X-API-Key header — never in a URL, never
 * logged.
 */
export async function checkApiConnection(
  signal?: AbortSignal,
): Promise<ApiConnectionStatus> {
  if (!API_TOKEN) {
    return {
      state: "frontend-not-configured",
      message:
        "This frontend build has no API token configured (NEXT_PUBLIC_CINEMORA_API_TOKEN), so it cannot sign in to the studio API.",
    };
  }
  try {
    await listProjects(signal);
    return { state: "connected", message: "Authenticated API connection confirmed." };
  } catch (error) {
    if (error instanceof ApiClientError) {
      if (error.status === 401) {
        return {
          state: "auth",
          message:
            "The studio API rejected this build's token. The frontend token must match the server's CINEMORA_API_TOKEN.",
        };
      }
      if (error.status === 503) {
        return {
          state: "backend-not-configured",
          message:
            "The studio API is running but not configured: CINEMORA_API_TOKEN is not set on the server, so it refuses authenticated calls.",
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
    if (API_TOKEN) xhr.setRequestHeader("X-API-Key", API_TOKEN);
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

export type EditorGraphic = {
  template?: string | null;
  family?: string | null;
  style?: string | null;
  inputs?: Record<string, unknown> | null;
  engine_metadata?: Record<string, unknown> | null;
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

/** The 12 editor operations, with the backend's exact payload
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
  | { type: "set_volumes"; narration?: number; music?: number };

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

/**
 * The media key the backend derives for a project file
 * (services/plan_document.build_media_index: sha256 of the
 * project-relative POSIX path, first 20 hex chars). The editor
 * document carries absolute file paths, not media keys, so the
 * editor computes the key for a path under the project directory
 * and fetches through the authenticated media endpoint. Files the
 * media index does not record — uploads, editor-generated media,
 * revision exports — answer 404 there; callers must treat that as
 * "no in-app preview" and fall back honestly, never fake one.
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
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(relative),
  );
  const hex = [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  return hex.slice(0, 20);
}
