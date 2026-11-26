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
    monthly_credits: 1300,
    included_generations: 11,
    script_allowance: 40,
    thumbnail_allowance: 40,
    billing_mode: "metered",
    features: {},
    is_public: true,
    is_default: false,
    max_concurrent_runs: null,
    max_runs_per_period: null,
    priority_tier: 1,
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
    price_monthly_cents: 7900,
    price_annual_cents: 75840,
    monthly_credits: 3600,
    included_generations: 29,
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
      generations_used: 3,
      generations_included: 11,
      generations_remaining: 8,
      script_ops_used: 5,
      script_allowance: 40,
      script_remaining: 35,
      thumbnail_ops_used: 2,
      thumbnail_allowance: 40,
      thumbnail_remaining: 38,
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
    credit_pricing: { standard_generation_credits: 84, durations: [] },
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
