/**
 * Item 1: the affiliate dashboard renders the real ledger
 * shape — 12% lifetime rate, referral link with the code,
 * metric set, and the honest no-payout-provider statement.
 */
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AffiliatePage from "@/app/affiliate/page";
import { getAffiliateDashboard } from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getAffiliateDashboard: vi.fn(),
    enrollAffiliate: vi.fn(),
    setAffiliatePayoutPreference: vi.fn(),
    getCurrentUser: vi.fn().mockResolvedValue({
      id: "u1",
      email: "owner@example.com",
      display_name: "Owner",
      role: "user",
      status: "active",
    }),
    listProjects: vi.fn().mockResolvedValue({ projects: [] }),
  };
});

vi.mock("next/navigation", () => ({
  usePathname: () => "/affiliate",
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

const mockDashboard = vi.mocked(getAffiliateDashboard);

beforeEach(() => {
  vi.clearAllMocks();
});

describe("Affiliate page (item 1)", () => {
  it("offers enrolment when not enrolled", async () => {
    mockDashboard.mockResolvedValue({
      enrolled: false,
      rate_percent: 12,
      lifetime: true,
    });
    render(<AffiliatePage />);
    expect(
      await screen.findByRole("button", { name: "Enrol and get my link" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/12% lifetime commission/)).toBeInTheDocument();
  });

  it("renders link, metrics and honest payout wording", async () => {
    mockDashboard.mockResolvedValue({
      enrolled: true,
      code: "CM-ABCD1234",
      status: "active",
      rate_percent: 12,
      lifetime: true,
      payout_preference: null,
      payout_preferences: ["bank_transfer", "paypal", "wise"],
      payout_provider_connected: false,
      metrics: {
        link_clicks: 10,
        registrations: 2,
        referred_customers: 2,
        active_customers: 1,
        conversion_percent: 20.0,
        lifetime_earnings_cents: 348,
        pending_cents: 348,
        approved_cents: 0,
        paid_cents: 0,
        reversed_cents: 0,
      },
      recent_activity: [
        {
          id: "e1",
          kind: "accrual",
          gross_cents: 2900,
          rate_percent: 12,
          commission_cents: 348,
          status: "pending",
          referred_user_id: "u2",
        },
      ],
      payout_history: [],
    });
    render(<AffiliatePage />);
    expect((await screen.findAllByText(/CM-ABCD1234/)).length).toBeGreaterThan(0);
    expect(screen.getByText(/\/register\?ref=CM-ABCD1234/)).toBeInTheDocument();
    expect(screen.getByText("Recorded link visits")).toBeInTheDocument();
    expect(screen.getAllByText("$3.48").length).toBeGreaterThan(0);
    expect(
      screen.getByText(/no payout provider is connected yet/),
    ).toBeInTheDocument();
    expect(screen.getByText("No payouts yet.")).toBeInTheDocument();
  });
});
