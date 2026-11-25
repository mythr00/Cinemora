/**
 * MarketingNavigation (Phase 6, A-01): account entries follow
 * the /auth/me session. Unresolved session renders NEITHER
 * signed-in nor signed-out links (no wrong-state flash);
 * signed-out visitors get "Sign in" + "Create account"; a
 * signed-in visitor gets "Open app" instead. lib/api is mocked
 * at the module boundary (getCurrentUser resolves null on a 401,
 * exactly like the real helper).
 */
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MarketingNavigation } from "@/components/marketing-navigation";
import { getCurrentUser } from "@/lib/api";
import { makeUser } from "../helpers/fixtures";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, getCurrentUser: vi.fn() };
});

const mockUser = vi.mocked(getCurrentUser);

beforeEach(() => {
  vi.clearAllMocks();
});

describe("MarketingNavigation session states", () => {
  it("renders no account links while the session is unresolved", () => {
    mockUser.mockReturnValue(new Promise(() => undefined));
    render(<MarketingNavigation />);
    expect(screen.queryByRole("link", { name: "Sign in" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Create account" })).toBeNull();
    expect(screen.queryByRole("link", { name: /Open app/ })).toBeNull();
    // The non-account navigation is present regardless.
    expect(screen.getByRole("link", { name: "Projects" })).toBeInTheDocument();
  });

  it("shows Sign in and Create account when signed out (401 from /auth/me)", async () => {
    mockUser.mockResolvedValue(null);
    render(<MarketingNavigation />);
    const signIns = await screen.findAllByRole("link", { name: "Sign in" });
    expect(signIns.length).toBeGreaterThan(0);
    for (const link of signIns) expect(link).toHaveAttribute("href", "/login");
    const creates = screen.getAllByRole("link", { name: /Create account/ });
    expect(creates.length).toBeGreaterThan(0);
    for (const link of creates) {
      expect(link).toHaveAttribute("href", "/register");
    }
    expect(screen.queryByRole("link", { name: /Open app/ })).toBeNull();
  });

  it("shows Open app and no login links when signed in", async () => {
    mockUser.mockResolvedValue(makeUser("user"));
    render(<MarketingNavigation />);
    const openApp = await screen.findAllByRole("link", { name: /Open app/ });
    expect(openApp.length).toBeGreaterThan(0);
    for (const link of openApp) {
      expect(link).toHaveAttribute("href", "/studio");
    }
    expect(screen.queryByRole("link", { name: "Sign in" })).toBeNull();
    expect(screen.queryByRole("link", { name: /Create account/ })).toBeNull();
  });

  it("resolves a network failure to the signed-out entries (public page)", async () => {
    mockUser.mockRejectedValue(new TypeError("Failed to fetch"));
    render(<MarketingNavigation />);
    expect(
      await screen.findAllByRole("link", { name: "Sign in" }),
    ).not.toHaveLength(0);
    expect(screen.queryByRole("link", { name: /Open app/ })).toBeNull();
  });
});
