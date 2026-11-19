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

/** Call a legacy (pre-v1) endpoint through the same client. */
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

export type Certificate = {
  certificate_version: number;
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

export function getHealth(signal?: AbortSignal) {
  return apiRequest<{ status: string; service: string; version: string }>(
    "/api/v1/health",
    { signal },
  );
}

export function listProjects(signal?: AbortSignal) {
  return apiRequest<{ projects: ProjectSummary[] }>("/api/v1/projects", { signal });
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
