/**
 * Billing page (Phase 8 MON, §14): everything renders from the
 * server's billing summary. With payments unconfigured the page
 * must say so honestly and offer NO working checkout — every
 * purchase control is disabled and no checkout call can fire.
 * With a configured provider, plan name, allowances and the
 * three credit buckets render from the API data. lib/api is
 * mocked at the module boundary.
 */
import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import BillingPage from "@/app/billing/page";
import {
  getBillingPayments,
  getBillingSummary,
  getBillingTransactions,
  getCurrentUser,
  getNotifications,
  listProjects,
  startSubscriptionCheckout,
  startTopupCheckout,
} from "@/lib/api";
import {
  makeBillingSummary,
  makeUser,
} from "../helpers/fixtures";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    cancelSubscription: vi.fn(),
    changeSubscription: vi.fn(),
    getBillingPayments: vi.fn(),
    getBillingSummary: vi.fn(),
    getBillingTransactions: vi.fn(),
    getCurrentUser: vi.fn(),
    getNotifications: vi.fn(),
    listProjects: vi.fn(),
    markNotificationRead: vi.fn(),
    startSubscriptionCheckout: vi.fn(),
    startTopupCheckout: vi.fn(),
  };
});

const mockSummary = vi.mocked(getBillingSummary);

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getCurrentUser).mockResolvedValue(makeUser("user"));
  vi.mocked(listProjects).mockResolvedValue({ projects: [] } as never);
  vi.mocked(getBillingTransactions).mockResolvedValue({ entries: [] });
  vi.mocked(getBillingPayments).mockResolvedValue({ payments: [] });
  vi.mocked(getNotifications).mockResolvedValue({
    notifications: [],
    unread_count: 0,
  });
});

describe("BillingPage with payments unconfigured", () => {
  beforeEach(() => {
    mockSummary.mockResolvedValue(
      makeBillingSummary({
        payment_provider: {
          name: null,
          configured: false,
          public_config: { client_token: null, environment: "" },
        },
      }),
    );
  });

  it("shows the honest unconfigured state", async () => {
    render(<BillingPage />);
    expect(
      await screen.findByText(/Payments are not configured yet/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/checkout is\s+unavailable/),
    ).toBeInTheDocument();
  });

  it("offers no working checkout: every purchase button is disabled", async () => {
    render(<BillingPage />);
    await screen.findByText(/Payments are not configured yet/);
    const purchaseButtons = screen
      .getAllByRole("button")
      .filter((button) =>
        /Subscribe|Switch to this plan|Buy credits/.test(
          button.textContent ?? "",
        ),
      );
    expect(purchaseButtons.length).toBeGreaterThan(0);
    for (const button of purchaseButtons) {
      expect(button).toBeDisabled();
    }
    expect(startSubscriptionCheckout).not.toHaveBeenCalled();
    expect(startTopupCheckout).not.toHaveBeenCalled();
  });

  it("still shows the recorded plan and credits exactly as recorded", async () => {
    render(<BillingPage />);
    expect(await screen.findByText(/Creator · active/)).toBeInTheDocument();
    expect(screen.getByText("900 available")).toBeInTheDocument();
  });
});

describe("BillingPage with a configured provider", () => {
  beforeEach(() => {
    mockSummary.mockResolvedValue(makeBillingSummary());
  });

  it("does not show the unconfigured notice", async () => {
    render(<BillingPage />);
    expect(
      await screen.findByRole("heading", { name: "Billing & credits" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Payments are not configured yet/),
    ).toBeNull();
  });

  it("renders the current plan name and subscription state from the API", async () => {
    render(<BillingPage />);
    const planName = await screen.findByText(/Creator · active/);
    expect(planName).toBeInTheDocument();
    expect(planName.textContent).toContain("billed monthly");
  });

  it("renders period allowances from the API data", async () => {
    const { container } = render(<BillingPage />);
    await screen.findByText(/Creator · active/);
    expect(container.textContent).toContain("3 of 11 used");
    expect(container.textContent).toContain("8 remaining");
    expect(container.textContent).toContain("5 of 11 used");
    expect(container.textContent).toContain("2 of 11 used");
  });

  it("renders the three credit buckets from the wallet data", async () => {
    render(<BillingPage />);
    await screen.findByText(/Creator · active/);
    expect(screen.getByText("900 available")).toBeInTheDocument();
    expect(screen.getByText(/^120 available/)).toBeInTheDocument();
    expect(screen.getByText("25 available")).toBeInTheDocument();
  });

  it("renders available plans with prices and included allowances", async () => {
    render(<BillingPage />);
    const studioCard = (await screen.findByRole("heading", { name: "Studio" }))
      .closest("article")!;
    expect(within(studioCard).getByText(/\$63\.00/)).toBeInTheDocument();
    expect(
      within(studioCard).getByText(/2325 video credits monthly/),
    ).toBeInTheDocument();
    expect(
      within(studioCard).getByText(/5 credits\s+per finished minute/),
    ).toBeInTheDocument();
    expect(
      within(studioCard).getByText(/31 standard\s+15-minute documentaries \/ month/),
    ).toBeInTheDocument();
  });

  it("marks the current plan and enables subscribing to another plan", async () => {
    render(<BillingPage />);
    await screen.findByText(/Creator · active/);
    expect(
      screen.getByRole("button", { name: "Current plan" }),
    ).toBeDisabled();
    // An existing subscription turns the action into a plan switch.
    expect(
      screen.getByRole("button", { name: "Switch to this plan" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Buy credits" }),
    ).toBeEnabled();
  });

  it("shows the empty ledger and payments states from the API", async () => {
    render(<BillingPage />);
    expect(
      await screen.findByText("No credit activity recorded yet."),
    ).toBeInTheDocument();
    expect(screen.getByText("No payments recorded.")).toBeInTheDocument();
  });
});

describe("BillingPage failure state", () => {
  it("shows the error state when the summary cannot load", async () => {
    mockSummary.mockRejectedValue(new Error("billing offline"));
    render(<BillingPage />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Billing & credits" }),
    ).toBeNull();
  });
});
