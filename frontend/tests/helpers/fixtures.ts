/**
 * Shared fixtures for the Phase 9 frontend suite. Shapes mirror
 * the real lib/api types; partial records are cast at the seam so
 * each test states only the fields its behaviour depends on.
 */
import type {
  AccountUser,
  BillingPlan,
  BillingSummary,
  ProjectOutputs,
  ProjectStatus,
  RetentionView,
  RunSummary,
} from "@/lib/api";

export function makeUser(role: AccountUser["role"] = "user"): AccountUser {
  return {
    id: "user-1",
    email: "maker@example.com",
    display_name: "Maker",
    role,
    status: "active",
    email_verified_at: "2026-11-01T00:00:00Z",
    created_at: "2026-11-01T00:00:00Z",
    last_login_at: null,
  };
}

export function makeRun(status: string, run_id = `run-${status}`): RunSummary {
  return { run_id, project_id: "project-1", status };
}

export function makeStatus(partial: Record<string, unknown>): ProjectStatus {
  return partial as unknown as ProjectStatus;
}

export function makeOutputs(filmExists: boolean): ProjectOutputs {
  const entry = (exists: boolean) =>
    ({
      exists,
      filename: exists ? "final.mp4" : null,
      media_url: exists ? "/media/final.mp4" : null,
    }) as ProjectOutputs["film"];
  return {
    project_id: "project-1",
    film: entry(filmExists),
    captions: { json: entry(false), srt: entry(false), vtt: entry(false) },
    asset_report: entry(false),
    rights_manifest: entry(false),
    run_manifest: entry(false),
    certificate_url: null,
    plan_url: null,
  };
}

export function makePlan(partial: Partial<BillingPlan> = {}): BillingPlan {
  return {
    id: "plan-creator",
    name: "creator",
    display_name: "Creator",
    price_monthly_cents: 2900,
    price_annual_cents: 27840,
    currency: "USD",
    monthly_credits: 990,
    included_generations: 11,
    script_allowance: 11,
    thumbnail_allowance: 11,
    billing_mode: "metered",
    features: { generation_credits_per_minute: 6 },
    is_public: true,
    is_default: false,
    max_concurrent_runs: null,
    max_runs_per_period: null,
    priority_tier: 1,
    generation_credits_per_minute: 6,
    standard_generation_credits: 90,
    standard_documentaries_per_month: 11,
    effective_cost_per_standard_video_cents: 255,
    ...partial,
  };
}

export function makeBillingSummary(
  overrides: Partial<BillingSummary> = {},
): BillingSummary {
  const creator = makePlan();
  const studio = makePlan({
    id: "plan-studio",
    name: "studio",
    display_name: "Studio",
    price_monthly_cents: 6300,
    price_annual_cents: 60480,
    monthly_credits: 2325,
    included_generations: 31,
    script_allowance: 31,
    thumbnail_allowance: 31,
    features: { generation_credits_per_minute: 5 },
    generation_credits_per_minute: 5,
    standard_generation_credits: 75,
    standard_documentaries_per_month: 31,
    effective_cost_per_standard_video_cents: 203,
  });
  return {
    plan: creator,
    subscription: {
      id: "sub-1",
      plan_id: creator.id,
      plan_name: "creator",
      status: "active",
      billing_interval: "monthly",
      current_period_start: "2026-11-01T00:00:00Z",
      current_period_end: "2026-12-01T00:00:00Z",
      cancel_at_period_end: false,
      pending_plan_name: null,
      billing_provider: "paddle",
    },
    period: {
      period_start: "2026-11-01T00:00:00Z",
      period_end: "2026-12-01T00:00:00Z",
      generations: { granted: 11, used: 3, remaining: 8 },
      scripts: { granted: 11, used: 5, remaining: 6 },
      thumbnails: { granted: 11, used: 2, remaining: 9 },
    },
    wallet: {
      subscription: { available: 900, reserved: 0, next_expiry_at: null },
      purchased: { available: 120, reserved: 0, next_expiry_at: null },
      promo: { available: 25, reserved: 0, next_expiry_at: null },
      total_available: 1045,
    },
    available_plans: [creator, studio],
    topup_packages: [
      { id: "topup-120", name: "120 credits", credits: 120, price_cents: 800, currency: "USD" },
    ],
    payment_provider: {
      name: "paddle",
      configured: true,
      public_config: { client_token: "test_client_token", environment: "sandbox" },
    },
    credit_pricing: {
      standard_generation_credits: 90,
      duration_cost_table: [
        { minutes: 5, credits: 30 },
        { minutes: 10, credits: 60 },
        { minutes: 15, credits: 90 },
        { minutes: 20, credits: 120 },
        { minutes: 30, credits: 180 },
      ],
      default_credits_per_minute: 6,
      plans: {
        creator: {
          credits_per_minute: 6,
          standard_generation_credits: 90,
          duration_cost_table: [],
        },
        studio: {
          credits_per_minute: 5,
          standard_generation_credits: 75,
          duration_cost_table: [],
        },
      },
      script_operations: { draft: 3, expand: 3, tighten: 3 },
      thumbnail_operations: { headlines: 3, compose: 5 },
    },
    ...overrides,
  };
}

export function makeRetention(
  overrides: Partial<NonNullable<RetentionView>> = {},
): NonNullable<RetentionView> {
  return {
    headline: "Download your video within 50 hours",
    retention_hours: 50,
    next_expiry_at: "2026-11-27T20:00:00.000Z",
    videos: [
      {
        id: "video-1",
        filename: "final.mp4",
        kind: "final",
        size_bytes: 1024,
        created_at: "2026-11-25T18:00:00.000Z",
        delete_after: "2026-11-27T20:00:00.000Z",
        status: "active",
        downloaded: false,
        download_count: 0,
        deleted_at: null,
      },
    ],
    ...overrides,
  };
}
