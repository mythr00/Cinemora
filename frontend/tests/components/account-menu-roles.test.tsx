/**
 * Item 7 (Product Improvements): no account-role labels in the
 * customer-facing account menu — for ANY role, including
 * super_admin. The plan line (customer-relevant) still renders.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AccountMenu } from "@/components/account-menu";
import { getAccountUsage, getNotifications } from "@/lib/api";
import { makeUser } from "../helpers/fixtures";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    getAccountUsage: vi.fn(),
    getNotifications: vi.fn(),
    logout: vi.fn(),
  };
});

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

const mockUsage = vi.mocked(getAccountUsage);
const mockNotifications = vi.mocked(getNotifications);

beforeEach(() => {
  vi.clearAllMocks();
  mockUsage.mockResolvedValue({
    plan: { name: "Creator", max_runs_per_period: 11 },
    runs_in_period: 2,
    concurrent_runs: 0,
  } as never);
  mockNotifications.mockResolvedValue({ unread_count: 0 } as never);
});

describe("AccountMenu role labels (item 7)", () => {
  it.each(["user", "admin", "super_admin"] as const)(
    "renders no role label for role=%s",
    async (role) => {
      render(<AccountMenu user={makeUser(role)} />);
      fireEvent.click(screen.getByRole("button"));
      await waitFor(() =>
        expect(screen.getByText(/Plan: Creator/)).toBeTruthy(),
      );
      const panel = document.querySelector(".account-menu-panel");
      expect(panel?.textContent).not.toMatch(/Role:/i);
      expect(panel?.textContent).not.toMatch(/super_admin/);
      expect(panel?.textContent).not.toMatch(/Administrator/);
      expect(
        document.querySelector('[aria-label="Administrator"]'),
      ).toBeNull();
    },
  );
});
